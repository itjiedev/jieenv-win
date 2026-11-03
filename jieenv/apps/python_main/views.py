"""
python 视图层

页面视图:
- IndexView          首页：导航页，通过 ext_hooks 收集各环境提供者链接

JSON API:
- api_env_remove     移除环境记录
- api_env_set_default 设置默认环境
- api_pip_list       列出环境下的 pip 包
- api_pip_install    安装包
- api_pip_uninstall  卸载包
"""
import os
import time
import shutil
import logging
import pathlib
import subprocess
from functools import lru_cache

from django.urls import reverse
from django.http import JsonResponse, HttpResponse
from django.shortcuts import redirect
from django.views.generic.base import TemplateView, RedirectView
from django.views.generic.edit import FormView
from django.views.decorators.http import require_POST, require_GET

from jiecore.views import AppMixin
from jiecore.ext_hook_registry import call_hooks
from jiecore.app_services import call_service, ServiceNotFoundError, ServiceCallError
from jiecore.system_ops import run_elevated
from jiecore.op_log import log_operation
from jiecore import tasks

from . import pypi_config
from . import pip_manager
from . import requirements_parser
from .pycharm_installer import (
    get_pycharm_options, find_version_entry, default_install_dir,
    download_pycharm, run_silent_install, pycharm_detect_installed,
    PythonPycharmError,
)
from .vscode_installer import (
    get_vscode_options, get_vscode_latest, get_vscode_latest_all, download_vscode,
    run_silent_install as vscode_run_silent_install,
    default_install_dir as vscode_default_install_dir,
    find_vscode_exe, vscode_detect_installed, reset_vscode_settings,
    INSTALLER_LABELS as VSCODE_INSTALLER_LABELS,
    PythonVscodeError,
)

logger = logging.getLogger(__name__)

# 子进程隐藏窗口标志：拉起 cmd / Code.exe 时避免闪现黑色控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


class PythonMixin(AppMixin):
    parent_menu_id = 'runtimes'
    menu_id = 'python_main'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        path = self.request.path
        # 注入环境提供者导航数据（python/layout.html 需要）
        envs = call_hooks('python_main.envs', context={'request': self.request})
        # 按返回数据中的 sort 字段排序（注册级 sort 相同时由返回值决定顺序）
        envs.sort(key=lambda e: e.get('sort', 0))
        for env in envs:
            # content_url 含具体页面段（如 /runtimes/python_installer/index/），
            # 截取到应用级前缀（/runtimes/python_installer/）以匹配该应用所有子页面
            url = env['content_url']
            base = url.rstrip('/')
            last_slash = base.rfind('/')
            # base[:last_slash] 是去掉页面段后的部分，再补 '/' 得到应用级前缀
            # 但 last_slash 本身是 '/' 的位置，base[:last_slash] 不含它
            app_base = base[:last_slash] + '/' if last_slash > 0 else base + '/'
            env['is_current'] = path.startswith(app_base)

        # 收集子应用注册的“清理缓存”配置（python.clear_cache 扩展点）：
        # 贡献者返回 {env_id, endpoint, label?, confirm_text?}，按 env_id 挂到对应 env 上；
        # 未注册的 env 走 python 应用默认的 api_clear_cache。
        # clear_cache_cfgs = call_hooks('python_main.clear_cache',
        #                               context={'request': self.request})
        # clear_cache_by_env = {}
        # for cfg in clear_cache_cfgs:
        #     if isinstance(cfg, dict) and cfg.get('env_id') and cfg.get('endpoint'):
        #         clear_cache_by_env[cfg['env_id']] = cfg
        # for env in envs:
        #     cfg = clear_cache_by_env.get(env.get('tab_id'))
        #     if cfg:
        #         env['clear_cache_url'] = cfg['endpoint']
        #         env['clear_cache_label'] = cfg.get('label') or '清理下载缓存'
        #         env['clear_cache_confirm_text'] = cfg.get('confirm_text') or ''
        context['envs'] = envs

        # 顶部“清理缓存”按钮的目标：当前激活 env 若注册了自定义清理则用之，
        # 否则回退到 python 应用默认接口（清空 tmp/python/）
        active_env = next((e for e in envs if e.get('is_current')), None) or {}
        context['active_clear_cache'] = {
            'url': active_env.get('clear_cache_url') or reverse('python_main:api_clear_cache'),
            'label': active_env.get('clear_cache_label') or '清理下载缓存',
            'confirm_text': active_env.get('clear_cache_confirm_text') or '',
        }

        # 开发工具扩展数据
        dev_tools_tabs = call_hooks('python_main.tools.tabs', context={'request': self.request})
        context['dev_tools_tabs'] = dev_tools_tabs
        dev_tools_url = reverse('python_main:dev_tools')
        context['dev_tools_is_current'] = path.startswith(dev_tools_url)
        return context


# ── 页面视图 ─────────────────────────────────────────
class IndexView(PythonMixin, TemplateView):
    """首页：自动重定向到排序最前的环境提供者页面"""
    template_name = 'python_main/layout.html'

    def get(self, request, *args, **kwargs):
        envs = call_hooks('python_main.envs', context={'request': request})
        envs.sort(key=lambda e: e.get('sort', 0))
        if envs:
            return redirect(envs[0]['content_url'])
        else:
            return redirect(reverse('python_main:dev_tools'))
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Python 环境管理'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': None, 'active': True},
        ]
        return context


class DevToolsView(PythonMixin, TemplateView):
    """开发工具页：暂无扩展时显示提示信息"""
    template_name = 'python_main/dev_tools.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Python 开发工具'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': '开发工具', 'href': None, 'active': True},
        ]
        context['pycharm'] = get_pycharm_options(refresh=True)
        context['vscode'] = get_vscode_options(refresh=True)
        return context

from .pycharm_versions import get_pycharm_versions
from django.http import Http404
from django.utils.functional import cached_property

from .config import LIMIT_VERSION, TMP_DIR


class PycharmVersionsView(PythonMixin, TemplateView):
    """开发工具：PyCharm"""
    template_name = 'python_main/pycharm_versions.html'

    @cached_property
    def pycharm_versions(self):
        return get_pycharm_versions()

    @cached_property
    def versions_map(self):
        return self.pycharm_versions.get('versions') or {}

    @cached_property
    def main_version(self):
        main_version = self.kwargs.get('main_version')
        if main_version is not None:
            return main_version
        return next(iter(self.versions_map), None)

    @cached_property
    def version_list(self):
        if self.main_version not in self.versions_map:
            raise Http404('未知的 PyCharm 主版本')
        return self.versions_map[self.main_version]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'PyCharm - 版本选择'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': '开发工具', 'href': reverse('python_main:dev_tools'), 'active': False},
            {'title': 'Pycharm - 版本选择', 'href': None, 'active': True},
        ]
        context['fetched_at'] = self.pycharm_versions.get('fetched_at')
        context['main_versions'] = list(self.versions_map.keys())
        context['versions'] = self.version_list
        context['current_main_version'] = self.main_version
        context['current_pycharm'] = get_pycharm_options()
        return context


class PycharmDetailView(PythonMixin, TemplateView):
    template_name = 'python_main/pycharm_detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'PyCharm - 详细信息'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': '开发工具', 'href': reverse('python_main:dev_tools'), 'active': False},
            {'title': 'Pycharm - 详细信息', 'href': None, 'active': True},
        ]
        context['pycharm'] = get_pycharm_options()
        return context

class PycharmVersionsRefreshView(RedirectView):
    """刷新 PyCharm 版本列表"""
    def get_redirect_url(self, *args, **kwargs):
        logger.info('刷新 PyCharm 版本列表')
        get_pycharm_versions(refresh=True, limit_ver=LIMIT_VERSION)
        return reverse('python_main:pycharm_versions')


class  PycharmUninstallView(PythonMixin, TemplateView):
    template_name = 'python_main/pycharm_uninstall.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '卸载 Pycharm'
        context['pycharm'] = get_pycharm_options()
        return context


