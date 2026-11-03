"""
python_installer 视图层

页面视图:
- ContentView         安装程序主页：展示已安装列表
- VersionListView     官网版本列表页（独立页面）
- InstallOptionsView  安装选项页：配置安装参数并执行安装
- DownloadSourceView  下载源设置页：选择/保存默认镜像

JSON API:
- api_versions            获取版本列表
- api_install_start       创建后台安装任务（下载+安装），返回 task_id 供 JieTaskLog 轮询
- api_uninstall           卸载
- api_set_py_default      设置 py 启动器默认版本（未注册时自动注册后写用户级 py.ini）
- api_add_path            清除旧 Python PATH 条目后将安装目录加入用户 PATH（前置）
- api_open_terminal       以解释器目录为工作目录打开 cmd 命令行
- api_uninstall_gui       创建后台卸载任务，返回 task_id 供 JieTaskLog 轮询
- api_clear_cache         清空本应用 tmp 目录（供 python 应用顶部“清理缓存”按钮调用）
- api_download_sources    列出可选下载源与当前默认
- api_set_download_source 保存默认下载源
"""

import datetime

import os
import re
import shutil
import logging
import json
import pathlib

from django.urls import reverse

from . import storage_venv
from . import venv_utils
from django.http import JsonResponse, HttpResponse
from django.views.generic.base import TemplateView
from django.views.decorators.http import require_POST, require_GET
from django.shortcuts import render

from jiecore import tasks
from jiecore.views import AppMixin
from jiecore.ext_hook_registry import call_hooks
from jiecore.op_log import log_operation
from apps.python_main.views import PythonMixin

from . import storage
from . import python_installer
from . import python_download
from . import detect
from . import env_ops
from .python_versions import get_python_versions, build_version_entry
from .config import PYTHON_INSTALL_DIR, TMP_DIR, DEFAULT_VENV_ROOT

logger = logging.getLogger(__name__)

_VERSION_PARAM_RE = re.compile(r'^\d+\.\d+\.\d+$')


class InstallerMixin(PythonMixin):
    """python_installer 视图基类：继承 PythonMixin 自动获得导航上下文"""


# ── 安装程序主页 ─────────────────────────────

class ContentView(InstallerMixin, TemplateView):
    """安装程序主页：展示已安装列表 + 版本列表入口"""
    template_name = 'python_installer/content.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Python安装程序模式'
        # 填充 py 默认 / PATH 默认标记并按版本倒序
        context['installed'] = env_ops.annotate_installed(storage.list_installed())
        # 未安装 py 启动器时前端隐藏 py 列与“设置为py默认”菜单
        context['py_available'] = env_ops.py_launcher_installed()
        context['default_install_dir'] = str(PYTHON_INSTALL_DIR)
        # 注入 locked / venv_count 给 content.html 表格列
        for rec in context['installed']:
            rec.setdefault('locked', False)
            rec['venv_count'] = storage_venv.count_by_owner(rec.get('id', ''))
        cfg = storage.load_config()
        context['strict_uninstall_check'] = cfg.get('strict_uninstall_check', False)
        context['default_venv_root'] = str(cfg.get('install_dir', ''))
        owners = [{'id': r['id'], 'version': r['version']} for r in storage.list_installed()]
        context['owners_json'] = json.dumps(owners, ensure_ascii=False)
        return context


# ── 官网版本列表页（独立页面）────────────────────

class VersionListView(InstallerMixin, TemplateView):
    """官网版本列表页：独立页面，展示可安装的 Python 版本"""
    template_name = 'python_installer/version_list.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Python 版本列表'
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_installer:index'), 'active': False},
            {'title': 'Python 版本', 'href': None, 'active': True},
        ]
        return context


# ── 安装选项页 ────────────────────────────────────

class InstallOptionsView(InstallerMixin, TemplateView):
    """安装选项页：配置安装参数并执行安装"""
    template_name = 'python_installer/install_options.html'

    def get(self, request, *args, **kwargs):
        version = request.GET.get('version', '')
        if not _VERSION_PARAM_RE.fullmatch(version):
            from django.shortcuts import redirect
            return redirect('python_main:index')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        version = self.request.GET.get('version', '')
        entry = build_version_entry(version)
        context['entry'] = entry
        context['page_title'] = f'安装 Python {version}'
        context['default_install_dir'] = python_installer._get_default_install_dir(entry['major_minor'].replace('.',''))
        # 下载源：全部可选项 + 当前持久化默认（None 表示自动探测）
        context['download_sources'] = python_download.list_sources()
        context['current_source'] = python_download.get_default_source()  # None => auto
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_installer:index'), 'active': False},
            {'title': 'Python 版本', 'href': reverse('python_installer:version_list'), 'active': False},
            {'title': f'安装 Python {version}', 'href': None, 'active': True},
        ]
        return context


# ── 下载源设置页 ────────────────────────────────

class DownloadSourceView(InstallerMixin, TemplateView):
    """下载源设置页：选择并保存默认镜像"""
    template_name = 'python_installer/download_source.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '设置下载源'
        context['download_sources'] = python_download.list_sources()
        context['current_source'] = python_download.get_default_source()
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_installer:index'), 'active': False},
            {'title': 'Python可安装版本', 'href': reverse('python_installer:version_list'), 'active': False},
            {'title': '设置下载源', 'href': None, 'active': True},
        ]
        return context


