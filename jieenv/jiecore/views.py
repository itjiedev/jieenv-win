"""
应用管理视图
提供 Web 界面的应用管理功能：列表、安装、启用、禁用、卸载、删除、升级。

设计原则：
- 不带登录和权限限制，可在任何场景下使用
- 核心逻辑提取为独立函数，可被其他视图直接调用
- 安全性通过以下机制保障：
  * 所有写操作仅接受 POST 请求（CSRF 保护）
  * 文件上传校验类型和扩展名
  * 输入参数合法性检查
"""
import os
import time
import string
import logging
import threading
from pathlib import Path
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.views.generic.base import ContextMixin, TemplateView
from django.views.generic.list import ListView
from django.views.decorators.http import require_POST, require_GET
from django.conf import settings

from .app_manager import AppManager, restart_server, is_runserver_mode, is_platform_fixed_app
from .version_manager import (
    scan_upgrade_packages, upgrade_from_zip,
    check_platform_upgrade_available, upgrade_platform_from_zip,
)
from .menu_aggregator import refresh_menu_cache
from .cache_handler import refresh_cache_from_db
from .system_ops import (query_service_state, start_service, stop_service,
                         restart_service, open_directory, open_env_variables,
                         query_service_startup_type, set_service_startup_type)
from .op_log import log_operation, get_recent_operations
from . import tasks

logger = logging.getLogger(__name__)

# 进程启动时间戳，供前端探测服务是否已完成重启
SERVICE_START_TIME = time.time()


# ── 可复用的核心逻辑函数 ─────────────────────────────────
# 其他视图可直接导入调用，无需经过 HTTP 层

class AppMixin(ContextMixin):
    """菜单高亮 Mixin，所有业务视图都应继承此 Mixin。
    在 dispatch 中将菜单ID存到 request，供上下文处理器读取并标记激活。
    子类需设置 menu_id 和 parent_menu_id 属性。
    """
    parent_menu_id = ''
    menu_id = ''

    def dispatch(self, request, *args, **kwargs):
        request.current_menu_id = self.menu_id
        request.parent_menu_id = self.parent_menu_id
        return super().dispatch(request, *args, **kwargs)


def get_app_list_data():
    """
    获取应用列表数据（纯数据，无 HTTP 依赖）。
    返回 dict: {apps: dict, upgrade_packages: list, using_db: bool}
    可被其他视图直接调用来获取应用管理数据。
    """
    all_apps = AppManager.get_all_apps()
    upgrade_packages = scan_upgrade_packages()

    # 标记可升级状态（键名不带下划线前缀，Django 模板禁止访问下划线开头的属性）
    for pkg in upgrade_packages:
        app_name = pkg['app_name']
        if app_name in all_apps:
            all_apps[app_name]['upgrade_available'] = pkg['can_upgrade']
            all_apps[app_name]['upgrade_version'] = pkg.get('version', '')

    return {
        'apps': all_apps,
        'upgrade_packages': upgrade_packages,
    }


def do_install(zip_file_obj=None, app_path=None, zip_path=None, progress_cb=None):
    """
    安装应用，支持多种模式：
    1. zip_file_obj: Django UploadedFile 对象，从 zip 包安装
    2. zip_path: zip 包的文件系统路径
    3. app_path: Python 风格的点分路径（如 'apps.home'），安装指定路径的应用
    返回 dict: {success: bool, message: str}

    progress_cb(message, progress=None, stage=None): 可选进度回调。
    """
    if app_path:
        return AppManager.install_from_path(app_path, progress_cb=progress_cb)

    if zip_path:
        return AppManager.install_from_zip(zip_path, progress_cb=progress_cb)

    if not zip_file_obj:
        return {'success': False, 'message': '请选择要上传的 zip 文件或指定应用路径'}

    if not zip_file_obj.name.lower().endswith('.zip'):
        return {'success': False, 'message': '仅支持 .zip 格式的应用包'}

    tmp_path = settings.UPGRADE_TMP_DIR / zip_file_obj.name
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_path, 'wb') as f:
        for chunk in zip_file_obj.chunks():
            f.write(chunk)

    result = AppManager.install_from_zip(str(tmp_path), progress_cb=progress_cb)

    if tmp_path.exists():
        os.remove(tmp_path)

    return result