class PycharmInstallView(PythonMixin, TemplateView):
    template_name = 'python_main/pycharm_install.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        version = self.kwargs.get('version', '')
        entry = find_version_entry(version)
        context['page_title'] = f'安装 PyCharm {version}'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': '开发工具', 'href': reverse('python_main:dev_tools'), 'active': False},
            {'title': 'Pycharm - 版本选择', 'href': reverse('python_main:pycharm_versions'), 'active': False},
            {'title': f'安装 PyCharm', 'href': None, 'active': True}
        ]
        context['version'] = version
        context['entry'] = entry or {}
        context['default_install_dir'] = default_install_dir(version)
        context['pycharm'] = get_pycharm_options()
        return context


class PipEnvMixin:
    """pip 页面公共基类：从 URL 的 id 参数解析 env_id，并注入宿主通用 pip API 地址。

    宿主 python 应用提供一套跨环境复用的通用 pip API（list/outdated/install/
    upgrade/uninstall + requirements 解析与批量导入安装），以 env_id 定位目标
    解释器；env_id→python_exe 的解析委托给 python_main.pip_envs 扩展点的贡献者
    （如 python_installer）。子类默认取 URL 的 id 作为 env_id，无需重复拼接 API 地址。
    """
    def get_env_id(self) -> str:
        return self.kwargs.get('id', '') or self.kwargs.get('env_id', '')

    def get_pip_api_context(self) -> dict:
        env_id = self.get_env_id()
        return {
            'env_id': env_id,
            'api_pip_list': reverse('python_main:api_pip_list', args=[env_id]),
            'api_pip_outdated': reverse('python_main:api_pip_outdated', args=[env_id]),
            'api_pip_install': reverse('python_main:api_pip_install', args=[env_id]),
            'api_pip_upgrade': reverse('python_main:api_pip_upgrade', args=[env_id]),
            'api_pip_uninstall': reverse('python_main:api_pip_uninstall', args=[env_id]),
            'api_pip_requirements_parse': reverse('python_main:api_pip_requirements_parse', args=[env_id]),
            'api_pip_requirements_install': reverse('python_main:api_pip_requirements_install', args=[env_id]),
            'api_pip_requirements_export': reverse('python_main:api_pip_requirements_export', args=[env_id]),
        }


class PipListViewMixin(PipEnvMixin, TemplateView):
    """pip 包管理页（列表）：子类覆写 url_install() 指向各自的完整安装页。"""
    template_name = 'python_main/pip_list.html'

    def url_install(self):
        return ''

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_pip_api_context())
        context['url_install'] = self.url_install()
        return context


class PipInstallViewMixin(PipEnvMixin, TemplateView):
    """安装新包页（完整页面，非弹窗）：含直接安装与 requirements.txt 三入口（上传/粘贴/
    本机选择）导入安装；子类覆写 url_list() 指向各自的包列表页。"""
    template_name = 'python_main/pip_install.html'

    def url_list(self):
        return ''

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_pip_api_context())
        context['url_list'] = self.url_list()
        return context


# ── 通用 pip 包管理 API（宿主提供，跨环境复用）──────────────
# env_id → python_exe 的解析委托给 python_main.pip_envs 扩展点：贡献者（如
# python_installer）注册 resolve_pip_env 服务，返回 {env_id, python_exe, label}。
# 任意新环境提供者只要 register_service('resolve_pip_env', ...) 即可复用同一套 pip API。
#
# 动态发现：通过 list_services() 扫所有注册了 resolve_pip_env 的应用，
# 无需硬编码贡献者名称。call_hooks 广播兜底兼容未知贡献者。


@lru_cache(maxsize=256)
def _resolve_pip_env(env_id: str):
    """动态发现 resolve_pip_env 贡献者，call_service 精确调用，call_hooks 广播兜底。

    返回 {env_id, python_exe, label} 或 None。lru_cache 保证进程内同一 env_id
    只解析一次（pip 相关 API 高频调用同一 env_id）。
    """
    env_id = (env_id or '').strip()
    if not env_id:
        return None

    from jiecore.app_services import list_services
    services = list_services()
    for provider, svcs in services.items():
        if 'resolve_pip_env' not in svcs:
            continue
        try:
            result = call_service(provider, 'resolve_pip_env', env_id)
        except (ServiceNotFoundError, ServiceCallError):
            continue
        if isinstance(result, dict) and result.get('env_id') == env_id and result.get('python_exe'):
            return result

    for r in call_hooks('python_main.pip_envs', context={'env_id': env_id}):
        if isinstance(r, dict) and r.get('env_id') == env_id and r.get('python_exe'):
            return r
    return None


def _pip_env_or_error(env_id):
    """解析环境；失败时返回 (None, JsonResponse)，成功时返回 (env, None)。"""
    env = _resolve_pip_env(env_id)
    if not env:
        return None, JsonResponse({'success': False,
                                   'message': '未找到指定的 Python 环境或解释器路径无效'})
    return env, None


@require_GET
def api_pip_list(request, env_id):
    """列出指定环境已安装的包（不检查可升级项，速度较快）。

    可升级信息需前端单独调用 api_pip_outdated 获取，避免每次打开包列表
    都联网查询 PyPI 导致加载缓慢。
    """
    env, err = _pip_env_or_error(env_id)
    if err:
        return err
    exe = env['python_exe']
    packages = pip_manager.list_packages(exe)
    return JsonResponse({
        'success': True,
        'packages': packages,
        'env_label': env.get('label', ''),
        'pip_version': pip_manager.get_pip_version(exe),
    })


@require_GET
def api_pip_outdated(request, env_id):
    """检查指定环境中可升级的包（联网查询 PyPI，按需调用）。

    返回 {name: latest_version} 字典，前端合并到包列表中展示升级标记。
    """
    env, err = _pip_env_or_error(env_id)
    if err:
        return err
    exe = env['python_exe']
    try:
        outdated = {o['name'].lower(): o.get('latest_version', '')
                    for o in pip_manager.list_outdated(exe)}
    except Exception as e:
        logger.warning('pip list --outdated 失败 (env_id=%s): %s', env_id, e)
        return JsonResponse({'success': False, 'message': f'检查可升级项失败: {e}'})
    return JsonResponse({'success': True, 'outdated': outdated})


@require_POST
def api_pip_install(request, env_id):
    """在指定环境后台安装新包（task + JieTaskLog 实时日志）。

    POST 参数：package=包名；version=可选，指定版本（组装为 name==version）。
    """
    env, err = _pip_env_or_error(env_id)
    if err:
        return err
    package = (request.POST.get('package') or '').strip()
    operator = (request.POST.get('operator') or '').strip()
    version = (request.POST.get('version') or '').strip()
    if not package:
        return JsonResponse({'success': False, 'message': '请填写要安装的包名'})
    if operator and version:
        spec = f'{package}{operator}{version}'
    else:
        spec = f'{package}'
    # spec = f'{package}=={version}' if version else package

    exe = env['python_exe']
    task = tasks.create_task('pip.install', meta={'env_id': env_id, 'spec': spec},
                             title=f'安装 pip 包 {spec}')
    tasks.append_log(task, f'安装任务已创建: {spec} -> {exe}')
    tasks.run_in_thread(task, _pip_install_worker, exe, spec)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


@require_POST
def api_pip_upgrade(request, env_id):
    """后台升级指定环境的包（task + JieTaskLog）。

    POST 参数：package=单个包名；或 packages=逗号分隔多个包名（一键全升）。
    """
    env, err = _pip_env_or_error(env_id)
    if err:
        return err
    exe = env['python_exe']
    raw = (request.POST.get('packages') or request.POST.get('package') or '').strip()
    names = [n.strip() for n in raw.split(',') if n.strip()]
    if not names:
        return JsonResponse({'success': False, 'message': '未指定要升级的包'})
    task = tasks.create_task('pip.upgrade', meta={'env_id': env_id, 'packages': names},
                             title=f'升级 pip 包 {", ".join(names)}')
    tasks.append_log(task, f'升级任务已创建: {", ".join(names)} -> {exe}')
    tasks.run_in_thread(task, _pip_upgrade_worker, exe, names)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