# ── 应用设置页 ────────────────────────────────

class SettingsView(InstallerMixin, TemplateView):
    """应用设置页：Python 默认安装目录等配置"""
    template_name = 'python_installer/settings.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Python 安装程序设置'
        context['config'] = storage.load_config()
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_installer:index'), 'active': False},
            {'title': '设置', 'href': None, 'active': True},
        ]
        return context


# ── pip 包管理视图（在指定环境下）──────────────────

from apps.python_main.views import PipListViewMixin, PipInstallViewMixin


def _get_record_by_id(record_id):
    """按安装记录 id 取记录，未命中返回 None。"""
    return storage.get_installed_by_id(record_id) if record_id else None


class PipListView(InstallerMixin, PipListViewMixin):
    """pip 包管理页：管理指定 Installer 环境（按安装记录 id 定位）下的 Python 包。"""

    def get(self, request, *args, **kwargs):
        if not _get_record_by_id(kwargs.get('id', '')):
            from django.shortcuts import redirect
            return redirect('python_installer:index')
        return super().get(request, *args, **kwargs)

    def url_install(self):
        # “安装新包”跳转到完整页面（非弹窗），由本应用提供路由
        return reverse('python_installer:pip_install', args=[self.get_env_id()])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = _get_record_by_id(self.get_env_id()) or {}
        version = record.get('version', '')
        context['page_title'] = f'包管理 - Python {version}'
        context['record'] = record
        context['version'] = version
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': 'Python Installer环境', 'href': reverse('python_installer:index'), 'active': False},
            {'title': f'包管理', 'href': None, 'active': True},
        ]
        return context


class PipInstallView(InstallerMixin, PipInstallViewMixin):
    """安装新包页（完整页面）：在指定 Installer 环境下直接安装与 requirements.txt 导入安装新包。"""

    def get(self, request, *args, **kwargs):
        if not _get_record_by_id(kwargs.get('id', '')):
            from django.shortcuts import redirect
            return redirect('python_main:index')
        return super().get(request, *args, **kwargs)

    def url_list(self):
        # 返回包列表页
        return reverse('python_installer:pip_list', args=[self.get_env_id()])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = _get_record_by_id(self.get_env_id()) or {}
        version = record.get('version', '')
        context['page_title'] = f'安装新包 - Python {version}'
        context['record'] = record
        context['version'] = version
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': 'Python Installer环境', 'href': reverse('python_installer:index'), 'active': False},
            {'title': f'包管理',
             'href': reverse('python_installer:pip_list', args=[self.get_env_id()]), 'active': False},
            {'title': '安装新包', 'href': None, 'active': True},
        ]
        return context


# ── JSON API ─────────────────────────────────────
@require_GET
def api_uninstall_py(request):
    """ 卸载 py launcher"""
    from .detect import find_python_launcher_from_registry
    py_info = find_python_launcher_from_registry()
    if not py_info:
        return JsonResponse({'success': False, 'message': 'py launcher 未安装'})
    import subprocess
    try:
        logger.debug('卸载 py launcher: %s', py_info['UninstallString'])
        uninstall_command = py_info['UninstallString'].split(' ')
        uninstall_command.extend(['/qn', "/norestart"])
        logger.debug('卸载命令: %s', uninstall_command)
        proc = subprocess.run(
            uninstall_command,
            capture_output=True,
            timeout=300,
            creationflags=0x08000000 if os.name == 'nt' else 0,
        )
        logger.debug('卸载程序返回信息: %s', proc)
        if proc.returncode != 0:
            return JsonResponse({'success': False, 'message': f'卸载程序返回退出码 {proc.returncode}'})
    except subprocess.TimeoutExpired:
        return JsonResponse({'success': False, 'message': '卸载超时'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'卸载程序执行失败: {e}'})
    return JsonResponse({'success': True, 'message': 'py launcher 卸载成功'})


@require_GET
def api_del_record(request):
    """删除安装记录"""
    version = request.GET.get('version', '')
    if not version:
        return JsonResponse({'success': False, 'message': f'缺少安装记录 {version}'})
    storage.remove_installed(version)
    return JsonResponse({'success': True, 'message': '安装记录已删除'})


@require_GET
def api_versions(request):
    """获取版本列表。

    版本条目中的 msi_url 默认指向官网直链，供版本列表页"下载"按钮使用。
    若用户已持久化设置默认下载源（非自动探测），则将每个条目的 msi_url 改写为
    该镜像源地址，使"下载"按钮直接指向用户选择的下载源；未设置时保持官网直链。
    改写仅在响应时进行，不写回本地版本缓存（缓存始终以官网直链为准）。
    """
    refresh = request.GET.get('refresh') == '1'
    result = get_python_versions(refresh=refresh)
    default_source = python_download.get_default_source()
    if default_source and result.get('versions'):
        for entry in result['versions']:
            ver = entry.get('version')
            if ver:
                entry['msi_url'] = python_download.build_exe_url(default_source['url'], ver)
    return JsonResponse(result)