def do_enable(app_name):
    """启用应用，返回 dict: {success: bool, message: str}"""
    if not app_name:
        return {'success': False, 'message': '未指定应用名称'}
    return AppManager.enable_app(app_name)


def do_disable(app_name):
    """禁用应用，返回 dict: {success: bool, message: str}"""
    if not app_name:
        return {'success': False, 'message': '未指定应用名称'}
    return AppManager.disable_app(app_name)


def do_uninstall(app_name):
    """卸载应用，返回 dict: {success: bool, message: str}"""
    if not app_name:
        return {'success': False, 'message': '未指定应用名称'}
    return AppManager.uninstall_app(app_name)


def do_delete(app_name):
    """彻底删除应用，返回 dict: {success: bool, message: str}"""
    if not app_name:
        return {'success': False, 'message': '未指定应用名称'}
    return AppManager.delete_app(app_name)


def do_upgrade(app_name, zip_file_obj=None, zip_path=None, progress_cb=None):
    """
    升级应用，支持两种输入：
    1. zip_file_obj: Django UploadedFile 对象（Web 上传的升级包）
    2. zip_path: 升级 zip 包的文件系统路径（CLI 场景）
    返回 dict: {success: bool, message: str}

    progress_cb(message, progress=None, stage=None): 可选进度回调。
    """
    if not app_name:
        return {'success': False, 'message': '未指定应用名称'}

    if zip_file_obj is not None:
        if not zip_file_obj.name.lower().endswith('.zip'):
            return {'success': False, 'message': '仅支持 .zip 格式的升级包'}
        settings.UPGRADE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = settings.UPGRADE_TMP_DIR / zip_file_obj.name
        with open(tmp_path, 'wb') as f:
            for chunk in zip_file_obj.chunks():
                f.write(chunk)
        try:
            return upgrade_from_zip(app_name, str(tmp_path), progress_cb=progress_cb)
        finally:
            if tmp_path.exists():
                os.remove(tmp_path)

    if not zip_path:
        return {'success': False, 'message': '请上传升级包或指定升级包路径'}
    return upgrade_from_zip(app_name, zip_path, progress_cb=progress_cb)


def do_platform_upgrade(zip_file_obj=None, zip_path=None, progress_cb=None):
    """
    平台升级，支持两种输入：
    1. zip_file_obj: Django UploadedFile 对象（Web 上传的平台升级包）
    2. zip_path: 升级 zip 包的文件系统路径（CLI 场景）
    返回 dict: {success: bool, message: str}

    progress_cb(message, progress=None, stage=None): 可选进度回调。
    """
    if zip_file_obj is not None:
        if not zip_file_obj.name.lower().endswith('.zip'):
            return {'success': False, 'message': '仅支持 .zip 格式的升级包'}
        settings.UPGRADE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = settings.UPGRADE_TMP_DIR / zip_file_obj.name
        with open(tmp_path, 'wb') as f:
            for chunk in zip_file_obj.chunks():
                f.write(chunk)
        try:
            return upgrade_platform_from_zip(str(tmp_path), progress_cb=progress_cb)
        finally:
            if tmp_path.exists():
                os.remove(tmp_path)

    if not zip_path:
        return {'success': False, 'message': '请上传升级包或指定升级包路径'}
    return upgrade_platform_from_zip(zip_path, progress_cb=progress_cb)


def do_refresh_cache():
    """刷新缓存，返回 dict: {success: bool, message: str}"""
    try:
        refresh_cache_from_db()
        refresh_menu_cache()
        return {'success': True, 'message': '缓存已刷新'}
    except Exception as e:
        logger.error('刷新缓存失败: %s', e)
        return {'success': False, 'message': f'刷新缓存失败: {e}'}


