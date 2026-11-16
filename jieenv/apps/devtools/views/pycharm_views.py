import os
import time
import logging
import pathlib
import subprocess
from functools import lru_cache

from django.urls import reverse
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic.base import TemplateView, RedirectView
from django.views.decorators.http import require_POST, require_GET
from django.utils.functional import cached_property

from jiecore.system_ops import run_elevated
from jiecore.op_log import log_operation
from jiecore import tasks

from apps.devtools.views import DevtoolsMixin
from apps.devtools.services.pycharm.versions import get_pycharm_versions
from apps.devtools.services.pycharm.config import LIMIT_VERSION
from apps.devtools.services.pycharm.installer import (
    get_pycharm_options, find_version_entry, default_install_dir,
    download_pycharm, run_silent_install, pycharm_detect_installed,
    PythonPycharmError,
)

logger = logging.getLogger(__name__)

_PYCHARM_UNINSTALL_TIMEOUT = 15
_PYCHARM_UNINSTALL_WAIT_INTERVAL = 2
_PYCHARM_UNINSTALL_WAIT_TIMEOUT = 900
_PYCHARM_LAUNCH_TIMEOUT = 10


def _split_uninstall_command(cmd: str):
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


def _find_pycharm_exe(install_dir: str):
    base = pathlib.Path(install_dir)
    for name in ('pycharm64.exe', 'pycharm.exe'):
        candidate = base / 'bin' / name
        if candidate.exists():
            return candidate
    return None


# ── 页面视图 ─────────────────────────────────────────────

class PycharmVersionsView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/pycharm/versions.html'

    @cached_property
    def pycharm_versions(self):
        return get_pycharm_versions(refresh=False, allow_fetch=False)

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
        if not self.versions_map:
            return []
        if self.main_version not in self.versions_map:
            return []
        return self.versions_map[self.main_version]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'PyCharm - 版本选择'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': 'PyCharm - 版本选择', 'href': None, 'active': True},
        ]
        context['fetched_at'] = self.pycharm_versions.get('fetched_at')
        context['main_versions'] = list(self.versions_map.keys())
        context['versions'] = self.version_list
        context['current_main_version'] = self.main_version
        context['current_pycharm'] = get_pycharm_options()
        context['need_fetch'] = not bool(self.versions_map)
        return context


class PycharmDetailView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/pycharm/detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'PyCharm - 详细信息'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': 'PyCharm - 详细信息', 'href': None, 'active': True},
        ]
        context['pycharm'] = get_pycharm_options()
        return context


class PycharmVersionsRefreshView(RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        logger.info('刷新 PyCharm 版本列表')
        get_pycharm_versions(refresh=True, limit_ver=LIMIT_VERSION)
        return reverse('devtools:pycharm_versions')


class PycharmUninstallView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/pycharm/uninstall.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '卸载 PyCharm'
        context['pycharm'] = get_pycharm_options()
        return context


class PycharmInstallView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/pycharm/install.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        version = self.kwargs.get('version', '')
        entry = find_version_entry(version)
        context['page_title'] = f'安装 PyCharm {version}'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': 'PyCharm - 版本选择', 'href': reverse('devtools:pycharm_versions'), 'active': False},
            {'title': f'安装 PyCharm', 'href': None, 'active': True},
        ]
        context['version'] = version
        context['entry'] = entry or {}
        context['default_install_dir'] = default_install_dir(version)
        context['pycharm'] = get_pycharm_options()
        return context


# ── API ───────────────────────────────────────────────────

@require_POST
def api_fetch_pycharm_version(request):
    try:
        get_pycharm_versions(refresh=True, limit_ver=LIMIT_VERSION)
        return JsonResponse({'code': 0, 'msg': '抓取成功'})
    except Exception as e:
        return JsonResponse({'code': 1, 'msg': f'抓取失败：{e}'})


@require_GET
def api_pycharm_info_refresh(request):
    try:
        get_pycharm_options(refresh=True)
        return JsonResponse({'code': 0, 'msg': '刷新成功'})
    except Exception as e:
        logger.warning('刷新本机 PyCharm 信息失败: %s', e)
        return JsonResponse({'code': 1, 'msg': f'刷新失败：{e}'})


