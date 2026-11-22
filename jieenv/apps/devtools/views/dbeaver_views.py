import os
import json
import time
import logging
import pathlib
import shutil
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
from apps.devtools.services.dbeaver.installer import (
    get_dbeaver_options, get_dbeaver_latest_all,
    download_dbeaver, run_silent_install_exe,
    default_install_dir, dbeaver_detect_installed,
    _find_dbeaver_exe, _unregister_install,
    INSTALLER_LABELS as DBEAVER_INSTALLER_LABELS,
    DBeaverError,
)

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
_DBEAVER_LAUNCH_TIMEOUT = 10
_DBEAVER_UNINSTALL_TIMEOUT = 15
_DBEAVER_UNINSTALL_WAIT_INTERVAL = 2
_DBEAVER_UNINSTALL_WAIT_TIMEOUT = 600


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


class DbeaverVersionsView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/dbeaver/versions.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'DBeaver 版本列表'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': 'DBeaver 版本列表', 'href': None, 'active': True},
        ]
        try:
            latest_all = get_dbeaver_latest_all(refresh=False, allow_fetch=True)
        except Exception:
            latest_all = {}
        has_data = bool(latest_all) and any(latest_all.values())
        context['need_fetch'] = not has_data
        context['latest_all'] = latest_all
        context['installer_labels'] = DBEAVER_INSTALLER_LABELS
        first_info = next((v for v in latest_all.values() if v), {})
        context['product_version'] = first_info.get('productVersion', '')
        context['fetched_at'] = first_info.get('fetched_at', '')
        context['current_dbeaver'] = get_dbeaver_options() or {}
        context['latest_json'] = json.dumps(latest_all, ensure_ascii=False)
        return context


class DbeaverInstallView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/dbeaver/install.html'

    def get(self, request, *args, **kwargs):
        if get_dbeaver_options():
            return redirect('devtools:dbeaver_detail')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '安装 DBeaver'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': 'DBeaver 版本列表', 'href': reverse('devtools:dbeaver_versions'), 'active': False},
            {'title': '安装 DBeaver', 'href': None, 'active': True},
        ]
        selected_type = self.request.GET.get('installer_type', 'exe')
        try:
            context['latest'] = get_dbeaver_latest_all(refresh=False, allow_fetch=False)
            context['latest_error'] = ''
        except Exception as e:
            context['latest'] = {}
            context['latest_error'] = str(e)
        context['installer_labels'] = DBEAVER_INSTALLER_LABELS
        context['selected_installer_type'] = 'exe'
        context['default_install_dir'] = default_install_dir(context['selected_installer_type'])
        context['dbeaver'] = get_dbeaver_options()
        return context


class DbeaverDetailView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/dbeaver/detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'DBeaver - 详细信息'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': 'DBeaver - 详细信息', 'href': None, 'active': True},
        ]
        context['dbeaver'] = get_dbeaver_options()
        return context


class DbeaverUninstallView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/dbeaver/uninstall.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '卸载 DBeaver'
        context['breadcrumb'] = [
            {'title': '开发工具', 'href': reverse('devtools:index'), 'active': False},
            {'title': '卸载 DBeaver', 'href': None, 'active': True},
        ]
        context['dbeaver'] = get_dbeaver_options()
        return context