def do_sync_apps():
    """从磁盘同步应用，返回 dict: {success: bool, message: str}"""
    try:
        AppManager.sync_from_disk()
        return {'success': True, 'message': '已从磁盘同步应用状态'}
    except Exception as e:
        logger.error('同步应用失败: %s', e)
        return {'success': False, 'message': f'同步应用失败: {e}'}


def list_fs_dirs(path: str, include_files: bool = False,
                 ext_filters: list[str] | None = None) -> dict:
    """
    列出指定目录下的子目录（纯逻辑，无 HTTP 依赖，供目录/文件选择器使用）。
    path 为空时返回盘符列表。
    include_files=True 时同时返回文件列表 files，ext_filters 为扩展名白名单
    （如 ['.zip', '.ini']，忽略大小写），缺省不限类型；此时若 path 指向单个
    文件，则返回其父目录列表并携带 selected 文件名（便于回填选中态）。
    返回 dict: {path, parent, dirs: [名称], files?: [名称], selected?: 文件名}
    目录不存在时不报错，回退为盘符列表并携带 requested（用户请求的原路径）与
    fallback=True，供前端保留输入框内容；路径不可读/无权访问时抛 ValueError。
    """
    if not path:
        dirs = [f'{letter}:\\'
                for letter in string.ascii_uppercase
                if os.path.exists(f'{letter}:\\')]
        result = {'path': '', 'parent': '', 'dirs': dirs}
        if include_files:
            result['files'] = []
        return result

    path = path.strip().strip('"')
    root = Path(path)
    selected_file = ''
    # 文件模式下允许直接定位到文件：改列其父目录并记住该文件
    try:
        if include_files and root.is_file():
            selected_file = root.name
            root = root.parent
    except (PermissionError, OSError):
        pass
    try:
        valid = root.exists() and root.is_dir()
    except (PermissionError, OSError):
        # 受限路径 stat 报错时退化为尝试列目录，由下方 iterdir 报错兼做提示
        valid = True
    if not valid:
        # 目录不存在（含用户键入的安装目标目录尚未创建）：不再报“目录不存在”错误，
        # 回退到盘符列表，并原样带回 requested 路径，供前端保留输入框内容、
        # 用户点“确定”时仍可直接采用该路径（安装时再创建目录）。
        dirs = [f'{letter}:\\'
                for letter in string.ascii_uppercase
                if os.path.exists(f'{letter}:\\')]
        result = {'path': '', 'parent': '', 'dirs': dirs,
                  'requested': path, 'fallback': True}
        if include_files:
            result['files'] = []
        return result
    try:
        entries = sorted(root.iterdir(), key=lambda e: e.name.lower())
    except NotADirectoryError:
        raise ValueError(f'不是目录: {path}')
    except (PermissionError, OSError) as e:
        raise ValueError(f'无权读取目录: {e}')

    filters = {ext.strip().lower() if ext.strip().startswith('.')
               else '.' + ext.strip().lower()
               for ext in (ext_filters or []) if ext.strip()}
    dirs, files = [], []
    for entry in entries:
        if entry.name.startswith('.'):
            continue
        try:
            if entry.is_dir():
                dirs.append(entry.name)
            elif include_files and entry.is_file():
                if not filters or any(entry.name.lower().endswith(ext) for ext in filters):
                    files.append(entry.name)
        except (PermissionError, OSError):
            continue
    parent = str(root.parent) if str(root.parent) != str(root) else ''
    result = {'path': str(root), 'parent': parent, 'dirs': dirs}
    if include_files:
        result['files'] = files
        if selected_file:
            result['selected'] = selected_file
    return result


# ── JSON API 层（供前端 AJAX 调用） ────────────────────────

@require_GET
def api_ping(request):
    """服务存活探测，返回进程启动时间戳供前端判断是否重启完成"""
    return JsonResponse({'status': 'ok', 'start_time': SERVICE_START_TIME})