@require_POST
def api_pip_uninstall(request, env_id):
    """卸载指定环境的包（较快，直接同步执行）。POST 参数：package。"""
    env, err = _pip_env_or_error(env_id)
    if err:
        return err
    package = (request.POST.get('package') or '').strip()
    if not package:
        return JsonResponse({'success': False, 'message': '未指定要卸载的包'})
    result = pip_manager.uninstall_package(env['python_exe'], package)
    if result.get('success'):
        log_operation('python', f'卸载 pip 包 {package}', success=True,
                      detail=f'环境 {env_id}（{env["python_exe"]}）：{result.get("message", "")}')
    else:
        log_operation('python', f'卸载 pip 包 {package}', success=False,
                      detail=f'环境 {env_id}（{env["python_exe"]}）：{result.get("message", "")}')
    return JsonResponse(result)


def _pip_install_worker(task, exe: str, spec: str):
    """后台线程执行安装，把 pip 输出实时写入任务日志（供 JieTaskLog 轮询）。"""
    def on_output(line):
        tasks.append_log(task, line)
        tasks.set_progress(task, stage=line[:80])
    tasks.set_progress(task, progress=5, stage=f'开始安装 {spec}')
    res = pip_manager.install_package_stream(exe, spec, on_output)
    if res['success']:
        tasks.set_progress(task, progress=100, stage='安装完成')
        tasks.set_result(task, {'success': True, 'message': res['message']})
        log_operation('python', f'安装 pip 包 {spec}',
                      detail=f'目标环境 {exe}：{res.get("message", "")}')
    else:
        tasks.set_result(task, {'success': False, 'message': res['message']})
        log_operation('python', f'安装 pip 包 {spec}', success=False,
                      detail=f'目标环境 {exe}：{res.get("message", "")}')
        raise RuntimeError(res['message'])


def _pip_upgrade_worker(task, exe: str, names: list):
    """后台线程执行升级：单包走 install --upgrade，多包走批量升级。"""
    def on_output(line):
        tasks.append_log(task, line)
        tasks.set_progress(task, stage=line[:80])
    tasks.set_progress(task, progress=5, stage='开始升级')
    if len(names) == 1:
        res = pip_manager.install_package_stream(exe, names[0], on_output, upgrade=True)
    else:
        res = pip_manager.upgrade_packages_stream(exe, names, on_output)
    desc = ', '.join(names)
    if res['success']:
        tasks.set_progress(task, progress=100, stage='升级完成')
        tasks.set_result(task, {'success': True, 'message': res['message']})
        log_operation('python', f'升级 pip 包 {desc}',
                      detail=f'目标环境 {exe}：{res.get("message", "")}')
    else:
        tasks.set_result(task, {'success': False, 'message': res['message']})
        log_operation('python', f'升级 pip 包 {desc}', success=False,
                      detail=f'目标环境 {exe}：{res.get("message", "")}')
        raise RuntimeError(res['message'])


# ── requirements 导入安装（三入口：上传 / 粘贴 / 本机文件）──────────────

# source → 用户可见来源标识（任务日志头部与预览徽标共用一套文案）
_REQ_SOURCE_LABELS = {'upload': '上传文件', 'paste': '粘贴文本', 'path': '本机文件'}


def _get_requirements_input(request):
    """提取并校验 requirements 输入来源（解析 / 安装两个 API 共用）。

    返回 (source, content, err_response)：
      - 校验通过：source='upload'|'paste' 时 content=文本（已剥 BOM）；
        source='path' 时 content=本机文件路径（由调用方继续五项复检）；
      - 校验失败：返回 (None, None, JsonResponse{success: False, message})。
    校验项：source 合法、content/path 互斥（spec 7.3.1）、content 字节数
    ≤ 1MB、content 无 NUL 控制字符（二进制特征兜底）、剥 BOM 前缀。
    """
    source = (request.POST.get('source') or '').strip()
    content = request.POST.get('content') or ''
    path = (request.POST.get('path') or '').strip()
    err = JsonResponse({'success': False,
                        'message': '请求来源不明确：仅支持单一输入来源（上传 / 粘贴 / 本机文件）'})
    if source not in _REQ_SOURCE_LABELS:
        return None, None, err
    has_content = bool(content.strip())
    has_path = bool(path)
    if source == 'path':
        # path 来源：路径必填，且不得同时携带文本内容
        if not has_path or has_content:
            return None, None, err
        return 'path', path, None
    # upload / paste 来源：文本必填，且不得同时携带路径
    if not has_content or has_path:
        return None, None, err
    if len(content.encode('utf-8')) > requirements_parser.MAX_FILE_BYTES:
        return None, None, JsonResponse({'success': False, 'message': '内容超过 1MB 上限'})
    if '\x00' in content:
        return None, None, JsonResponse({'success': False, 'message': '仅支持 UTF-8 文本文件'})
    return source, content.lstrip('\ufeff'), None


def _parse_requirements_for_request(request):
    """解析 / 安装两个 API 的共用前置链：来源校验 → path 五项复检 → 三分法解析。

    返回 (source, result, err_response)：全部通过时 result 为
    parse_requirements_text 的返回结构；任一步失败返回
    (None, None, JsonResponse)（文案见 design 2.2.2 异常映射）。
    """
    source, content, err = _get_requirements_input(request)
    if err:
        return None, None, err
    if source == 'path':
        read = requirements_parser.read_requirements_file(content)
        if not read['success']:
            return None, None, JsonResponse({'success': False, 'message': read['message']})
        content = read['content']
    result = requirements_parser.parse_requirements_text(content)
    if result['overflow']:
        return None, None, JsonResponse({
            'success': False,
            'message': f'有效行数超过 {requirements_parser.MAX_TOTAL_LINES} 条上限'
                       f'（当前 {result["total_lines"]} 行）'})
    if result['counts']['valid'] == 0:
        summary = requirements_parser.summarize_ignored(result['counts'], result['ignored'])
        return None, None, JsonResponse({'success': False,
                                         'message': f'未发现可安装的依赖：{summary}'})
    return source, result, None


@require_POST
def api_pip_requirements_parse(request, env_id):
    """解析 requirements 输入并返回预览摘要（只读，不创建任务）。

    POST 参数：
        source  - 输入来源，'upload' | 'paste' | 'path'（必填，三选一）
        content - 文本内容（source=upload/paste 时必填，source=path 时必须为空）
        path    - 本机文件完整路径（source=path 时必填，其余来源必须为空）

    三入口解析结果逐字节等价（两条通道在 parse_requirements_text 汇合）。
    响应仅返回有效条目与计数 {success, source, valid, counts}：
    预览确认区只展示有效条目，忽略 / 无法识别明细仅记录于安装任务日志。
    """
    env, err = _pip_env_or_error(env_id)
    if err:
        return err
    source, result, err = _parse_requirements_for_request(request)
    if err:
        return err
    return JsonResponse({
        'success': True,
        'source': source,
        'valid': result['valid'],
        'counts': result['counts'],
    })


@require_POST
def api_pip_requirements_install(request, env_id):
    """创建 requirements 批量安装后台任务（单任务、单次 pip 会话）。

    POST 参数：与解析接口完全同构（source / content / path）。
    服务端对原始来源重新执行复检 + 解析，不信任前端预览结果。
    """
    env, err = _pip_env_or_error(env_id)
    if err:
        return err
    source, result, err = _parse_requirements_for_request(request)
    if err:
        return err
    specs = result['valid']
    count = len(specs)
    task = tasks.create_task('pip.install_requirements',
                             meta={'env_id': env_id, 'count': count, 'source': source},
                             title=f'requirements 安装（{count} 个包）')
    tasks.append_log(task, f'requirements 安装任务已创建: {count} 个包 -> {env["python_exe"]}')
    tasks.run_in_thread(task, _pip_requirements_install_worker, env['python_exe'],
                        specs, result, _REQ_SOURCE_LABELS[source])
    return JsonResponse({'success': True, 'task_id': task['task_id']})