@require_POST
def api_dbeaver_install(request):
    if get_dbeaver_options():
        return JsonResponse({'success': False,
                             'message': 'DBeaver 已安装，不支持重装；如需重装请先卸载后再安装'})
    installer_type = 'exe'
    install_dir = (request.POST.get('install_dir') or '').strip()
    mirror_key = (request.POST.get('mirror_key') or '').strip()
    if not install_dir:
        return JsonResponse({'success': False, 'message': '请指定安装目录'})

    try:
        latest_all = get_dbeaver_latest_all(refresh=False, allow_fetch=False)
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'获取 DBeaver 最新版失败: {e}'})

    info = latest_all.get(installer_type)
    if not info:
        return JsonResponse({'success': False, 'message': '没有可用的下载信息，请先从官网抓取最新版本'})

    mirrors = info.get('mirrors', [])
    if not mirrors:
        return JsonResponse({'success': False, 'message': '没有可用的下载地址，请先抓取最新版本'})

    ordered = []
    if mirror_key:
        for m in mirrors:
            if m.get('key') == mirror_key:
                ordered.append(m)
                break
    for m in mirrors:
        if m not in ordered:
            ordered.append(m)

    download_urls = [m['url'] for m in ordered if m.get('url')]
    if not download_urls:
        return JsonResponse({'success': False, 'message': '没有可用的下载地址'})

    filename = info.get('filename', '')
    version = info.get('productVersion', '')
    selected_label = ordered[0].get('label', '')

    task = tasks.create_task('install_dbeaver',
                             meta={'installer_type': installer_type,
                                   'install_dir': install_dir, 'version': version,
                                   'mirror_key': mirror_key},
                             title=f'安装 DBeaver {version}')
    tasks.append_log(task, f'安装任务已创建: DBeaver {version}（{installer_type}）-> {install_dir}')
    if selected_label:
        tasks.append_log(task, f'首选镜像: {selected_label}')
    tasks.run_in_thread(task, _run_install_dbeaver, installer_type, download_urls,
                        install_dir, filename, version)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_install_dbeaver(task: dict, installer_type: str, download_urls: list[str],
                         install_dir: str, filename: str, version: str):
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'开始下载 DBeaver {version}（{installer_type}）安装包...')
    tasks.set_progress(task, progress=5)
    try:
        pkg_path = download_dbeaver(download_urls, filename, on_progress=on_step)
    except Exception as e:
        task['result'] = {'success': False, 'message': f'下载 DBeaver 安装包失败: {e}'}
        raise
    tasks.set_progress(task, progress=50, stage='下载完成，准备安装')

    if installer_type == 'exe':
        try:
            code = run_silent_install_exe(pkg_path, install_dir, on_progress=on_step)
        except PermissionError as e:
            task['result'] = {'success': False, 'message': str(e)}
            raise
        except DBeaverError as e:
            task['result'] = {'success': False, 'message': str(e)}
            raise
        except TimeoutError:
            task['result'] = {'success': False, 'message': 'DBeaver 安装超时，请检查系统状态后重试'}
            raise
        except OSError as e:
            task['result'] = {'success': False, 'message': f'静默安装失败: {e}'}
            raise
        if code != 0:
            task['result'] = {'success': False, 'message': f'DBeaver 安装异常，退出码 {code}'}
            raise RuntimeError(f'DBeaver 安装异常，退出码 {code}')

    on_step('DBeaver 安装完成。')
    tasks.set_progress(task, progress=100, stage='安装成功')
    try:
        dbeaver_detect_installed()
    except Exception as e:
        logger.warning('安装后刷新 DBeaver 信息失败: %s', e)
    task['result'] = {'success': True, 'message': f'DBeaver {version} 安装成功',
                      'install_dir': install_dir}
    log_operation('devtools', f'安装 DBeaver {version}',
                  detail=f'安装目录 {install_dir}（{installer_type}）')


@require_POST
def api_start_dbeaver(request):
    dbeaver = get_dbeaver_options()
    if not dbeaver:
        return JsonResponse({'success': False, 'message': '未检测到已安装的 DBeaver'})
    install_dir = dbeaver.get('InstallLocation', '')
    if not install_dir:
        return JsonResponse({'success': False,
                             'message': 'DBeaver 安装信息缺少安装目录（InstallLocation）'})
    exe = _find_dbeaver_exe(install_dir)
    if not exe:
        return JsonResponse({'success': False,
                             'message': f'未在 {install_dir} 下找到 dbeaver.exe'})
    task = tasks.create_task('start_dbeaver', meta={'exe': str(exe)},
                             title=f'启动 DBeaver')
    tasks.append_log(task, f'启动任务已创建: {exe}')
    tasks.run_in_thread(task, _run_start_dbeaver, str(exe))
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_start_dbeaver(task: dict, exe_path: str):
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step('正在启动 DBeaver...')
    command = f'/c start "" "{exe_path}"'
    try:
        code = _run_cmd_start(command, _DBEAVER_LAUNCH_TIMEOUT)
    except TimeoutError:
        on_step('DBeaver 启动命令已下发。')
        tasks.set_progress(task, progress=100, stage='启动成功')
        task['result'] = {'success': True, 'message': 'DBeaver 已启动'}
        return
    except OSError as e:
        task['result'] = {'success': False, 'message': f'启动失败: {e}'}
        raise
    if code == 0:
        on_step('DBeaver 已启动。')
        tasks.set_progress(task, progress=100, stage='启动成功')
        task['result'] = {'success': True, 'message': 'DBeaver 已启动'}
    else:
        task['result'] = {'success': False, 'message': f'DBeaver 启动异常，退出码 {code}'}
        raise RuntimeError(f'DBeaver 启动异常，退出码 {code}')