@require_GET
def api_fs_dirs(request):
    """列出目录下的子目录（供通用目录选择器使用），path 为空时返回盘符列表。
    参数: files=1 时同时返回文件列表；exts=.zip,.ini 按扩展名过滤文件"""
    include_files = request.GET.get('files') == '1'
    exts = [e.strip() for e in request.GET.get('exts', '').split(',') if e.strip()]
    try:
        return JsonResponse({'success': True,
                             **list_fs_dirs(request.GET.get('path', ''),
                                            include_files=include_files,
                                            ext_filters=exts or None)})
    except ValueError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    except Exception as e:
        logger.exception('列目录失败')
        return JsonResponse({'success': False, 'message': f'列目录失败: {e}'})


@require_POST
def api_save_restart_url(request):
    """保存重启前的页面 URL，供桌面壳重启后导航回原页面。

    前端在触发重启前调用此接口，将当前 window.location.href 写入临时文件；
    桌面壳的子进程重启协调器读取该文件后，导航到保存的 URL 而非首页。
    纯 Web 模式下此文件无影响（前端轮询完成后自行跳转）。
    """
    url = request.POST.get('url', '').strip()
    if not url or not url.startswith('http'):
        return JsonResponse({'success': False, 'message': '无效的 URL'})
    restart_url_file = os.path.join(
        getattr(settings, 'BASE_DIR', Path(__file__).resolve().parent.parent),
        'tmp', 'restart_url.txt')
    try:
        os.makedirs(os.path.dirname(restart_url_file), exist_ok=True)
        with open(restart_url_file, 'w', encoding='utf-8') as f:
            f.write(url)
    except OSError as e:
        logger.warning('保存重启 URL 失败: %s', e)
    return JsonResponse({'success': True})


@require_POST
def api_restart(request):
    """后台重启服务：先返回响应，延迟后重启进程。

    具体的重启语义一律交给 app_manager.restart_server()（内部再分发给入口
    注册的策略）；此处只负责"先把响应发回去再动手"，避免连接被掐断。
    runserver 模式下 restart_server 会终止进程等人工重启，前端遮罩轮询等待恢复。
    """
    if is_runserver_mode():
        logger.info('收到前端重启请求，当前为 runserver 模式，将自动终止进程等待手动重启')
        # 不能在这里自己 os._exit：runserver 的终止时序已统一收敛到
        # restart_server，两处各写一份会造成“谁是真相”的歧义
        threading.Timer(1.5, restart_server).start()
        return JsonResponse({
            'success': False,
            'manual_restart_required': True,
            'message': '当前服务以 runserver 方式运行，服务将自动停止，请手动重新启动',
        })
    logger.info('收到前端重启请求，服务即将重启')
    threading.Timer(1.0, restart_server).start()
    return JsonResponse({'success': True, 'message': '服务正在重启'})


@require_POST
def api_fs_open_dir(request):
    """在资源管理器中打开目录（通用能力，供各应用前端调用）。
    参数: path=目录全路径"""
    return JsonResponse(open_directory(request.POST.get('path', '')))


@require_GET
def api_service_state(request):
    """查询 Windows 服务状态（通用能力）。
    参数: name=服务名；返回 {success, exists, running}"""
    state = query_service_state(request.GET.get('name', ''))
    return JsonResponse({'success': True, **state})


@require_POST
def api_service_action(request, action):
    """启停 Windows 服务（通用能力，action=start/stop/restart）。
    参数: name=服务名；返回 {success, message}"""
    handlers = {'start': start_service, 'stop': stop_service,
                'restart': restart_service}
    handler = handlers.get(action)
    if not handler:
        return JsonResponse({'success': False, 'message': f'未知操作: {action}'})
    return JsonResponse(handler(request.POST.get('name', '')))

@require_POST
def api_service_startup(request):
    name = request.POST.get('name', '')
    start_type = request.POST.get('start_type', '')   # auto|delayed-auto|demand|disabled
    return JsonResponse(set_service_startup_type(name, start_type))

@require_GET
def api_service_startup_state(request):
    name = request.GET.get('name', '')
    return JsonResponse(query_service_startup_type(name))