@require_GET
def api_pip_requirements_export(request, env_id):
    """导出指定环境的 requirements.txt（pip freeze），直接返回文件下载。

    作为通用 pip API 的一员，installer / archive 等贡献者应用无需自己实现导出，
    只要各自 PipListViewMixin 页面注入的 api_pip_requirements_export URL 指向这里即可。
    """
    env, err = _pip_env_or_error(env_id)
    if err:
        return err
    res = pip_manager.export_requirements(env['python_exe'])
    if not res['success']:
        return JsonResponse({'success': False, 'message': res['message']})
    content = res.get('content', '')
    fname = f'requirements-{env_id}.txt'
    resp = HttpResponse(content, content_type='text/plain; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


def _append_req_detail(head, items, limit, format_line):
    """输出 requirements 明细段（≤ limit 行），超出部分合并为分类计数一行。

    明细段封顶（LOG_DETAIL_LIMIT）：jiecore 任务日志 2000 行从头截断，
    各段封顶后为 pip 实时输出预留行数（design R4 截断缓解）。
    """
    for item in items[:limit]:
        head(format_line(item))
    rest = items[limit:]
    if not rest:
        return
    stats = {}
    for item in rest:
        key = (item.get('reason') or '无法识别').split('（')[0]
        stats[key] = stats.get(key, 0) + 1
    parts = '、'.join(f'{n} 行{k}' for k, n in sorted(stats.items(), key=lambda kv: -kv[1]))
    head(f'...其余 {len(rest)} 条略（{parts}）')


def _pip_requirements_install_worker(task, exe: str, specs: list, parse_result: dict,
                                     source_label: str):
    """requirements 批量安装后台线程：头部摘要 → 单次 pip 会话 → 成败判定。

    头部摘要顺序固定（REQ-NEW-008）：来源标识 → 目标环境 → 解析摘要 →
    有效条目清单 → 忽略明细 → 无法识别明细 → 开始安装；各明细段以
    LOG_DETAIL_LIMIT 封顶（jiecore 任务日志 2000 行从头截断的缓解）。
    """
    counts = parse_result['counts']
    ignored = parse_result['ignored']
    unrecognized = parse_result['unrecognized']
    limit = requirements_parser.LOG_DETAIL_LIMIT

    def head(line):
        tasks.append_log(task, line)

    tasks.set_progress(task, progress=5, stage='写入解析摘要')
    head(f'requirements 安装任务（来源：{source_label}）')
    head(f'目标环境: {exe}')
    head(f'解析摘要: 有效 {counts["valid"]} 条 / 忽略 {counts["ignored"]} 条'
         f' / 无法识别 {counts["unrecognized"]} 条')
    head('── 有效条目清单 ──')
    for spec in specs[:limit]:
        head(spec)
    if len(specs) > limit:
        head(f'...其余 {len(specs) - limit} 条略（完整清单见解析预览）')
    head('── 忽略条目明细 ──')
    _append_req_detail(head, ignored, limit,
                       lambda item: f'L{item["line"]}: {item["text"]}（{item["reason"]}）')
    head('── 无法识别行明细 ──')
    _append_req_detail(head, unrecognized, limit,
                       lambda item: f'L{item["line"]}: {item["text"]}')
    tasks.set_progress(task, progress=10, stage=f'开始安装 {len(specs)} 个包')
    head(f'开始单次 pip 会话安装 {len(specs)} 个包...')

    def on_output(line):
        tasks.append_log(task, line)
        tasks.set_progress(task, stage=line[:80])

    res = pip_manager.install_requirements_stream(
        exe, specs, on_output, timeout=requirements_parser.PIP_INSTALL_TIMEOUT)
    desc = f'requirements 安装（{len(specs)} 个包）'
    if res['success']:
        tasks.set_progress(task, progress=100, stage='安装完成')
        tasks.set_result(task, {'success': True, 'message': res['message']})
        log_operation('python', desc,
                      detail=f'目标环境 {exe}（来源：{source_label}，{len(specs)} 个包）：{res.get("message", "")}')
    else:
        tasks.set_result(task, {'success': False, 'message': res['message']})
        log_operation('python', desc, success=False,
                      detail=f'目标环境 {exe}（来源：{source_label}，{len(specs)} 个包）：{res.get("message", "")}')
        raise RuntimeError(res['message'])


# 卸载程序是常驻 GUI 向导，用 cmd /c start 转手拉起后 cmd 会立即退出，这里的
# timeout 只是等待 cmd 退出的上限，卸载向导本身不受影响（详见 _run_uninstall_pycharm）。
_PYCHARM_UNINSTALL_TIMEOUT = 15

# 卸载向导由用户逐步点击完成，cmd 秒退只代表“已拉起”而非“已卸载”。拉起后需轮询
# 注册表判断是否真正卸载完毕：只有检测不到 PyCharm 安装项时才置任务成功，前端
# JieTaskLog 也才显示“确定”按钮供跳转（否则跳回开发工具页时 PyCharm 仍在、看似没卸载）。
_PYCHARM_UNINSTALL_WAIT_INTERVAL = 2      # 轮询卸载完成的间隔（秒）
_PYCHARM_UNINSTALL_WAIT_TIMEOUT = 900     # 等待用户在向导中完成卸载的上限（秒）


def _split_uninstall_command(cmd: str):
    """把注册表 UninstallString 拆成 (可执行文件, 参数)。

    兼容带引号（"C:\\...\\Uninstall.exe" /currentuser）与不带引号两种写法；
    无法可靠拆分时按第一个空白切分。
    """
    cmd = (cmd or '').strip()
    if not cmd:
        return '', ''
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end != -1:
            return cmd[1:end], cmd[end + 1:].strip()
        return cmd.strip('"'), ''
    idx = cmd.lower().find('.exe')
    if idx != -1:
        cut = idx + len('.exe')
        return cmd[:cut], cmd[cut:].strip()
    parts = cmd.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else '')


@require_POST
def api_pycharm_uninstall(request):
    """以任务方式提权启动本机 PyCharm 卸载程序（后台执行 + 逐步日志，前端 JieTaskLog 轮询）。

    复用 jiecore 公共能力：任务登记在 jiecore.tasks，提权由 jiecore.system_ops.
    run_elevated 完成（ShellExecuteEx → PowerShell → schtasks 三级后备）；前端拿到
    task_id 后走 /jiecore/api/task-status/<task_id>/ 通用轮询接口展示日志弹窗。
    """
    pycharm = get_pycharm_options()
    if not pycharm:
        return JsonResponse({'success': False, 'message': '未检测到已安装的 PyCharm'})
    uninstall_cmd = (pycharm.get('UninstallString') or '').strip()
    if not uninstall_cmd:
        return JsonResponse({'success': False,
                             'message': 'PyCharm 安装信息缺少卸载命令（UninstallString）'})
    exe, args = _split_uninstall_command(uninstall_cmd)
    if not exe or not os.path.exists(exe):
        return JsonResponse({'success': False,
                             'message': f'卸载程序不存在: {exe or uninstall_cmd}'})
    task = tasks.create_task('uninstall_pycharm', meta={'exe': exe},
                             title=f'卸载 {pycharm.get("DisplayName", "PyCharm")}')
    tasks.append_log(task, f'卸载任务已创建: {exe}')
    tasks.run_in_thread(task, _run_uninstall_pycharm, exe, args, pycharm.get('DisplayName', ''))
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_uninstall_pycharm(task: dict, exe_path: str, args: str, display_name: str):
    """后台线程提权启动 PyCharm 卸载向导，通过 on_step 回报逐步进度。

    卸载程序是需要用户逐步确认的 GUI 向导，直接提权并阻塞等待会触发 run_elevated
    的超时降级链路。借助 cmd.exe /c start 让 cmd 拉起卸载向导后立即退出（退出码 0），
    卸载向导则在独立窗口运行并继承管理员令牌；这里的 timeout 只是等待 cmd 退出的上限。
    """
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'正在以管理员权限启动 {display_name or "PyCharm"} 卸载程序...')
    on_step('若弹出 UAC 对话框，请点击“是”授权。')
    on_step('卸载向导弹出后，请按提示完成卸载。')
    # start 的第一个引号参数是窗口标题，必须留空占位；show=0 隐藏 cmd 控制台窗口，
    # 卸载向导由 start 以独立进程拉起、正常显示并继承管理员令牌。
    params = (args + ' ').strip() if args else ''
    command = f'/c start "" "{exe_path}" {params}'
    try:
        code = run_elevated('cmd.exe', command, _PYCHARM_UNINSTALL_TIMEOUT, show=0)
    except PermissionError as e:
        # 用户取消 UAC，或三级链路均无提权能力：置结果后抛出，由 run_in_thread 统一记为失败
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except TimeoutError:
        # 正常情况下 start 会让 cmd 秒退、不会走到这里；兜底视为卸载程序已拉起，
        # 同样进入“等待卸载完成”轮询，不因拉起耗时而提前误报成功
        on_step('卸载程序已拉起，请在弹出的卸载向导中完成操作。')
        _wait_until_uninstalled(task, on_step)
        return
    except OSError as e:
        # 提权链路自身失败（ShellExecuteEx 被拒 / PowerShell 报错 / schtasks 拒绝访问）。
        # 必须先把可读的失败原因写进 result 再抛出，否则前端 onFail 只能拿到空对象
        task['result'] = {'success': False, 'message': f'提权启动卸载程序失败: {e}'}
        raise

    if code != 0:
        task['result'] = {'success': False, 'message': f'卸载程序启动异常，退出码 {code}'}
        raise RuntimeError(f'卸载程序启动异常，退出码 {code}')

    # 卸载向导已拉起（cmd 秒退）：不立即置成功，改为轮询注册表直到 PyCharm 安装项消失，
    # 检测不到才视为真正卸载完成；等待期间任务保持 running，前端 JieTaskLog 不显示“确定”按钮
    on_step('卸载程序已启动，请在弹出的卸载向导中按提示完成卸载。')
    _wait_until_uninstalled(task, on_step)