@require_POST
def api_dbeaver_uninstall(request):
    dbeaver = get_dbeaver_options()
    if not dbeaver:
        return JsonResponse({'success': False, 'message': '未检测到已安装的 DBeaver'})

    purge_data = request.POST.get('purge_data', '0') == '1'

    uninstall_cmd = (dbeaver.get('UninstallString') or '').strip()
    if not uninstall_cmd:
        return JsonResponse({'success': False,
                             'message': 'DBeaver 安装信息缺少卸载命令（UninstallString）'})
    exe, args = _split_uninstall_command(uninstall_cmd)
    if not exe or not os.path.exists(exe):
        return JsonResponse({'success': False,
                             'message': f'卸载程序不存在: {exe or uninstall_cmd}'})

    task = tasks.create_task('uninstall_dbeaver', meta={'exe': exe, 'purge_data': purge_data},
                             title=f'卸载 {dbeaver.get("DisplayName", "DBeaver")}')
    tasks.append_log(task, f'卸载任务已创建: {exe}')
    tasks.run_in_thread(task, _run_uninstall_dbeaver_exe, exe, args)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_uninstall_dbeaver_exe(task: dict, exe_path: str, args: str):
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    purge_data = bool(task.get('meta', {}).get('purge_data'))

    on_step('正在以管理员权限启动 DBeaver 卸载程序...')
    on_step('若弹出 UAC 对话框，请点击"是"授权。')
    on_step('卸载向导弹出后，请按提示完成卸载。')
    params = (args + ' ').strip() if args else ''
    command = f'/c start "" "{exe_path}" {params}'
    try:
        code = run_elevated('cmd.exe', command, _DBEAVER_UNINSTALL_TIMEOUT, show=0)
    except PermissionError as e:
        task['result'] = {'success': False, 'message': str(e)}
        raise
    except TimeoutError:
        on_step('卸载程序已拉起，请在弹出的卸载向导中完成操作。')
        _wait_until_dbeaver_uninstalled(task, on_step, purge_data)
        return
    except OSError as e:
        task['result'] = {'success': False, 'message': f'提权启动卸载程序失败: {e}'}
        raise

    if code != 0:
        task['result'] = {'success': False, 'message': f'卸载程序启动异常，退出码 {code}'}
        raise RuntimeError(f'卸载程序启动异常，退出码 {code}')

    on_step('卸载程序已启动，请在弹出的卸载向导中按提示完成卸载。')
    _wait_until_dbeaver_uninstalled(task, on_step, purge_data)


def _wait_until_dbeaver_uninstalled(task: dict, on_step, purge_data: bool = False):
    waited = 0
    next_hint = 30
    while waited < _DBEAVER_UNINSTALL_WAIT_TIMEOUT:
        time.sleep(_DBEAVER_UNINSTALL_WAIT_INTERVAL)
        waited += _DBEAVER_UNINSTALL_WAIT_INTERVAL
        if not dbeaver_detect_installed():
            on_step('检测到 DBeaver 已从系统中移除，卸载完成。')
            if purge_data:
                _purge_dbeaver_user_data(on_step)
            tasks.set_progress(task, progress=100, stage='卸载完成')
            task['result'] = {'success': True, 'message': 'DBeaver 卸载完成'}
            log_operation('devtools', '卸载 DBeaver', detail=f'等待 {waited} 秒后检测到已卸载')
            return
        if waited >= next_hint:
            next_hint += 30
            on_step(f'仍在等待卸载向导完成...（已等待 {waited} 秒）')
            tasks.set_progress(task, stage=f'等待卸载向导完成（{waited}s）')
    on_step('等待超时：仍未检测到 DBeaver 卸载完成，请确认是否已在卸载向导中完成操作。')
    tasks.set_progress(task, progress=100, stage='等待卸载完成超时')
    task['result'] = {'success': False, 'message': '等待卸载完成超时，请手动确认 DBeaver 是否已卸载'}
    raise RuntimeError('等待 DBeaver 卸载完成超时')