@require_GET
def api_installed(request):
    """探测当前系统中通过 installer 安装的 Python 版本"""
    try:
        installed = detect.detect_installed()
    except Exception as e:
        logger.warning('探测已安装 Python 失败: %s', e)
        return JsonResponse({'success': False, 'message': str(e), 'installed': {}})
    return JsonResponse({'success': True, 'installed': installed})


@require_POST
def api_import_installed(request):
    """探测系统已安装的 Python，清除旧记录后重新写入已安装列表"""
    try:
        result = detect.import_detected()
    except Exception as e:
        logger.exception('探测已安装 Python 失败')
        return JsonResponse({'success': False, 'message': f'探测失败: {e}'})
    added = result.get('added', [])
    total = result.get('total', 0)
    if added:
        message = f'探测到 {total} 个已安装版本：{", ".join(added)}'
    else:
        message = '未探测到已安装的 Python 版本'
    return JsonResponse({
        'success': True,
        'message': message,
        'added': added,
        'total': total,
    })


@require_POST
def api_install_start(request):
    """创建后台安装任务（下载+安装串在一起），返回 task_id 供 JieTaskLog 轮询。

    POST 参数：
        version          版本号
        install_dir      安装目录
        add_to_path      '1'/'0'
        install_pip      '1'/'0'
        install_py       '1'/'0'
        download_source  本次临时指定的下载源名（'auto'/空 表示走默认/自动探测）
        msi_url          可选，显式直链（兼容旧调用，给出时跳过下载源解析）
    """
    version = request.POST.get('version', '').strip()
    install_dir = request.POST.get('install_dir', '').strip()
    add_to_path = request.POST.get('add_to_path', '') == '1'
    install_pip = request.POST.get('install_pip', '') != '0'
    install_py = request.POST.get('install_py', '') != '0'
    download_source = request.POST.get('download_source', '').strip()
    msi_url = request.POST.get('msi_url', '').strip()

    if not _VERSION_PARAM_RE.fullmatch(version):
        return JsonResponse({'success': False, 'message': '版本号格式不正确'})
    if msi_url and not msi_url.startswith('https://'):
        return JsonResponse({'success': False, 'message': '下载地址必须为 https 链接'})
    if storage.get_installed(version):
        return JsonResponse({'success': False, 'message': f'Python {version} 已安装，请先卸载'})

    options = {
        'version': version,
        'install_dir': install_dir,
        'add_to_path': add_to_path,
        'install_pip': install_pip,
        'install_py': install_py,
        'download_source': download_source,
        'msi_url': msi_url,
    }

    task = tasks.create_task('python_installer.install', meta={'version': version},
                             title=f'安装 Python {version}')
    tasks.run_in_thread(task, _install_worker, options)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _install_worker(task, options):
    """后台安装任务体：下载源解析 → 下载 → 安装 → 登记。

    首参固定为 task（jiecore.tasks.run_in_thread 约定）；抛异常即视为失败，
    由 run_in_thread 统一置 error 状态。
    """
    version = options['version']

    def log(msg):
        # 每条日志同时刷新弹窗阶段文字：JieTaskLog 只展示 stage（不展示 progress 数值），
        # 若仅 append_log 而不改 stage，阶段文字会一直停留在上一次 set_progress
        # 设置的文案（如“解析下载源”），看起来像卡住。随日志同步 stage 后，
        # 下载百分比、安装阶段等都能实时反映到弹窗标题区。
        tasks.append_log(task, msg)
        tasks.set_progress(task, stage=msg)

    def progress(pct, stage=None):
        tasks.set_progress(task, progress=pct, stage=stage)

    try:
        progress(5, f'准备安装 Python {version}')
        log(f'安装选项：目录={options.get("install_dir") or "(默认)"}, '
            f'PATH={options["add_to_path"]}, pip={options["install_pip"]}, '
            f'py={options["install_py"]}, 下载源={options.get("download_source") or "默认/自动"}')

        # run_install 内部按阶段回调 on_progress；这里用 stage 粗粒度推进度条，
        # 具体百分比由下载函数按已下载字节计算后通过 log 输出
        progress(10, '解析下载源')
        result = python_installer.run_install(options, on_progress=log)

        progress(100, '安装完成')
        # 安装程序可能向注册表 PATH 写入新条目（py launcher 目录 / PrependPath），
        # 刷新当前进程 PATH 以便跳转列表页后 py_launcher_installed() 能正确探测
        env_ops.refresh_process_path()
        # 重新探测已安装列表，确保数据与系统状态一致
        try:
            detect.import_detected()
        except Exception as e:
            logger.warning('安装后探测已安装失败: %s', e)
        tasks.set_result(task, {
            'success': True,
            'message': f'Python {version} 安装成功',
            'python_exe': result.get('python_exe', ''),
            'install_dir': result.get('install_dir', ''),
            'download_source': result.get('download_source', ''),
        })
        log_operation('python_installer', f'安装 Python {version}',
                      detail=f'安装目录 {result.get("install_dir", "")}，解释器 {result.get("python_exe", "")}')
    except python_installer.InstallError as e:
        # 可预期错误：写进 result 供前端展示，状态由 run_in_thread 置 error
        tasks.set_result(task, {'success': False, 'message': str(e)})
        log_operation('python_installer', f'安装 Python {version}', success=False, detail=str(e))
        raise
    except Exception as e:
        logger.exception('安装 Python %s 失败', version)
        tasks.set_result(task, {'success': False, 'message': f'安装失败: {e}'})
        log_operation('python_installer', f'安装 Python {version}', success=False, detail=str(e))
        raise