def _wait_until_uninstalled(task: dict, on_step):
    """轮询注册表等待 PyCharm 真正卸载完成，完成后才置任务成功。

    卸载向导由用户逐步点击确认，拉起（cmd 秒退）并不代表卸载结束。这里每隔
    _PYCHARM_UNINSTALL_WAIT_INTERVAL 秒检测一次注册表安装项：检测不到即视为卸载完成
    并置成功；超过 _PYCHARM_UNINSTALL_WAIT_TIMEOUT 仍存在则记为失败并提示用户手动确认。
    等待期间任务保持 running 状态，前端 JieTaskLog 不显示“确定”按钮，直到成功回调触发
    才出现按钮供跳转，避免用户提前跳转后看到 PyCharm 仍未卸载。
    """
    waited = 0
    next_hint = 30
    while waited < _PYCHARM_UNINSTALL_WAIT_TIMEOUT:
        time.sleep(_PYCHARM_UNINSTALL_WAIT_INTERVAL)
        waited += _PYCHARM_UNINSTALL_WAIT_INTERVAL
        if not pycharm_detect_installed():
            on_step('检测到 PyCharm 已从系统中移除，卸载完成。')
            tasks.set_progress(task, progress=100, stage='卸载完成')
            task['result'] = {'success': True, 'message': 'PyCharm 卸载完成'}
            log_operation('python', '卸载 PyCharm', detail=f'等待 {waited} 秒后检测到已卸载')
            return
        # 每隔一段时间提示一次，避免用户以为弹窗卡住
        if waited >= next_hint:
            next_hint += 30
            on_step(f'仍在等待卸载向导完成...（已等待 {waited} 秒）')
            tasks.set_progress(task, stage=f'等待卸载向导完成（{waited}s）')
    # 超时仍未检测到卸载完成（用户可能取消了向导）
    on_step('等待超时：仍未检测到 PyCharm 卸载完成，请确认是否已在卸载向导中完成操作。')
    tasks.set_progress(task, progress=100, stage='等待卸载完成超时')
    task['result'] = {'success': False, 'message': '等待卸载完成超时，请手动确认 PyCharm 是否已卸载'}
    raise RuntimeError('等待 PyCharm 卸载完成超时')


@require_POST
def api_pycharm_install(request):
    """下载并静默安装指定版本 PyCharm（后台任务 + 逐步日志，前端 JieTaskLog 轮询）。

    流程：先把安装包下载到 tmp 下载目录，再以管理员权限（UAC）执行
    installer /S /CONFIG=<silent.config> /D=<安装目录> 静默安装。任务登记在
    jiecore.tasks，提权由 jiecore.system_ops.run_elevated 完成（ShellExecuteEx →
    PowerShell → schtasks 三级后备）；前端拿到 task_id 后走
    /jiecore/api/task-status/<task_id>/ 通用轮询接口展示安装日志弹窗。
    """
    version = (request.POST.get('version') or '').strip()
    download_url = (request.POST.get('download_url') or '').strip()
    install_dir = (request.POST.get('install_dir') or '').strip()

    if not version:
        return JsonResponse({'success': False, 'message': '未指定要安装的 PyCharm 版本'})
    if not install_dir:
        return JsonResponse({'success': False, 'message': '请指定安装目录'})

    filename = download_url.rstrip('/').split('/')[-1] or f'pycharm-{version}.exe'
    task = tasks.create_task('install_pycharm',
                             meta={'version': version, 'install_dir': install_dir},
                             title=f'安装 PyCharm {version}')
    tasks.append_log(task, f'安装任务已创建: PyCharm {version} -> {install_dir}')
    tasks.run_in_thread(task, _run_install_pycharm, version, download_url,
                        install_dir, filename)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_install_pycharm(task: dict, version: str, download_url: str,
                         install_dir: str, filename: str):
    """后台线程：先下载安装包到 tmp，再提权静默安装到指定目录。

    下载阶段无需提权，直接写本地 tmp；安装阶段通过 run_elevated 弹 UAC 提权，
    以 installer /S /CONFIG /D 静默安装。任一阶段抛异常由 run_in_thread 统一记为失败。
    """
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    # 1. 下载安装包到 tmp
    on_step(f'开始下载 PyCharm {version} 安装包...')
    tasks.set_progress(task, progress=5)
    try:
        exe_path = download_pycharm(download_url, filename, on_progress=on_step)
    except Exception as e:
        task['result'] = {'success': False, 'message': f'下载 PyCharm 安装包失败: {e}'}
        raise
    tasks.set_progress(task, progress=50, stage='下载完成，准备安装')

    # 2. 提权静默安装
    try:
        code = run_silent_install(exe_path, install_dir, on_progress=on_step)
    except PermissionError as e:
        # 用户取消 UAC，或三级链路均无提权能力
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except PythonPycharmError as e:
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except TimeoutError:
        task['result'] = {'success': False, 'message': 'PyCharm 安装超时，请检查系统状态后重试'}
        raise
    except OSError as e:
        task['result'] = {'success': False, 'message': f'提权静默安装失败: {e}'}
        raise

    if code == 0:
        on_step('PyCharm 安装完成。')
        tasks.set_progress(task, progress=100, stage='安装成功')
        # 刷新本机已安装 PyCharm 信息，便于详情页立即展示
        try:
            get_pycharm_options(refresh=True)
        except Exception as e:
            logger.warning('安装后刷新 PyCharm 信息失败: %s', e)
        task['result'] = {'success': True, 'message': f'PyCharm {version} 安装成功',
                          'install_dir': install_dir}
        log_operation('python', f'安装 PyCharm {version}',
                      detail=f'安装目录 {install_dir}')
    else:
        task['result'] = {'success': False, 'message': f'PyCharm 安装异常，退出码 {code}'}
        log_operation('python', f'安装 PyCharm {version}', success=False,
                      detail=f'安装目录 {install_dir}，退出码 {code}')
        raise RuntimeError(f'PyCharm 安装异常，退出码 {code}')


@require_GET
def api_pycharm_info_refresh(request):
    try:
        get_pycharm_options(refresh=True)
        return JsonResponse({'code': 0, 'msg': '刷新成功'})
    except Exception as e:
        logger.warning('刷新本机 PyCharm 信息失败: %s', e)
        return JsonResponse({'code': 1, 'msg': f'刷新失败：{e}'})


@require_POST
def api_fetch_pycharm_version(request):
    try:
        get_pycharm_versions(refresh=True, limit_ver=LIMIT_VERSION)
        return JsonResponse({'code': 0, 'msg': '抓取成功'})
    except Exception as e:
        return JsonResponse({'code': 1, 'msg': f'抓取失败：{e}'})


# 用 cmd.exe /c start 转手拉起 PyCharm：PyCharm 是常驻 GUI 进程，若直接提权启动它
# 并阻塞等待，run_elevated 必然等到超时。借助 start 让 cmd.exe 拉起 PyCharm 后立即
# 退出（退出码 0），PyCharm 则继承管理员令牌在独立窗口运行；这里的 timeout 只是
# 等待 cmd.exe 退出的上限，PyCharm 本身不受影响。
_PYCHARM_LAUNCH_TIMEOUT = 10