def _purge_dbeaver_user_data(on_step=None):
    appdata = os.environ.get('APPDATA', '')
    data_dir = os.path.join(appdata, 'DBeaverData') if appdata else ''
    def _log(msg):
        if on_step:
            on_step(msg)
    if not data_dir or not os.path.isdir(data_dir):
        _log('用户数据目录不存在，跳过清理。')
        return True, '用户数据目录不存在，跳过清理。'
    _log(f'正在清理用户数据目录: {data_dir}')
    try:
        shutil.rmtree(data_dir, ignore_errors=True)
    except Exception as e:
        msg = f'清理时出现异常: {e}（部分文件可能被占用）'
        _log(msg)
        return False, msg
    if not os.path.exists(data_dir):
        _log('用户数据目录已清理。')
        log_operation('devtools', '清理 DBeaver 用户数据', detail=data_dir)
        return True, '用户数据已清理，下次启动 DBeaver 将恢复默认设置。'
    else:
        msg = '清理完成，但部分文件可能被占用未能删除。'
        _log(msg)
        return False, msg


@require_POST
def api_dbeaver_reset(request):
    dbeaver = get_dbeaver_options()
    if not dbeaver:
        return JsonResponse({'code': 500, 'msg': '没有找到 DBeaver 安装信息！'})
    success, message = _purge_dbeaver_user_data()
    if success:
        return JsonResponse({'code': 0, 'msg': message})
    return JsonResponse({'code': 500, 'msg': message})


@require_GET
def api_dbeaver_info_refresh(request):
    try:
        get_dbeaver_options(refresh=True)
        return JsonResponse({'code': 0, 'msg': '刷新成功'})
    except Exception as e:
        logger.warning('刷新本机 DBeaver 信息失败: %s', e)
        return JsonResponse({'code': 1, 'msg': f'刷新失败：{e}'})


@require_POST
def api_fetch_dbeaver_latest(request):
    try:
        get_dbeaver_latest_all(refresh=True)
        return JsonResponse({'code': 0, 'msg': '抓取成功'})
    except Exception as e:
        return JsonResponse({'code': 1, 'msg': f'抓取失败：{e}'})


@require_POST
def api_dbeaver_check_dir(request):
    target = (request.POST.get('path') or '').strip()
    if not target:
        return JsonResponse({'success': False, 'message': '路径不能为空'})

    p = pathlib.Path(target)
    parent = p.parent
    parent_exists = parent.exists() if parent else False

    if not parent_exists:
        return JsonResponse({
            'success': True, 'exists': False, 'is_dir': False, 'is_empty': True,
            'has_dbeaver': False, 'parent_exists': False,
            'message': f'父目录不存在：{parent}，安装时将自动创建',
            'level': 'warn',
        })

    if not p.exists():
        return JsonResponse({
            'success': True, 'exists': False, 'is_dir': False, 'is_empty': True,
            'has_dbeaver': False, 'parent_exists': True,
            'message': '目录不存在，安装时将自动创建',
            'level': 'info',
        })

    if not p.is_dir():
        return JsonResponse({
            'success': False, 'exists': True, 'is_dir': False, 'is_empty': False,
            'has_dbeaver': False, 'parent_exists': True,
            'message': '目标路径是一个文件，不是目录',
            'level': 'error',
        })

    entries = list(p.iterdir())
    is_empty = len(entries) == 0
    has_dbeaver = any('dbeaver' in (e.name or '').lower() for e in entries)

    if has_dbeaver:
        return JsonResponse({
            'success': True, 'exists': True, 'is_dir': True, 'is_empty': False,
            'has_dbeaver': True, 'parent_exists': True,
            'message': '目录中已存在 DBeaver 相关文件，继续安装可能覆盖现有版本',
            'level': 'warn',
        })

    if is_empty:
        return JsonResponse({
            'success': True, 'exists': True, 'is_dir': True, 'is_empty': True,
            'has_dbeaver': False, 'parent_exists': True,
            'message': '空目录，可以安全安装',
            'level': 'ok',
        })

    return JsonResponse({
        'success': True, 'exists': True, 'is_dir': True, 'is_empty': False,
        'has_dbeaver': False, 'parent_exists': True,
        'message': '目录非空（但未发现 DBeaver），继续安装可能与已有文件混淆',
        'level': 'warn',
    })