@require_GET
def api_task_status(request, task_id):
    """轮询后台任务状态与增量日志（通用能力，供各应用前端调用）。
    参数: after=上次返回的 log_index，用于增量拉取日志
    返回 {success, status, progress, stage, logs, log_index, result}，与
    static/js/jiecore_tasklog.js 的约定一致，前端 JieTaskLog 可直接消费。

    任务统一登记在 jiecore.tasks，故各应用不必再实现同构的轮询视图；应用侧
    已有的同名接口（如 dbmysql 的 api_task_status / api_install_status）保留原
    路由以免改动模板，内部改为复用 tasks.task_payload()，返回体完全一致。
    """
    task = tasks.get_task(task_id)
    if not task:
        return JsonResponse({'success': False, 'message': '任务不存在'})
    return JsonResponse(tasks.task_payload(task, request.GET.get('after', 0)))


@require_GET
def api_task_list(request):
    """返回活跃 + 近期已完成任务摘要列表，供全局任务指示器（JieTaskBar）轮询。

    返回 {success: true, tasks: [{task_id, type, title, status, progress,
    stage, meta, result, created_at}, ...]}
    """
    return JsonResponse({'success': True, 'tasks': tasks.list_tasks()})


@require_POST
def api_app_action(request, app_name, action):
    """
    应用操作统一接口（enable/disable/uninstall/delete/upgrade）。
    返回 JSON: {success, message, restart_required}
    """
    if is_platform_fixed_app(app_name):
        return JsonResponse({
            'success': False,
            'message': f'「{app_name}」属于平台骨架应用，请通过平台升级统一更新。',
        })
    _action_labels = {
        'enable': '启用', 'disable': '禁用',
        'uninstall': '卸载', 'delete': '彻底删除', 'upgrade': '升级',
    }
    handlers = {
        'enable': do_enable,
        'disable': do_disable,
        'uninstall': do_uninstall,
        'delete': do_delete,
    }
    if action == 'upgrade':
        zip_file = request.FILES.get('zip_file')
        if not zip_file:
            return JsonResponse({'success': False, 'message': '请选择升级包 zip 文件'})
        if not zip_file.name.lower().endswith('.zip'):
            return JsonResponse({'success': False, 'message': '仅支持 .zip 格式的升级包'})
        settings.UPGRADE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = settings.UPGRADE_TMP_DIR / zip_file.name
        with open(tmp_path, 'wb') as f:
            for chunk in zip_file.chunks():
                f.write(chunk)
        tmp_path_str = str(tmp_path)

        task = tasks.create_task(
            'app_upgrade',
            meta={'app_name': app_name, 'zip': tmp_path_str},
            title=f'升级应用 {app_name}',
        )

        def _worker(task, _app=app_name, _zip=tmp_path_str):
            def _cb(message, progress=None, stage=None):
                tasks.append_log(task, message)
                tasks.set_progress(task, progress=progress, stage=stage)
            result = do_upgrade(_app, zip_path=_zip, progress_cb=_cb)
            result.setdefault('restart_required', False)
            tasks.set_result(task, result)

        tasks.run_in_thread(task, _worker)
        return JsonResponse({'success': True, 'task_id': task['task_id']})
    elif action in handlers:
        result = handlers[action](app_name)
    else:
        result = {'success': False, 'message': f'未知操作: {action}'}
    result.setdefault('restart_required', False)
    # 记录操作日志
    label = _action_labels.get(action, action)
    if result.get('success'):
        log_operation('appmanage', f'{label}应用 {app_name}')
    else:
        log_operation('appmanage', f'{label}应用 {app_name}', success=False,
                      detail=result.get('message', ''))
    return JsonResponse(result)