def _find_pycharm_exe(install_dir: str):
    """在安装目录的 bin 下定位 PyCharm 可执行文件，优先 64 位。找不到返回 None。"""
    base = pathlib.Path(install_dir)
    for name in ('pycharm64.exe', 'pycharm.exe'):
        candidate = base / 'bin' / name
        if candidate.exists():
            return candidate
    return None


@require_POST
def api_start_pycharm(request):
    """以任务方式提权启动本机 PyCharm（后台执行 + 逐步日志，前端 JieTaskLog 轮询）。

    复用 jiecore 公共能力：任务登记在 jiecore.tasks，提权由 jiecore.system_ops.
    run_elevated 完成（ShellExecuteEx → PowerShell → schtasks 三级后备）；前端拿到
    task_id 后走 /jiecore/api/task-status/<task_id>/ 通用轮询接口展示日志弹窗。
    """
    pycharm = get_pycharm_options()
    if not pycharm:
        return JsonResponse({'success': False, 'message': '未检测到已安装的 PyCharm'})
    install_dir = pycharm.get('InstallLocation', '')
    if not install_dir:
        return JsonResponse({'success': False,
                             'message': 'PyCharm 安装信息缺少安装目录（InstallLocation）'})
    exe = _find_pycharm_exe(install_dir)
    if not exe:
        return JsonResponse({'success': False,
                             'message': f'未在 {install_dir}\\bin 下找到 pycharm64.exe 或 pycharm.exe'})
    task = tasks.create_task('start_pycharm', meta={'exe': str(exe)},
                             title=f'启动 {pycharm.get("DisplayName", "PyCharm")}')
    tasks.append_log(task, f'启动任务已创建: {exe}')
    tasks.run_in_thread(task, _run_start_pycharm, str(exe), pycharm.get('DisplayName', ''))
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_start_pycharm(task: dict, exe_path: str, display_name: str):
    """后台线程提权启动 PyCharm，通过 on_step 回报逐步进度。

    经 cmd.exe /c start 转手拉起，使提权进程本身秒退，避免因等待常驻的 PyCharm
    而触发 run_elevated 的超时降级链路（详见 _PYCHARM_LAUNCH_TIMEOUT 处注释）。
    """
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'正在以管理员权限启动 {display_name or "PyCharm"}...')
    on_step('若弹出 UAC 对话框，请点击“是”授权。')
    # start 的第一个引号参数是窗口标题，必须留空占位；否则带引号的可执行路径会被
    # 当成标题，cmd 转而启动一个交互式 shell。show=0 隐藏 cmd 控制台窗口，PyCharm
    # 由 start 以独立进程拉起、正常显示主窗口并继承管理员令牌。
    command = f'/c start "" "{exe_path}"'
    try:
        code = run_elevated('cmd.exe', command, _PYCHARM_LAUNCH_TIMEOUT, show=0)
    except PermissionError as e:
        # 用户取消 UAC，或三级链路均无提权能力：置结果后抛出，由 run_in_thread 统一记为失败
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except TimeoutError:
        # 正常情况下 start 会让 cmd 秒退、不会走到这里；兜底视为命令已下发
        on_step('PyCharm 启动命令已下发。')
        tasks.set_progress(task, progress=100, stage='启动成功')
        task['result'] = {'success': True, 'message': 'PyCharm 已启动'}
        return
    except OSError as e:
        # 提权链路自身失败（ShellExecuteEx 被拒 / PowerShell 报错 / schtasks 拒绝访问）。
        # PermissionError、TimeoutError 已在上面两个分支拦截，这里只剩一般 OSError。
        # 必须先把可读的失败原因写进 result 再抛出，否则前端 onFail 只能拿到空对象
        task['result'] = {'success': False, 'message': f'提权启动失败: {e}'}
        raise

    if code == 0:
        on_step('PyCharm 已启动。')
        tasks.set_progress(task, progress=100, stage='启动成功')
        task['result'] = {'success': True, 'message': 'PyCharm 已启动'}
    else:
        task['result'] = {'success': False, 'message': f'PyCharm 启动异常，退出码 {code}'}
        raise RuntimeError(f'PyCharm 启动异常，退出码 {code}')


def api_pycharm_reset(request):
    pycharm = get_pycharm_options()
    if pycharm:
        localapp_dir = os.path.join(os.environ.get('LOCALAPPDATA'), 'JetBrains', pycharm['dataDirectoryName'])
        appdata_dir = os.path.join(os.environ.get('APPDATA'), 'JetBrains', pycharm['dataDirectoryName'])
        try:
            if os.path.exists(localapp_dir):
                shutil.rmtree(localapp_dir)
            if os.path.exists(appdata_dir):
                shutil.rmtree(appdata_dir)
        except Exception as e:
            logger.warning(f'重置失败。错误信息：{e}')
            return JsonResponse({'code': 500, 'msg': '重置失败:{e}'})
    else:
        logger.warning(f'没有找到pycharm安装信息！')
        return JsonResponse({'code': 500, 'msg':'找不到Pycharm安装信息！'})
    return JsonResponse({'code':0, 'msg':'Pycharm配置已经重置！</br>请您重启Pycharm!'})


# ══════════ VSCode（Visual Studio Code）══════════
# 与 PyCharm 不同：VSCode 不区分历史版本，统一取官方最新稳定版；安装仅区分
# User Installer / System Installer（仅 x64）。启动/详情/重置/卸载沿用 PyCharm 的
# 任务 + JieTaskLog 日志窗模式；卸载为交互式向导，并支持卸载后清理用户配置。

class VscodeInstallView(PythonMixin, TemplateView):
    """开发工具：安装 VSCode（无版本选择，取最新稳定版）"""
    template_name = 'python_main/vscode_install.html'

    def get(self, request, *args, **kwargs):
        # VSCode 不提供重装：已安装时直接跳详情页，杠掉经直接访问安装页 URL 覆盖安装
        if get_vscode_options():
            return redirect('python_main:vscode_detail')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '安装 VSCode'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': '开发工具', 'href': reverse('python_main:dev_tools'), 'active': False},
            {'title': '安装 VSCode', 'href': None, 'active': True},
        ]
        try:
            context['latest'] = get_vscode_latest_all(refresh=True)
            context['latest_error'] = ''
        except Exception as e:
            context['latest'] = {}
            context['latest_error'] = str(e)
        context['installer_labels'] = VSCODE_INSTALLER_LABELS
        context['default_install_dir'] = vscode_default_install_dir()
        context['vscode'] = get_vscode_options()
        return context


class VscodeDetailView(PythonMixin, TemplateView):
    """开发工具：VSCode 详细信息"""
    template_name = 'python_main/vscode_detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'VSCode - 详细信息'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': '开发工具', 'href': reverse('python_main:dev_tools'), 'active': False},
            {'title': 'VSCode - 详细信息', 'href': None, 'active': True},
        ]
        context['vscode'] = get_vscode_options()
        return context


class VscodeUninstallView(PythonMixin, TemplateView):
    """开发工具：卸载 VSCode（交互式向导 + 可选清理配置）"""
    template_name = 'python_main/vscode_uninstall.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '卸载 VSCode'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': '开发工具', 'href': reverse('python_main:dev_tools'), 'active': False},
            {'title': '卸载 VSCode', 'href': None, 'active': True},
        ]
        context['vscode'] = get_vscode_options()
        return context