@require_POST
def api_uninstall(request):
    """卸载指定版本"""
    version = request.POST.get('version', '').strip()
    if not _VERSION_PARAM_RE.fullmatch(version):
        return JsonResponse({'success': False, 'message': '版本号格式不正确'})

    logs = []

    def on_progress(msg):
        logs.append(msg)

    try:
        result = python_installer.run_uninstall(version, on_progress=on_progress)
        log_operation('python_installer', f'卸载 Python {version}',
                      success=result.get('success', False),
                      detail=result.get('message', ''))
        return JsonResponse({**result, 'logs': logs})
    except Exception as e:
        logger.exception('卸载 Python %s 失败', version)
        log_operation('python_installer', f'卸载 Python {version}',
                      success=False, detail=str(e))
        return JsonResponse({'success': False, 'message': f'卸载失败: {e}', 'logs': logs})


# ── py 默认 / 环境变量 / 终端 / 官方卸载 ──────────────

@require_POST
def api_set_py_default(request):
    """设置 py 启动器默认版本：写用户级 %LOCALAPPDATA%\\py.ini"""
    version = request.POST.get('version', '').strip()
    if not _VERSION_PARAM_RE.fullmatch(version):
        return JsonResponse({'success': False, 'message': '版本号格式不正确'})
    return JsonResponse(env_ops.set_py_default(version))


@require_POST
def api_add_path(request):
    """清除旧 Python PATH 条目后将指定版本的安装目录及 Scripts 前置加入用户 PATH"""
    version = request.POST.get('version', '').strip()
    record = storage.get_installed(version)
    if not record:
        return JsonResponse({'success': False, 'message': f'未找到 Python {version} 的安装记录'})
    install_dir = record.get('install_dir') or os.path.dirname(record.get('python_exe', ''))
    return JsonResponse(env_ops.add_to_path(install_dir))


@require_POST
def api_open_terminal(request):
    """以指定目录为工作目录打开 cmd 命令行窗口，PATH 前置该目录。"""
    work_dir = request.POST.get('path', '').strip()
    return JsonResponse(env_ops.open_terminal(
        work_dir=work_dir,
        path_prepend=[work_dir] if work_dir else None,
    ))


@require_POST
def api_uninstall_gui(request):
    """创建后台卸载任务，返回 task_id 供 JieTaskLog 轮询。

    参数: version=版本号；remove_leftovers=1 时卸载成功后删除安装目录遗留文件。
    卸载成功后移除本应用记录与 python 应用环境记录。
    """
    version = request.POST.get('version', '').strip()
    if not _VERSION_PARAM_RE.fullmatch(version):
        return JsonResponse({'success': False, 'message': '版本号格式不正确'})
    remove_leftovers = request.POST.get('remove_leftovers', '') == '1'

    options = {'version': version, 'remove_leftovers': remove_leftovers}
    task = tasks.create_task('python_installer.uninstall', meta={'version': version},
                             title=f'卸载 Python {version}')
    tasks.run_in_thread(task, _uninstall_gui_worker, options)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _uninstall_gui_worker(task, options):
    """后台卸载任务体：封装 run_uninstall_interactive 的逻辑，通过 tasks 实时输出进度。"""
    version = options['version']
    remove_leftovers = options['remove_leftovers']

    def log(msg):
        tasks.append_log(task, msg)
        tasks.set_progress(task, stage=msg)

    try:
        log(f'开始卸载 Python {version}')
        result = env_ops.run_uninstall_interactive(
            version, remove_leftovers, on_progress=log)
        if result.get('success'):
            log(result.get('message', '卸载完成'))
            env_ops.refresh_process_path()
            # 重新探测已安装列表，确保数据与系统状态一致
            try:
                detect.import_detected()
            except Exception as e:
                logger.warning('卸载后探测已安装失败: %s', e)
            tasks.set_result(task, result)
            log_operation('python_installer', f'卸载 Python {version}',
                          detail=f'版本 {version}' + ('，已清理遗留文件' if remove_leftovers else ''))
        else:
            log(result.get('message', '卸载失败'))
            tasks.set_result(task, result)
            log_operation('python_installer', f'卸载 Python {version}',
                          success=False, detail=f'版本 {version}：{result.get("message", "")}')
    except Exception as e:
        logger.exception('卸载 Python %s 失败', version)
        msg = f'卸载失败: {e}'
        log(msg)
        tasks.set_result(task, {'success': False, 'message': msg})
        raise


# ── 缓存清理 ────────────────────────────