@require_POST
def api_platform_upgrade(request):
    """
    平台升级接口：支持上传 platform-version.zip 或直接指定 zip_path。
    返回 JSON {task_id} 供前端 JieTaskLog 轮询。
    """
    zip_file = request.FILES.get('zip_file')
    zip_path = request.POST.get('zip_path', '').strip()

    tmp_path_str = None
    if zip_file:
        if not zip_file.name.lower().endswith('.zip'):
            return JsonResponse({'success': False, 'message': '仅支持 .zip 格式的升级包'})
        settings.UPGRADE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = settings.UPGRADE_TMP_DIR / zip_file.name
        with open(tmp_path, 'wb') as f:
            for chunk in zip_file.chunks():
                f.write(chunk)
        tmp_path_str = str(tmp_path)
    elif zip_path:
        tmp_path_str = zip_path
    else:
        return JsonResponse({'success': False, 'message': '请上传升级包或指定升级包路径'})

    task = tasks.create_task(
        'platform_upgrade',
        meta={'zip': tmp_path_str},
        title='平台升级',
    )

    def _worker(task, _zip=tmp_path_str):
        def _cb(message, progress=None, stage=None):
            tasks.append_log(task, message)
            tasks.set_progress(task, progress=progress, stage=stage)
        result = do_platform_upgrade(zip_path=_zip, progress_cb=_cb)
        result.setdefault('restart_required', False)
        tasks.set_result(task, result)

    tasks.run_in_thread(task, _worker)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


@require_POST
def api_refresh_cache(request):
    """重建缓存接口，返回 JSON: {success, message}"""
    return JsonResponse(do_refresh_cache())


@require_POST
def api_app_install(request):
    """安装应用接口：支持 zip 上传或 Python 路径，返回 JSON {task_id} 供前端 JieTaskLog 轮询。"""
    zip_file = request.FILES.get('zip_file')
    app_path = request.POST.get('app_path', '').strip() or None

    tmp_path_str = None
    if zip_file:
        if not zip_file.name.lower().endswith('.zip'):
            return JsonResponse({'success': False, 'message': '仅支持 .zip 格式的应用包'})
        settings.UPGRADE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = settings.UPGRADE_TMP_DIR / zip_file.name
        with open(tmp_path, 'wb') as f:
            for chunk in zip_file.chunks():
                f.write(chunk)
        tmp_path_str = str(tmp_path)

    task = tasks.create_task(
        'app_install',
        meta={'app_path': app_path, 'zip': tmp_path_str},
        title=f'安装应用 {app_path or (zip_file.name if zip_file else "")}',
    )

    def _worker(task):
        def _cb(message, progress=None, stage=None):
            tasks.append_log(task, message)
            tasks.set_progress(task, progress=progress, stage=stage)

        if app_path:
            result = do_install(app_path=app_path, progress_cb=_cb)
        elif tmp_path_str:
            result = do_install(zip_path=tmp_path_str, progress_cb=_cb)
        else:
            result = {'success': False, 'message': '请选择要上传的 zip 文件或指定应用路径'}
        result.setdefault('restart_required', False)
        tasks.set_result(task, result)

    tasks.run_in_thread(task, _worker)
    return JsonResponse({'success': True, 'task_id': task['task_id']})

@require_GET
def api_open_dir(request):
    import os
    path = request.GET.get('savedir', '')
    path = path.replace('/', '\\')
    os.startfile(path)
    return JsonResponse({"code": 200, "msg": "已打开指定文件夹!"})


@require_POST
def api_open_env_variables(request):
    """打开 Windows 系统环境变量编辑对话框（公共能力，供各应用前端调用）"""
    return JsonResponse(open_env_variables())


# ── 操作日志管理 ──────────────────────────────────────────

class OperationLogListView(AppMixin, ListView):
    """操作日志列表页，支持分页。"""
    template_name = 'jiecore/operation_log_list.html'
    context_object_name = 'operations'
    paginate_by = 20

    def get_queryset(self):
        from .models import OperationLog
        return OperationLog.objects.order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '操作日志'
        return context


@require_GET
def api_op_log_detail(request, log_id):
    """查询单条操作日志详情。"""
    try:
        from .models import OperationLog
        obj = OperationLog.objects.filter(pk=log_id).first()
        if not obj:
            return JsonResponse({'success': False, 'message': '记录不存在'})
        return JsonResponse({
            'success': True,
            'action': obj.action,
            'detail': obj.detail,
        })
    except Exception as e:
        logger.warning('查询操作日志详情失败: %s', e)
        return JsonResponse({'success': False, 'message': f'查询失败: {e}'})


