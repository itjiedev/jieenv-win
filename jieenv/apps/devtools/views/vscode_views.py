import os
import time
import logging
import pathlib
import subprocess

from django.urls import reverse
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic.base import TemplateView
from django.views.decorators.http import require_POST, require_GET

from jiecore.system_ops import run_elevated
from jiecore.op_log import log_operation
from jiecore import tasks

from apps.devtools.views import DevtoolsMixin
from apps.devtools.services.vscode.installer import (
    get_vscode_options, get_vscode_latest, get_vscode_latest_all,
    download_vscode, run_silent_install as vscode_run_silent_install,
    default_install_dir as vscode_default_install_dir,
    find_vscode_exe, vscode_detect_installed, reset_vscode_settings,
    INSTALLER_LABELS as VSCODE_INSTALLER_LABELS,
    PythonVscodeError,
)

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
_VSCODE_LAUNCH_TIMEOUT = 10
_VSCODE_UNINSTALL_TIMEOUT = 15
_VSCODE_UNINSTALL_WAIT_INTERVAL = 2
_VSCODE_UNINSTALL_WAIT_TIMEOUT = 900


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


def _run_cmd_start(command: str, timeout: int) -> int:
    try:
        proc = subprocess.run(f'cmd.exe {command}', timeout=timeout,
                              creationflags=_CREATE_NO_WINDOW,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f'命令执行超时（{timeout} 秒）')
    return proc.returncode


# ── 页面视图 ─────────────────────────────────────────────

class VscodeVersionsView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/vscode/versions.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'VSCode 版本列表'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': 'VSCode 版本列表', 'href': None, 'active': True},
        ]
        try:
            latest_all = get_vscode_latest_all(refresh=False, allow_fetch=False)
        except Exception:
            latest_all = {}
        has_data = latest_all and any(latest_all.values())
        context['need_fetch'] = not has_data
        context['latest_all'] = latest_all
        context['installer_labels'] = VSCODE_INSTALLER_LABELS
        first_info = next((v for v in latest_all.values() if v), {})
        context['product_version'] = first_info.get('productVersion', '')
        context['fetched_at'] = first_info.get('fetched_at', '')
        context['current_vscode'] = get_vscode_options() or {}
        return context


class VscodeInstallView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/vscode/install.html'

    def get(self, request, *args, **kwargs):
        if get_vscode_options():
            return redirect('devtools:vscode_detail')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '安装 VSCode'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': 'VSCode 版本列表', 'href': reverse('devtools:vscode_versions'), 'active': False},
            {'title': '安装 VSCode', 'href': None, 'active': True},
        ]
        selected_type = self.request.GET.get('installer_type', 'user')
        try:
            context['latest'] = get_vscode_latest_all(refresh=False, allow_fetch=False)
            context['latest_error'] = ''
        except Exception:
            context['latest'] = {}
        has_data = context['latest'] and any(context['latest'].values())
        context['need_fetch'] = not has_data
        context['installer_labels'] = VSCODE_INSTALLER_LABELS
        context['selected_installer_type'] = selected_type if selected_type in ('user', 'system') else 'user'
        context['default_install_dir'] = vscode_default_install_dir(context['selected_installer_type'])
        context['vscode'] = get_vscode_options()
        return context


class VscodeDetailView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/vscode/detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'VSCode - 详细信息'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': 'VSCode - 详细信息', 'href': None, 'active': True},
        ]
        context['vscode'] = get_vscode_options()
        return context


class VscodeUninstallView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/vscode/uninstall.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '卸载 VSCode'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': '卸载 VSCode', 'href': None, 'active': True},
        ]
        context['vscode'] = get_vscode_options()
        return context


# ── API ───────────────────────────────────────────────────

@require_POST
def api_vscode_install(request):
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
        log_operation('devtools', f'安装 VSCode {version}',
                      detail=f'安装目录 {install_dir}')
    else:
        task['result'] = {'success': False, 'message': f'VSCode 安装异常，退出码 {code}'}
        log_operation('devtools', f'安装 VSCode {version}', success=False,
                      detail=f'安装目录 {install_dir}，退出码 {code}')
        raise RuntimeError(f'VSCode 安装异常，退出码 {code}')


@require_POST
def api_start_vscode(request):
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


@require_POST
def api_vscode_uninstall(request):
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
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    elevate = (install_type == 'system')
    on_step(f'正在启动 {display_name or "VSCode"} 卸载程序...')
    if elevate:
        on_step('若弹出 UAC 对话框，请点击"是"授权。')
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
            log_operation('devtools', '卸载 VSCode', detail=f'等待 {waited} 秒后检测到已卸载')
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
    try:
        get_vscode_options(refresh=True)
        return JsonResponse({'code': 0, 'msg': '刷新成功'})
    except Exception as e:
        logger.warning('刷新本机 VSCode 信息失败: %s', e)
        return JsonResponse({'code': 1, 'msg': f'刷新失败：{e}'})


@require_POST
def api_fetch_vscode_latest(request):
    try:
        get_vscode_latest_all(refresh=True)
        return JsonResponse({'code': 0, 'msg': '抓取成功'})
    except Exception as e:
        return JsonResponse({'code': 1, 'msg': f'抓取失败：{e}'})


@require_POST
def api_vscode_reset(request):
    res = reset_vscode_settings()
    if res.get('success'):
        return JsonResponse({'code': 0, 'msg': 'VSCode 配置已重置！</br>请您重启 VSCode！'})
    return JsonResponse({'code': 500, 'msg': res.get('message', '重置失败')})