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
from .config import TMP_DIR

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
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Python 环境管理'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': None, 'active': True},
        ]
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

    删除 tmp/python/ 目录内所有文件与子目录（保留目录本身，下载目录会在下次使用时自动重建），不影响已安装的软件，
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