@require_POST
def api_install_py_launcher(request):
    """为指定已安装版本安装 py_launcher。

    从版本数据中获取 launcher_url（win32/launcher.msi），下载并安装。
    先检查本应用下载缓存是否有 launcher MSI，有则直接安装；无则返回
    need_download=True 及下载大小预估，前端据此征求用户许可后再发起第二次请求。

    POST 参数:
        version   版本号
        confirmed '1' 表示用户已确认下载（无缓存时必需）
    """
    version = request.POST.get('version', '').strip()
    if not _VERSION_PARAM_RE.fullmatch(version):
        return JsonResponse({'success': False, 'message': '版本号格式不正确'})
    record = storage.get_installed(version)
    if not record:
        return JsonResponse({'success': False, 'message': f'未找到 Python {version} 的安装记录'})

    # 从版本数据中获取 launcher_url（build_version_entry 已预拼接）
    entry = build_version_entry(version)
    launcher_url = entry.get('launcher_url', '')

    from .config import DOWNLOAD_DIR
    msi_path = DOWNLOAD_DIR / f'launcher-{version}.msi'
    has_local = msi_path.exists()
    confirmed = request.POST.get('confirmed', '') == '1'

    # 无缓存且用户未确认 → 返回需要下载的信息，让前端征求用户许可
    if not has_local and not confirmed:
        return JsonResponse({
            'success': True,
            'need_download': True,
            'message': f'本地无 py launcher 安装包缓存，需从网络下载（约 1-3 MB），是否继续？',
        })

    task = tasks.create_task('python_installer.py_launcher', meta={'version': version},
                             title=f'安装 py_launcher {version}')
    tasks.run_in_thread(task, _py_launcher_worker, {
        'version': version,
        'install_dir': record.get('install_dir', ''),
        'has_local': has_local,
        'launcher_url': launcher_url,
    })
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _py_launcher_worker(task, options):
    """后台 py_launcher 安装任务体：下载 win32/launcher.msi（如需）+ msiexec 安装。

    优先使用版本数据中预拼接的 launcher_url；若缺失（旧缓存无此字段），
    则回退到 resolve_launcher_download 按镜像源重新拼接。
    """
    version = options['version']
    has_local = options.get('has_local', False)
    launcher_url = options.get('launcher_url', '')

    from .config import DOWNLOAD_DIR

    def log(msg):
        tasks.append_log(task, msg)
        tasks.set_progress(task, stage=msg)

    try:
        msi_path = DOWNLOAD_DIR / f'launcher-{version}.msi'

        if not has_local:
            # 需要下载：优先使用版本数据中的 launcher_url
            if not launcher_url:
                log('版本数据中无 launcher_url，回退到镜像源解析...')
                resolved = python_download.resolve_launcher_download(version, on_progress=log)
                launcher_url = resolved['launcher_url']
                source_name = resolved['source'].get('name', '')
                log(f'下载源: {source_name}')
            else:
                log(f'使用版本数据中的 launcher URL: {launcher_url}')
            msi_path = python_installer._download_launcher(launcher_url, version, on_progress=log)
        else:
            log(f'使用本地缓存 launcher 安装包: {msi_path}')

        # 安装前检查 MSI 是否存在
        if not os.path.isfile(str(msi_path)):
            raise RuntimeError(f'launcher 安装包不存在: {msi_path}')

        # 使用 msiexec 直接安装 launcher.msi
        python_installer._install_launcher_msi(msi_path, on_progress=log)

        env_ops.refresh_process_path()
        log('py_launcher 安装完成')
        tasks.set_result(task, {
            'success': True,
            'message': f'Python {version} py_launcher 安装完成',
        })
    except Exception as e:
        logger.exception('安装 py_launcher 失败')
        err_msg = f'安装失败: {e}'
        log(err_msg)
        tasks.set_result(task, {'success': False, 'message': err_msg})
        raise


def _path_size(path: pathlib.Path) -> int:
    """递归统计文件或目录占用字节数，出错按 0 处理。"""
    try:
        if path.is_file() or path.is_symlink():
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
    """字节数格式化为易读字符串。"""
    size = float(num)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