@require_POST
def api_vscode_install(request):
    """下载并静默安装最新版 VSCode（后台任务 + 逐步日志，前端 JieTaskLog 轮询）。

    参数：installer_type=user|system、install_dir=安装目录。下载地址由后端按
    installer_type 从官方 update API（缓存）取得，不信任前端传入的 URL。
    提权按需：System Installer 一律提权，User Installer 仅目录不可写时提权。
    不支持重装：已检测到安装时直接拒绝，需先卸载再安装。
    """
    if get_vscode_options():
        return JsonResponse({'success': False,
                             'message': 'VSCode 已安装，不支持重装；如需重装请先卸载后再安装'})
    installer_type = (request.POST.get('installer_type') or 'user').strip()
    install_dir = (request.POST.get('install_dir') or '').strip()
    if installer_type not in ('user', 'system'):
        return JsonResponse({'success': False, 'message': f'未知的 installer 类型: {installer_type}'})
    if not install_dir:
        return JsonResponse({'success': False, 'message': '请指定安装目录'})
    try:
        latest = get_vscode_latest(installer_type, refresh=False)
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'获取 VSCode 最新版失败: {e}'})
    download_url = latest.get('url', '')
    filename = latest.get('filename', '')
    version = latest.get('productVersion', '')
    if not download_url:
        return JsonResponse({'success': False, 'message': '没有可用的下载地址，请稍后重试'})
    task = tasks.create_task('install_vscode',
                             meta={'installer_type': installer_type,
                                   'install_dir': install_dir, 'version': version},
                             title=f'安装 VSCode {version}')
    tasks.append_log(task, f'安装任务已创建: VSCode {version}（{installer_type}）-> {install_dir}')
    tasks.run_in_thread(task, _run_install_vscode, installer_type, download_url,
                        install_dir, filename, version)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_install_vscode(task: dict, installer_type: str, download_url: str,
                        install_dir: str, filename: str, version: str):
    """后台线程：先下载安装包到 tmp，再按需提权静默安装到指定目录。"""
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'开始下载 VSCode {version}（{installer_type}）安装包...')
    tasks.set_progress(task, progress=5)
    try:
        exe_path = download_vscode(download_url, filename, on_progress=on_step)
    except Exception as e:
        task['result'] = {'success': False, 'message': f'下载 VSCode 安装包失败: {e}'}
        raise
    tasks.set_progress(task, progress=50, stage='下载完成，准备安装')

    try:
        code = vscode_run_silent_install(exe_path, install_dir, installer_type,
                                         on_progress=on_step)
    except PermissionError as e:
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except PythonVscodeError as e:
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except TimeoutError:
        task['result'] = {'success': False, 'message': 'VSCode 安装超时，请检查系统状态后重试'}
        raise
    except OSError as e:
        task['result'] = {'success': False, 'message': f'静默安装失败: {e}'}
        raise

    if code == 0:
        on_step('VSCode 安装完成。')
        tasks.set_progress(task, progress=100, stage='安装成功')
        try:
            get_vscode_options(refresh=True)
        except Exception as e:
            logger.warning('安装后刷新 VSCode 信息失败: %s', e)
        task['result'] = {'success': True, 'message': f'VSCode {version} 安装成功',
                          'install_dir': install_dir}
        log_operation('python', f'安装 VSCode {version}',
                      detail=f'安装目录 {install_dir}')
    else:
        task['result'] = {'success': False, 'message': f'VSCode 安装异常，退出码 {code}'}
        log_operation('python', f'安装 VSCode {version}', success=False,
                      detail=f'安装目录 {install_dir}，退出码 {code}')
        raise RuntimeError(f'VSCode 安装异常，退出码 {code}')


# VSCode 是常驻 GUI 进程，用 cmd /c start 转手拉起后 cmd 立即退出，这里的 timeout
# 只是等待 cmd 退出的上限。与 PyCharm 不同：VSCode 是用户态编辑器，不提权启动
# （提权会导致拖放失效、新建文件属主变管理员、扩展行为异常）。
_VSCODE_LAUNCH_TIMEOUT = 10


def _run_cmd_start(command: str, timeout: int) -> int:
    """非提权运行 cmd.exe 命令（拉起 GUI 后立即返回），返回退出码。

    绝不能用 capture_output=True：那会给 cmd.exe 建 stdout/stderr 管道，而经
    `start` 拉起的 GUI 进程（Code.exe / 卸载向导）会继承这些管道写端。cmd.exe 虽
    秒退，但 subprocess.run 内部的 communicate() 要读到管道 EOF 才返回，而被继承的
    写端直到 GUI 进程退出才关闭——于是启动任务会一直阻塞到用户关掉 VSCode 才“成功”。
    改用 DEVNULL：没有管道可读，run() 只等待直接子进程 cmd.exe 退出（start 后立即退出）。
    """
    try:
        proc = subprocess.run(f'cmd.exe {command}', timeout=timeout,
                              creationflags=_CREATE_NO_WINDOW,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f'命令执行超时（{timeout} 秒）')
    return proc.returncode


@require_POST
def api_start_vscode(request):
    """以任务方式启动本机 VSCode（后台执行 + 逐步日志，前端 JieTaskLog 轮询）。"""
    vscode = get_vscode_options()
    if not vscode:
        return JsonResponse({'success': False, 'message': '未检测到已安装的 VSCode'})
    install_dir = vscode.get('InstallLocation', '')
    if not install_dir:
        return JsonResponse({'success': False,
                             'message': 'VSCode 安装信息缺少安装目录（InstallLocation）'})
    exe = find_vscode_exe(install_dir)
    if not exe:
        return JsonResponse({'success': False,
                             'message': f'未在 {install_dir} 下找到 Code.exe'})
    task = tasks.create_task('start_vscode', meta={'exe': str(exe)},
                             title=f'启动 {vscode.get("DisplayName", "VSCode")}')
    tasks.append_log(task, f'启动任务已创建: {exe}')
    tasks.run_in_thread(task, _run_start_vscode, str(exe), vscode.get('DisplayName', ''))
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_start_vscode(task: dict, exe_path: str, display_name: str):
    """后台线程启动 VSCode（非提权，经 cmd /c start 转手拉起）。"""
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'正在启动 {display_name or "VSCode"}...')
    command = f'/c start "" "{exe_path}"'
    try:
        code = _run_cmd_start(command, _VSCODE_LAUNCH_TIMEOUT)
    except TimeoutError:
        on_step('VSCode 启动命令已下发。')
        tasks.set_progress(task, progress=100, stage='启动成功')
        task['result'] = {'success': True, 'message': 'VSCode 已启动'}
        return
    except OSError as e:
        task['result'] = {'success': False, 'message': f'启动失败: {e}'}
        raise
    if code == 0:
        on_step('VSCode 已启动。')
        tasks.set_progress(task, progress=100, stage='启动成功')
        task['result'] = {'success': True, 'message': 'VSCode 已启动'}
    else:
        task['result'] = {'success': False, 'message': f'VSCode 启动异常，退出码 {code}'}
        raise RuntimeError(f'VSCode 启动异常，退出码 {code}')


# 卸载向导是常驻 GUI，用 cmd /c start 拉起后轮询注册表判断是否真正卸载完毕。
_VSCODE_UNINSTALL_TIMEOUT = 15
_VSCODE_UNINSTALL_WAIT_INTERVAL = 2
_VSCODE_UNINSTALL_WAIT_TIMEOUT = 900


@require_POST
def api_vscode_uninstall(request):
    """以任务方式启动 VSCode 卸载向导（交互式），可选卸载后清理用户配置。

    参数：cleanup=1 时，卸载成功后执行与“重置”相同的清理（删 %APPDATA%\\Code
    与 %USERPROFILE%\\.vscode）。System Installer 提权拉起卸载器，User 不提权。
    """
    vscode = get_vscode_options()
    if not vscode:
        return JsonResponse({'success': False, 'message': '未检测到已安装的 VSCode'})
    uninstall_cmd = (vscode.get('UninstallString') or '').strip()
    if not uninstall_cmd:
        return JsonResponse({'success': False,
                             'message': 'VSCode 安装信息缺少卸载命令（UninstallString）'})
    exe, args = _split_uninstall_command(uninstall_cmd)
    if not exe or not os.path.exists(exe):
        return JsonResponse({'success': False,
                             'message': f'卸载程序不存在: {exe or uninstall_cmd}'})
    cleanup = (request.POST.get('cleanup') or '').strip() in ('1', 'true', 'True', 'on')
    install_type = vscode.get('installType', 'system')
    task = tasks.create_task('uninstall_vscode', meta={'exe': exe, 'cleanup': cleanup},
                             title=f'卸载 {vscode.get("DisplayName", "VSCode")}')
    tasks.append_log(task, f'卸载任务已创建: {exe}（清理配置={cleanup}）')
    tasks.run_in_thread(task, _run_uninstall_vscode, exe, args,
                        vscode.get('DisplayName', ''), install_type, cleanup)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_uninstall_vscode(task: dict, exe_path: str, args: str, display_name: str,
                          install_type: str, cleanup: bool):
    """后台线程拉起 VSCode 卸载向导，轮询注册表直到卸载完成，可选清理配置。"""
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    elevate = (install_type == 'system')
    on_step(f'正在启动 {display_name or "VSCode"} 卸载程序...')
    if elevate:
        on_step('若弹出 UAC 对话框，请点击“是”授权。')
    on_step('卸载向导弹出后，请按提示完成卸载。')
    params = (args + ' ').strip() if args else ''
    command = f'/c start "" "{exe_path}" {params}'
    try:
        if elevate:
            code = run_elevated('cmd.exe', command, _VSCODE_UNINSTALL_TIMEOUT, show=0)
        else:
            code = _run_cmd_start(command, _VSCODE_UNINSTALL_TIMEOUT)
    except PermissionError as e:
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except TimeoutError:
        on_step('卸载程序已拉起，请在弹出的卸载向导中完成操作。')
        _wait_until_vscode_uninstalled(task, on_step, cleanup)
        return
    except OSError as e:
        task['result'] = {'success': False, 'message': f'启动卸载程序失败: {e}'}
        raise

    if code != 0:
        task['result'] = {'success': False, 'message': f'卸载程序启动异常，退出码 {code}'}
        raise RuntimeError(f'卸载程序启动异常，退出码 {code}')

    on_step('卸载程序已启动，请在弹出的卸载向导中按提示完成卸载。')
    _wait_until_vscode_uninstalled(task, on_step, cleanup)