@require_POST
def api_op_log_delete(request, log_id):
    """删除单条操作日志。"""
    try:
        from .models import OperationLog
        obj = OperationLog.objects.filter(pk=log_id).first()
        if not obj:
            return JsonResponse({'success': False, 'message': '记录不存在'})
        obj.delete()
        return JsonResponse({'success': True, 'message': '已删除'})
    except Exception as e:
        logger.warning('删除操作日志失败: %s', e)
        return JsonResponse({'success': False, 'message': f'删除失败: {e}'})


@require_POST
def api_op_log_clear(request):
    """清空全部操作日志。"""
    try:
        from .models import OperationLog
        count, _ = OperationLog.objects.all().delete()
        return JsonResponse({'success': True, 'message': f'已清空 {count} 条记录'})
    except Exception as e:
        logger.warning('清空操作日志失败: %s', e)
        return JsonResponse({'success': False, 'message': f'清空失败: {e}'})


class AppListView(AppMixin, ListView):
    """应用列表页（含升级包展示）。"""
    template_name = 'jiecore/app_list.html'
    menu_id = 'app_list'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '应用管理'
        data = get_app_list_data()
        context['apps'] = data['apps']
        context['upgrade_packages'] = data['upgrade_packages']
        from .version_manager import _get_platform_version
        context['platform_version'] = _get_platform_version()
        return context

    def get_queryset(self):
        return AppManager.get_all_apps()


def _redirect_app_list(request, result, success_msg=None):
    """通用：执行操作后重定向回应用列表页。"""
    if result.get('success'):
        messages.success(request, success_msg or result.get('message', '操作成功'))
    else:
        messages.error(request, result.get('message', '操作失败'))
    return redirect('jiecore:app_list')


@require_POST
def app_sync_apps(request):
    """POST 从磁盘同步应用。"""
    return _redirect_app_list(request, do_sync_apps(), '已从磁盘同步应用')


@require_POST
def app_refresh_cache(request):
    """POST 刷新缓存。"""
    return _redirect_app_list(request, do_refresh_cache(), '缓存已刷新')


@require_POST
def app_enable(request, app_name):
    return _redirect_app_list(request, do_enable(app_name))


@require_POST
def app_disable(request, app_name):
    return _redirect_app_list(request, do_disable(app_name))


@require_POST
def app_uninstall(request, app_name):
    return _redirect_app_list(request, do_uninstall(app_name))


@require_POST
def app_delete(request, app_name):
    return _redirect_app_list(request, do_delete(app_name))


@require_POST
def app_install(request):
    """POST 安装应用（支持 zip 上传和 Python 路径）。"""
    zip_file_obj = request.FILES.get('zip_file')
    app_path = request.POST.get('app_path', '').strip() or None
    return _redirect_app_list(
        request,
        do_install(zip_file_obj=zip_file_obj, app_path=app_path),
        '应用安装完成',
    )


class PlatformUpgradeView(AppMixin, TemplateView):
    """平台升级页面：独立页面展示当前版本 + zip 上传 + 扫描到的 platform 升级包 + 更新日志。"""
    template_name = 'jiecore/platform_upgrade.html'
    menu_id = 'platform_upgrade'

    def get_context_data(self, **kwargs):
        from .version_manager import _get_platform_version, _get_platform_changelog_text
        context = super().get_context_data(**kwargs)
        context['page_title'] = '平台升级'
        context['platform_version'] = _get_platform_version()
        context['upgrade_packages'] = [
            p for p in scan_upgrade_packages() if p['app_name'] == 'platform'
        ]
        context['platform_changelog'] = _get_platform_changelog_text()
        return context


@require_GET
def root_view(request):
    """根路径欢迎页（无根应用时展示，有根应用时其路由优先匹配）。"""
    return render(request, 'root_view.html', {'page_title': '欢迎'})