@require_POST
def api_clear_cache(request):
    """清空 python_installer 应用的 tmp 目录（tmp/python_installer/）下的全部内容。

    仅清理下载缓存（如 downloads/ 内的 Python 安装包），不影响已安装的 Python 环境，
    也不触碰 DATA_DIR 中的版本缓存与已安装记录。
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
        'message': f'已清空安装程序应用缓存目录，共删除 {removed_count} 项，释放 {_human_size(freed)}',
    })


# ── 下载源管理 ────────────────────────────────

@require_GET
def api_download_sources(request):
    """列出全部可选下载源与当前持久化默认。

    返回 {success, sources:[{name,display_name,url}], current:{name,url}|null}
    current 为 null 表示“自动探测”模式。
    """
    return JsonResponse({
        'success': True,
        'sources': python_download.list_sources(),
        'current': python_download.get_default_source(),
    })


@require_POST
def api_set_download_source(request):
    """保存默认下载源。POST 参数 name：源名或 'auto'（恢复自动探测）。"""
    name = request.POST.get('name', '').strip()
    try:
        saved = python_download.set_default_source(name)
    except ValueError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    except RuntimeError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    return JsonResponse({
        'success': True,
        'message': f'默认下载源已设为：{saved["display_name"]}',
        'current': saved,
    })


# ── 应用设置 ────────────────────────────────

@require_GET
def api_settings(request):
    """获取当前应用配置"""
    return JsonResponse({'success': True, 'config': storage.load_config()})


@require_POST
def api_settings_save(request):
    """保存应用配置"""
    config = storage.load_config()
    install_dir = request.POST.get('install_dir', '').strip()
    strict_uninstall_check = request.POST.get('strict_uninstall_check', '')
    if install_dir:
        p = pathlib.Path(install_dir)
        if not p.exists():
            return JsonResponse({'success': False,
                                 'message': f'目录不存在: {install_dir}'})
        config['install_dir'] = install_dir
    if strict_uninstall_check:
        config['strict_uninstall_check'] = strict_uninstall_check.lower() in ('on', 'true', '1')
    storage.save_config(config)
    return JsonResponse({'success': True, 'message': '设置已保存'})


# ═══════════════════════════════════════════════════════════
# 通用锁切换
# ═══════════════════════════════════════════════════════════
def api_toggle_lock(request):
    """通用锁切换接口：type=owner|venv, id=记录ID"""
    obj_type = request.POST.get('type', '').strip()
    obj_id = request.POST.get('id', '').strip()
    if obj_type not in ('owner', 'venv') or not obj_id:
        return JsonResponse({'success': False, 'message': '参数错误'})

    if obj_type == 'owner':
        result = storage.toggle_lock(obj_id)
        if not result:
            return JsonResponse({'success': False, 'message': '记录不存在'})
        label = result.get('name', '')
    else:
        result = storage_venv.toggle_venv_lock(obj_id)
        if not result:
            return JsonResponse({'success': False, 'message': '虚拟环境不存在'})
        label = result.get('name', '')

    locked = result.get('locked', False)
    state = '锁定' if locked else '解锁'
    return JsonResponse({'success': True, 'locked': locked,
                        'message': f'已{state} {label}'})


def _annotate_venvs(venvs):
    """给 venv 列表补充归属解释器版本"""
    installed_map = {r['id']: r for r in storage.list_installed()}
    for v in venvs:
        owner = installed_map.get(v.get('owner_record_id', ''), {})
        v['owner_version'] = owner.get('version', '')
        v.setdefault('locked', False)
    return venvs


# ═══════════════════════════════════════════════════════════
# VenvListView: 全局虚拟环境列表
# ═══════════════════════════════════════════════════════════
class VenvListView(InstallerMixin, TemplateView):
    template_name = 'python_installer/venv_list.html'

    def get(self, request, *args, **kwargs):
        record_id = kwargs.get('record_id', '')
        record = storage.get_by_id(record_id)
        if not record:
            from django.shortcuts import redirect
            return redirect('python_installer:index')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        record_id = self.kwargs.get('record_id', '')
        record = storage.get_by_id(record_id) or {}

        raw_venvs = storage_venv.list_venvs(owner_record_id=record_id)
        ctx['venvs'] = _annotate_venvs(raw_venvs)
        ctx['record'] = record
        ctx['record_id'] = record_id
        ctx['page_title'] = f'Python-{record.get("version", "")} 的虚拟环境'

        cfg = storage.load_config()
        ctx['default_venv_root'] = str(DEFAULT_VENV_ROOT).replace('\\', '/')
        owners = [{'id': r['id'], 'version': r['version']} for r in storage.list_installed()]
        ctx['owners_json'] = json.dumps(owners, ensure_ascii=False)
        ctx['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_installer:index'), 'active': False},
            {'title': f'Python-{record.get("version", "")} 的虚拟环境', 'href': None, 'active': True},
        ]
        return ctx


# ═══════════════════════════════════════════════════════════
# PipListVenvView / PipInstallVenvView（跨环境复用）
# ═══════════════════════════════════════════════════════════
class PipListVenvView(InstallerMixin, PipListViewMixin):
    def get(self, request, *args, **kwargs):
        vrec = storage_venv.get_venv_by_id(kwargs.get('venv_id', ''))
        if not vrec:
            from django.shortcuts import redirect
            return redirect('python_installer:venv_list')
        return super().get(request, *args, **kwargs)

    def get_env_id(self):
        return self.kwargs.get('venv_id', '')

    def url_install(self):
        return reverse('python_installer:pip_install_venv', args=[self.kwargs['venv_id']])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        vrec = storage_venv.get_venv_by_id(self.kwargs['venv_id']) or {}
        ctx['page_title'] = f'虚拟环境包管理 - {vrec.get("name", "")}'
        ctx['venv'] = vrec
        ctx['venv_id'] = self.kwargs['venv_id']
        ctx['breadcrumb'] = [
            {'title': 'Python 独立版本模式', 'href': reverse('python_installer:index'), 'active': False},
            {'title': '虚拟环境管理',
             'href': reverse('python_installer:venv_list', args=[vrec.get('owner_record_id', '')]),
             'active': False},
            {'title': f'包管理', 'href': None, 'active': True},
        ]
        return ctx


class PipInstallVenvView(InstallerMixin, PipInstallViewMixin):
    def get(self, request, *args, **kwargs):
        vrec = storage_venv.get_venv_by_id(kwargs.get('venv_id', ''))
        if not vrec:
            from django.shortcuts import redirect
            return redirect('python_installer:venv_list')
        return super().get(request, *args, **kwargs)

    def get_env_id(self):
        return self.kwargs.get('venv_id', '')

    def url_list(self):
        return reverse('python_installer:pip_list_venv', args=[self.get_env_id()])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        vrec = storage_venv.get_venv_by_id(self.kwargs['venv_id']) or {}
        ctx['page_title'] = f'安装包 - {vrec.get("name", "")}'
        ctx['venv'] = vrec
        ctx['record'] = vrec
        ctx['venv_id'] = self.kwargs['venv_id']
        return ctx


# ═══════════════════════════════════════════════════════════
# 虚拟环境 JSON API
# ═══════════════════════════════════════════════════════════
def api_venv_list(request, record_id=None):
    owner = storage.get_by_id(record_id) if record_id else None
    if record_id and not owner:
        return JsonResponse({'success': False, 'message': '解释器不存在'})
    raw_venvs = storage_venv.list_venvs(owner_record_id=record_id) if record_id else storage_venv.list_venvs()
    annotated = _annotate_venvs(raw_venvs)
    return JsonResponse({'success': True, 'venvs': annotated})


def _venv_create_worker(task, options):
    def log(msg):
        tasks.append_log(task, msg)

    try:
        log(f'开始创建虚拟环境 {options["venv_name"]}')
        log(f'保存路径: {options["venv_path"]}')
        if options['include_system_site']:
            log('已开启 继承系统 site-packages')

        def on_output(line):
            tasks.append_log(task, line)

        result = venv_utils.create_venv(
            options['install_dir'],
            target_path=options['venv_path'],
            include_system_site=options['include_system_site'],
            on_output=on_output)

        if not result.get('success'):
            tasks.set_result(task, {'success': False, 'message': result.get('message', '创建失败')})
            return

        verify = result.get('verify', {})
        log(f'验证通过，pip_count={verify.get("pip_count", 0)}')

        storage_venv.add_venv({
            'name': options['venv_name'],
            'owner_record_id': options['record_id'],
            'owner_install_dir': options['install_dir'],
            'owner_version': '',
            'venv_path': result.get('venv_path', ''),
            'python_exe': verify.get('python_exe', ''),
            'pip_count': verify.get('pip_count', 0),
            'include_system_site': options['include_system_site'],
            'created_source': 'created',
            'status': verify.get('status', 'ready'),
            'last_verified_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
        log('已登记到虚拟环境管理列表')
        log_operation('python_installer', f'创建虚拟环境 {options["venv_name"]}',
                      detail=f'归属 {options["record_name"]} -> {result.get("venv_path")}')
        tasks.set_result(task, {'success': True, 'message': result.get('message', '')})


    except Exception as _e:
        import traceback as _tb
        tasks.append_log(task, f'❌ 异常: {type(_e).__name__}: {_e}')
        for _line in _tb.format_exc().split(chr(10)):
            tasks.append_log(task, _line)
        tasks.set_result(task, {"success": False, "message": f'创建异常: {_e}'})


def api_venv_create(request):
    record_id = request.POST.get('record_id', '').strip()
    venv_path = request.POST.get('venv_path', '').strip()
    venv_name = request.POST.get('name', '').strip()
    include_system_site = request.POST.get('include_system_site') == '1'

    record = storage.get_by_id(record_id)
    if not record:
        return JsonResponse({'success': False, 'message': '归属的 Python 实例不存在'})

    if not venv_path:
        return JsonResponse({'success': False, 'message': '请选择虚拟环境保存目录'})

    path_obj = pathlib.Path(venv_path)
    name_part = path_obj.name or 'venv'
    import re as _re
    if _re.search(r'[^\w\-]', name_part, _re.UNICODE):
        return JsonResponse({'success': False, 'message':
                            '虚拟环境名称只能包含字母、数字、下划线和短横线（不能有中文或空格）'})

    if path_obj.exists():
        if not path_obj.is_dir():
            return JsonResponse({'success': False, 'message': f'目标路径是一个文件: {venv_path}'})
        try:
            if any(path_obj.iterdir()):
                return JsonResponse({'success': False, 'message':
                                    f'目标目录已存在且非空，请选择不存在的路径或先清空: {venv_path}'})
        except PermissionError:
            return JsonResponse({'success': False, 'message':
                                f'目标目录已存在且无法访问，请选择其他路径: {venv_path}'})

    existing = storage_venv.get_venv_by_path(venv_path)
    if existing:
        return JsonResponse({'success': False, 'message':
                            f'该路径已是虚拟环境记录，请改用导入功能'})

    if not venv_name:
        venv_name = path_obj.name or 'venv'

    task = tasks.create_task('venv.create',
                             meta={'record_id': record_id, 'venv_path': venv_path},
                             title=f'创建虚拟环境 {venv_name}')

    options = {
        'record_id': record_id,
        'venv_name': venv_name,
        'venv_path': venv_path,
        'include_system_site': include_system_site,
        'install_dir': record.get('install_dir', ''),
        'record_name': record.get('name', ''),
    }
    tasks.run_in_thread(task, _venv_create_worker, options)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def api_venv_import(request):
    import pathlib
    venv_path = request.POST.get('venv_path', '').strip()
    force = request.POST.get('force') == '1'

    result = venv_utils.import_venv(venv_path, force=force)
    if not result.get('success'):
        return JsonResponse({'success': False, 'message': result.get('message', '')})

    owner = result.get('owner_record')
    if not owner:
        return JsonResponse({'success': False, 'message':
                            '归属的 Python 未登记，请先安装或导入该 Python，'
                            '或使用强制导入（将标记为归属丢失）'})

    existing = storage_venv.get_venv_by_path(venv_path)
    if existing:
        return JsonResponse({'success': False, 'message': '该虚拟环境已在管理中'})

    verify = result.get('verify', {})
    name = os.path.basename(venv_path.rstrip(os.sep))
    storage_venv.add_venv({
        'name': name,
        'owner_record_id': owner.get('id', ''),
        'owner_install_dir': owner.get('install_dir', ''),
        'owner_version': owner.get('version', ''),
        'venv_path': venv_path,
        'python_exe': result.get('python_exe', ''),
        'pip_count': verify.get('pip_count', 0),
        'created_source': 'imported',
        'status': verify.get('status', 'ready'),
        'last_verified_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })
    log_operation('python_installer', f'导入虚拟环境 {name}', detail=venv_path)
    return JsonResponse({'success': True, 'message': '导入成功'})


@require_POST
def api_venv_delete(request, venv_id=None):
    venv_id = venv_id or request.POST.get('venv_id') or request.GET.get('venv_id')
    delete_dir = request.POST.get('delete_dir', '1') == '1'
    if not venv_id:
        return JsonResponse({'success': False, 'message': '缺少 venv_id'})
    rec = storage_venv.get_venv_by_id(venv_id)
    if not rec:
        return JsonResponse({'success': False, 'message': '虚拟环境不存在'})
    if rec.get('locked'):
        return JsonResponse({'success': False, 'blocked': True,
                            'message': '已锁定，请先解锁后再删除'})
    result = venv_utils.delete_venv(rec.get('venv_path', ''), delete_dir=delete_dir)
    if not result.get('success'):
        return JsonResponse(result)
    storage_venv.remove_venv(venv_id)
    return JsonResponse({'success': True, 'message': '删除成功'})


def api_venv_rename(request, venv_id=None):
    venv_id = venv_id or request.POST.get('venv_id')
    new_name = request.POST.get('new_name', '').strip()
    if not venv_id or not new_name:
        return JsonResponse({'success': False, 'message': '参数缺失'})
    rec = storage_venv.get_venv_by_id(venv_id)
    if not rec:
        return JsonResponse({'success': False, 'message': '不存在'})
    result = storage_venv.update_venv(venv_id, name=new_name)
    return JsonResponse({'success': bool(result), 'message': '重命名成功' if result else '失败'})


def api_venv_verify(request, venv_id=None):
    venv_id = venv_id or request.GET.get('venv_id')
    if not venv_id:
        return JsonResponse({'success': False, 'message': '缺少 venv_id'})
    rec = storage_venv.get_venv_by_id(venv_id)
    if not rec:
        return JsonResponse({'success': False, 'message': '不存在'})
    l2 = venv_utils.verify_venv_l2(rec.get('venv_path', ''))
    storage_venv.update_venv(venv_id,
        status=l2.get('status', 'ready'),
        pip_count=l2.get('pip_count', 0),
        status_reason=l2.get('reason', ''),
        last_verified_at=l2.get('verified_at', ''))
    return JsonResponse({
        'success': True,
        'status': l2.get('status', 'ready'),
        'reason': l2.get('reason', ''),
        'pip_count': l2.get('pip_count', 0),
        'pip_version': l2.get('pip_version', ''),
    })


def api_venv_verify_l2(request, venv_id=None):
    venv_id = venv_id or request.GET.get('venv_id')
    if not venv_id:
        return JsonResponse({'success': False, 'message': '缺少 venv_id'})
    rec = storage_venv.get_venv_by_id(venv_id)
    if not rec:
        return JsonResponse({'success': False, 'message': '不存在'})
    result = venv_utils.verify_venv_l2(rec.get('venv_path', ''))
    fields = {
        'status': result['status'],
        'status_reason': result.get('reason', ''),
        'last_verified_at': result.get('verified_at', ''),
    }
    if 'pip_count' in result:
        fields['pip_count'] = result['pip_count']
    storage_venv.update_venv(venv_id, **fields)
    return JsonResponse({'success': True, 'result': result})



def _terminal_cmd(venv_path: str) -> str:
    bat = os.path.join(venv_path, 'Scripts', 'activate.bat')
    if os.path.exists(bat):
        return f'cmd /k "{bat}"'
    return f'cmd /k cd /d "{venv_path}"'


def api_venv_open_terminal(request):
    venv_id = request.POST.get('venv_id', '').strip()
    record = storage_venv.get_venv_by_id(venv_id)
    if not record:
        return JsonResponse({'success': False, 'message': '虚拟环境不存在'})

    venv_path = record.get('venv_path', '')
    venv_scripts = os.path.join(venv_path, 'Scripts')
    bat = os.path.join(venv_scripts, 'activate.bat')
    activate_cmd = f'"{bat}"' if os.path.exists(bat) else None

    return JsonResponse(env_ops.open_terminal(
        work_dir=venv_path,
        path_prepend=[venv_scripts] if venv_path else None,
        activate_cmd=activate_cmd,
    ))


@require_GET


def api_venv_sync(request):
    try:
        summary = venv_utils.sync_all_venvs()
        return JsonResponse({'success': True, 'summary': summary})
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)})