@require_POST
def api_pycharm_install(request):
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
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'开始下载 PyCharm {version} 安装包...')
    tasks.set_progress(task, progress=5)
    try:
        exe_path = download_pycharm(download_url, filename, on_progress=on_step)
    except Exception as e:
        task['result'] = {'success': False, 'message': f'下载 PyCharm 安装包失败: {e}'}
        raise
    tasks.set_progress(task, progress=50, stage='下载完成，准备安装')

    try:
        code = run_silent_install(exe_path, install_dir, on_progress=on_step)
    except PermissionError as e:
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
        try:
            get_pycharm_options(refresh=True)
        except Exception as e:
            logger.warning('安装后刷新 PyCharm 信息失败: %s', e)
        task['result'] = {'success': True, 'message': f'PyCharm {version} 安装成功',
                          'install_dir': install_dir}
        log_operation('devtools', f'安装 PyCharm {version}',
                      detail=f'安装目录 {install_dir}')
    else:
        task['result'] = {'success': False, 'message': f'PyCharm 安装异常，退出码 {code}'}
        log_operation('devtools', f'安装 PyCharm {version}', success=False,
                      detail=f'安装目录 {install_dir}，退出码 {code}')
        raise RuntimeError(f'PyCharm 安装异常，退出码 {code}')


@require_POST
def api_pycharm_uninstall(request):
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
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'正在以管理员权限启动 {display_name or "PyCharm"} 卸载程序...')
    on_step('若弹出 UAC 对话框，请点击"是"授权。')
    on_step('卸载向导弹出后，请按提示完成卸载。')
    params = (args + ' ').strip() if args else ''
    command = f'/c start "" "{exe_path}" {params}'
    try:
        code = run_elevated('cmd.exe', command, _PYCHARM_UNINSTALL_TIMEOUT, show=0)
    except PermissionError as e:
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except TimeoutError:
        on_step('卸载程序已拉起，请在弹出的卸载向导中完成操作。')
        _wait_until_uninstalled(task, on_step)
        return
    except OSError as e:
        task['result'] = {'success': False, 'message': f'提权启动卸载程序失败: {e}'}
        raise

    if code != 0:
        task['result'] = {'success': False, 'message': f'卸载程序启动异常，退出码 {code}'}
        raise RuntimeError(f'卸载程序启动异常，退出码 {code}')

    on_step('卸载程序已启动，请在弹出的卸载向导中按提示完成卸载。')
    _wait_until_uninstalled(task, on_step)


def _wait_until_uninstalled(task: dict, on_step):
    waited = 0
    next_hint = 30
    while waited < _PYCHARM_UNINSTALL_WAIT_TIMEOUT:
        time.sleep(_PYCHARM_UNINSTALL_WAIT_INTERVAL)
        waited += _PYCHARM_UNINSTALL_WAIT_INTERVAL
        if not pycharm_detect_installed():
            on_step('检测到 PyCharm 已从系统中移除，卸载完成。')
            tasks.set_progress(task, progress=100, stage='卸载完成')
            task['result'] = {'success': True, 'message': 'PyCharm 卸载完成'}
            log_operation('devtools', '卸载 PyCharm', detail=f'等待 {waited} 秒后检测到已卸载')
            return
        if waited >= next_hint:
            next_hint += 30
            on_step(f'仍在等待卸载向导完成...（已等待 {waited} 秒）')
            tasks.set_progress(task, stage=f'等待卸载向导完成（{waited}s）')
    on_step('等待超时：仍未检测到 PyCharm 卸载完成，请确认是否已在卸载向导中完成操作。')
    tasks.set_progress(task, progress=100, stage='等待卸载完成超时')
    task['result'] = {'success': False, 'message': '等待卸载完成超时，请手动确认 PyCharm 是否已卸载'}
    raise RuntimeError('等待 PyCharm 卸载完成超时')


@require_POST
def api_start_pycharm(request):
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
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'正在以管理员权限启动 {display_name or "PyCharm"}...')
    on_step('若弹出 UAC 对话框，请点击"是"授权。')
    command = f'/c start "" "{exe_path}"'
    try:
        code = run_elevated('cmd.exe', command, _PYCHARM_LAUNCH_TIMEOUT, show=0)
    except PermissionError as e:
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except TimeoutError:
        on_step('PyCharm 启动命令已下发。')
        tasks.set_progress(task, progress=100, stage='启动成功')
        task['result'] = {'success': True, 'message': 'PyCharm 已启动'}
        return
    except OSError as e:
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
    import shutil
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
            return JsonResponse({'code': 500, 'msg': f'重置失败:{e}'})
    else:
        logger.warning(f'没有找到 pycharm 安装信息！')
        return JsonResponse({'code': 500, 'msg': '找不到 Pycharm 安装信息！'})
    return JsonResponse({'code': 0, 'msg': 'Pycharm 配置已经重置！</br>请您重启 Pycharm!'})