def _wait_until_vscode_uninstalled(task: dict, on_step, cleanup: bool):
    """轮询注册表等待 VSCode 真正卸载完成；cleanup 为真时随后清理用户配置。"""
    waited = 0
    next_hint = 30
    while waited < _VSCODE_UNINSTALL_WAIT_TIMEOUT:
        time.sleep(_VSCODE_UNINSTALL_WAIT_INTERVAL)
        waited += _VSCODE_UNINSTALL_WAIT_INTERVAL
        if not vscode_detect_installed():
            on_step('检测到 VSCode 已从系统中移除，卸载完成。')
            if cleanup:
                on_step('正在清理 VSCode 用户配置（%APPDATA%\\Code、%USERPROFILE%\\.vscode）...')
                res = reset_vscode_settings()
                if res.get('removed'):
                    on_step('已清理配置目录: ' + ', '.join(res['removed']))
                if not res.get('success'):
                    on_step('配置清理未完全成功: ' + res.get('message', ''))
            tasks.set_progress(task, progress=100, stage='卸载完成')
            task['result'] = {'success': True,
                              'message': 'VSCode 卸载完成' + ('，配置已清理' if cleanup else '')}
            log_operation('python', '卸载 VSCode', detail=f'等待 {waited} 秒后检测到已卸载')
            return
        if waited >= next_hint:
            next_hint += 30
            on_step(f'仍在等待卸载向导完成...（已等待 {waited} 秒）')
            tasks.set_progress(task, stage=f'等待卸载向导完成（{waited}s）')
    on_step('等待超时：仍未检测到 VSCode 卸载完成，请确认是否已在卸载向导中完成操作。')
    tasks.set_progress(task, progress=100, stage='等待卸载完成超时')
    task['result'] = {'success': False, 'message': '等待卸载完成超时，请手动确认 VSCode 是否已卸载'}
    raise RuntimeError('等待 VSCode 卸载完成超时')


@require_GET
def api_vscode_info_refresh(request):
    """重新探测本机 VSCode 安装信息。"""
    try:
        get_vscode_options(refresh=True)
        return JsonResponse({'code': 0, 'msg': '刷新成功'})
    except Exception as e:
        logger.warning('刷新本机 VSCode 信息失败: %s', e)
        return JsonResponse({'code': 1, 'msg': f'刷新失败：{e}'})


@require_POST
def api_fetch_vscode_latest(request):
    """刷新 VSCode 最新版下载信息（user / system 两种 installer）。"""
    try:
        get_vscode_latest_all(refresh=True)
        return JsonResponse({'code': 0, 'msg': '抓取成功'})
    except Exception as e:
        return JsonResponse({'code': 1, 'msg': f'抓取失败：{e}'})


@require_POST
def api_vscode_reset(request):
    """重置 VSCode：删除 %APPDATA%\\Code 与 %USERPROFILE%\\.vscode。"""
    res = reset_vscode_settings()
    if res.get('success'):
        return JsonResponse({'code': 0, 'msg': 'VSCode 配置已重置！</br>请您重启 VSCode！'})
    return JsonResponse({'code': 500, 'msg': res.get('message', '重置失败')})


# ---------- PYPI源管理----------------------------------------
class PypiMixin(PythonMixin):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['current_content_name'] = 'python_pypi'
        return context

class PypiListView(PypiMixin, TemplateView):
    template_name = 'python_main/pypi_list.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Python 全局 PyPI 源配置'
        context['sources'] = pypi_config.get_all_sources()
        context['current_source'] = pypi_config.get_effective_source()
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': '全局 PyPI 源', 'href': None, 'active': True},
        ]
        return context


# ── 全局 PyPI 源 API ─────────────────────────

@require_GET
def api_pypi_get(request):
    """获取当前生效的全局 PyPI 源及可选源列表"""
    try:
        return JsonResponse({
            'success': True,
            'current': pypi_config.get_effective_source(),
            'sources': pypi_config.get_all_sources(),
        })
    except Exception as e:
        logger.exception('获取 PyPI 源配置失败')
        return JsonResponse({'success': False, 'message': f'获取失败: {e}'})


@require_POST
def api_pypi_set(request):
    """设置全局 PyPI 源"""
    url = request.POST.get('url', '').strip()
    result = pypi_config.set_index_url(url)
    result['current'] = pypi_config.get_effective_source()
    return JsonResponse(result)


@require_POST
def api_pypi_reset(request):
    """恢复官方默认源（清除自定义 index-url）"""
    result = pypi_config.unset_index_url()
    result['current'] = pypi_config.get_effective_source()
    return JsonResponse(result)


@require_POST
def api_pypi_add_custom(request):
    """添加自定义源（JSON 持久化，显示在源列表中）"""
    name = request.POST.get('name', '').strip()
    url = request.POST.get('url', '').strip()
    result = pypi_config.add_custom_source(name, url)
    if result.get('success'):
        result['sources'] = pypi_config.get_all_sources()
    return JsonResponse(result)


@require_POST
def api_pypi_remove_custom(request):
    """删除自定义源"""
    source_id = request.POST.get('id', '').strip()
    result = pypi_config.remove_custom_source(source_id)
    if result.get('success'):
        result['sources'] = pypi_config.get_all_sources()
        result['current'] = pypi_config.get_effective_source()
    return JsonResponse(result)


# ---------- 下载缓存清理 ----------------------------------------
def _path_size(path: pathlib.Path) -> int:
    """递归统计文件/目录占用字节数（用于汇报释放空间，出错按 0 计）。"""
    try:
        if path.is_file():
            return path.stat().st_size
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _human_size(num: int) -> str:
    """把字节数格式化为易读字符串。"""
    size = float(num)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


@require_POST
def api_clear_cache(request):
    """清理缓存：清空本应用 tmp 目录（tmp/python/）下的全部内容。

    删除 tmp/python/ 目录内所有文件与子目录（保留目录本身，pycharm_download /
    vscode_download 等下载目录会在下次下载时自动重建），不影响已安装的软件，
    也不触碰 DATA_DIR 里的版本/安装信息缓存文件。
    """
    removed_count = 0
    freed = 0
    errors = []
    tmp_dir = pathlib.Path(TMP_DIR)
    if tmp_dir.exists():
        for entry in list(tmp_dir.iterdir()):
            try:
                freed += _path_size(entry)
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed_count += 1
            except OSError as e:
                errors.append(f'{entry.name}: {e}')
                logger.warning('清理缓存失败 %s: %s', entry, e)
    if errors:
        return JsonResponse({
            'success': False,
            'message': f'部分文件清理失败：{"；".join(errors)}',
        })
    if removed_count == 0:
        return JsonResponse({'success': True, 'message': '缓存目录已为空，无需清理'})
    return JsonResponse({
        'success': True,
        'message': f'已清空本应用缓存目录，共删除 {removed_count} 项，释放 {_human_size(freed)}',
    })