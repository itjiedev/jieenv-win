"""
dbmysql 视图层

页面视图:
- IndexView          首页：MySQL 实例列表（手动添加/安装登记），登录入口
- InstanceAddView    添加实例页：选择安装目录，自动解析并登记
- InstallView        安装页：抓取官网版本列表（zip/MSI 直链），选版本跳安装选项页
- InstallOptionsView 安装选项页：配置安装参数并启动自动安装
- LoginView          登录页：用户名密码登录 MySQL 实例
- DatabasesView      库列表页 / DatabaseCreateView 建库页
- TablesView         表列表页 / TableCreateView 建表页
- ColumnsView        字段列表页 / ColumnAddView 添加字段页 / ColumnEditView 编辑字段页

JSON API（前端 AJAX 调用，POST 带 CSRF）:
- api_instance_add / api_instance_remove / api_instance_start
- api_instance_service_task / api_task_status（启停/重启任务式执行，逐步进度日志）
- api_versions / api_install_start / api_install_status
- api_login / api_logout / api_change_password / api_instance_conn_clear
- api_db_action / api_table_action / api_column_action（需已登录连接）

性能约定：
- 页面视图（尤其首页 IndexView）不做同步端口探测，避免 TCP 超时拖慢首屏；
- 运行状态与失效连接清理放在异步接口 api_instance_status 中完成；
- ConnRequiredMixin 进入连接页时做一次带缓存的快速探测兜底。
"""
import json
import os
import re
import shutil
import threading
import time
import pathlib
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

from django.urls import reverse
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic.base import TemplateView
from django.views.decorators.http import require_POST, require_GET

from jiecore.views import AppMixin
from jiecore.op_log import log_operation
from jiecore.system_ops import (query_service_state, start_service,
                                  stop_service, restart_service,
                                  open_directory, run_elevated,
                                  query_service_startup_type,
                                  set_service_startup_type)
from jiecore.utils import get_cmd_encoding
# 后台任务注册表已提升为 jiecore 公共能力（原本应用与 lang_python 等各自
# 维护一份同构实现），各应用共用同一份注册表，任务在通用轮询接口与本应用
# 路由之间互通
from jiecore import tasks
from .mysql_service import register_mysql_service, remove_mysql_service

from . import storage
from . import config as app_config
from . import mysql_client as mc
from .mysql_resolve import (resolve_instance, ResolveError, check_port, find_my_ini,
                              find_msi_product_code, msi_uninstall_product)
from .mysql_versions import get_mysql_versions, build_version_entry
from . import mysql_installer
from .mysql_process import (locate_mysqld_process, terminate_process,
                              wait_port_ready, wait_port_released, launch_mysqld,
                              process_exists, START_READY_TIMEOUT, STOP_CONFIRM_TIMEOUT,
                              GRACE_PERIOD, MIN_FORCE_CONFIRM)

logger = logging.getLogger(__name__)

# session 中保存已登录连接的键。
# 注意：session 里只保存「非敏感」元信息（host/port/user/name/version/instance_id），
# 密码保存在进程内存缓存 _CONN_SECRETS 中，避免明文随 session 落到数据库/磁盘。
SESSION_CONNS_KEY = 'dbmysql_conns'

SOURCE_DISPLAY = {'installed': '应用安装', 'manual': '手动添加', 'remote': '远程连接'}


# ── 连接密码的内存缓存 ───────────────────────────────────
# 以 conn_id 为键保存密码，超过 TTL 未访问自动过期；进程重启后全部失效，
# 用户重新登录即可（这是预期的安全行为）。
_CONN_SECRETS: dict = {}
_CONN_SECRETS_LOCK = threading.Lock()
_CONN_SECRET_TTL = 8 * 3600  # 8 小时


def _set_secret(conn_id: str, password: str):
    """写入/更新连接密码（内存）。"""
    with _CONN_SECRETS_LOCK:
        _CONN_SECRETS[conn_id] = {'password': password, 'ts': time.time()}


def _get_secret(conn_id: str):
    """读取连接密码；过期或不存在返回 None，读取成功会刷新 TTL。"""
    with _CONN_SECRETS_LOCK:
        item = _CONN_SECRETS.get(conn_id)
        if not item:
            return None
        if time.time() - item['ts'] > _CONN_SECRET_TTL:
            _CONN_SECRETS.pop(conn_id, None)
            return None
        item['ts'] = time.time()
        return item['password']


def _drop_secret(conn_id: str):
    with _CONN_SECRETS_LOCK:
        _CONN_SECRETS.pop(conn_id, None)


def _drop_secrets(conn_ids):
    with _CONN_SECRETS_LOCK:
        for cid in conn_ids:
            _CONN_SECRETS.pop(cid, None)


def _gc_secrets():
    """清理过期密码缓存（随会话写入顺带执行，无需定时器）。"""
    now = time.time()
    with _CONN_SECRETS_LOCK:
        expired = [cid for cid, v in _CONN_SECRETS.items()
                   if now - v['ts'] > _CONN_SECRET_TTL]
        for cid in expired:
            _CONN_SECRETS.pop(cid, None)


# ── 端口可达性探测（带短缓存 + 并发批量） ────────────────

_PORT_CACHE: dict = {}
_PORT_CACHE_LOCK = threading.Lock()
_PORT_CACHE_TTL = 3.0


def _port_reachable(host: str, port: int, use_cache: bool = True) -> bool:
    """带 3 秒缓存的端口可达探测，用于视图/校验路径，避免重复 TCP 探测。

    use_cache=False 时强制实时探测（用于状态查询接口，用户希望看到最新状态）。
    """
    key = (host or '127.0.0.1', int(port or 3306))
    now = time.time()
    if use_cache:
        with _PORT_CACHE_LOCK:
            hit = _PORT_CACHE.get(key)
            if hit and now - hit[0] < _PORT_CACHE_TTL:
                return hit[1]
    ok = check_port(key[0], key[1])
    with _PORT_CACHE_LOCK:
        _PORT_CACHE[key] = (now, ok)
        if len(_PORT_CACHE) > 256:
            for k in [k for k, v in _PORT_CACHE.items()
                      if now - v[0] > _PORT_CACHE_TTL]:
                _PORT_CACHE.pop(k, None)
    return ok


def _invalidate_port_cache(host: str = '', port=None):
    """启停/改端口后立即失效缓存，保证界面状态实时。"""
    with _PORT_CACHE_LOCK:
        if port is None:
            _PORT_CACHE.clear()
        else:
            _PORT_CACHE.pop((host or '127.0.0.1', int(port)), None)


def _batch_port_status(instances, use_cache: bool = True) -> dict:
    """并发探测多个实例端口可达性，返回 {instance_id: bool}。

    use_cache=False 用于状态查询接口（要看到最新状态）；
    use_cache=True 用于校验/进入页面路径（减少重复探测）。
    """
    if not instances:
        return {}

    def _check(inst):
        return _port_reachable(inst.get('host', '127.0.0.1'),
                               int(inst.get('port') or 3306),
                               use_cache=use_cache)

    with ThreadPoolExecutor(max_workers=min(len(instances), 8)) as pool:
        results = list(pool.map(_check, instances))
    return {inst.get('id', ''): ok for inst, ok in zip(instances, results)}


def _is_port_occupied(port: int, exclude_instance_id: str = '') -> str:
    """检查端口是否被占用（运行中进程 + 已保存实例记录）。
    返回占用原因描述，未占用返回空串。"""
    # 1. 检查运行中的进程（此处必须实时，不能走缓存）
    if check_port('127.0.0.1', port):
        return f'端口 {port} 已被运行中的程序占用'
    # 2. 检查已保存的实例记录
    for inst in storage.list_instances():
        if inst.get('id') == exclude_instance_id:
            continue
        if int(inst.get('port') or 0) == port:
            return f'端口 {port} 已被已登记实例「{inst.get("name", "")}」使用'
    return ''


def _find_free_port(start: int = 3306, exclude_instance_id: str = '') -> int:
    """从 start 开始向上寻找可用端口，直到无冲突（最多尝试 100 个）"""
    for p in range(start, min(start + 100, 65536)):
        if not _is_port_occupied(p, exclude_instance_id):
            return p
    return start  # 兜底，理论上不会走到


def _build_service_instance_name(version: str, service_name: str) -> str:
    """注册服务后更新实例名称（去掉"进程模式"等标记）"""
    ver = (version or '').replace('.', '')
    return f'MySQL {ver} ({service_name})' if ver else f'MySQL ({service_name})'


class DbmysqlMixin(AppMixin):
    parent_menu_id = 'db'
    menu_id = 'dbmysql'


class ConnRequiredMixin:
    """会话连接校验 Mixin：conn_id 无效、或实例端口已不可达时回首页（置于 MRO 最前）

    端口不可达意味着实例已停止 / 进程已退出 / 端口被改，此时会话连接即失效，
    顺手把它从会话中清理掉，避免页面带着失效连接进入后报错。
    这里的探测走 3 秒缓存，不拖慢页面打开速度。
    """

    def get(self, request, *args, **kwargs):
        conn_id = kwargs['conn_id']
        info = get_conn_info(request, conn_id)
        if not info:
            return redirect('dbmysql:index')
        if not _port_reachable(info.get('host', '127.0.0.1'),
                               int(info.get('port') or 3306)):
            conns = _get_conns(request)
            conns.pop(conn_id, None)
            _save_conns(request, conns)
            _drop_secret(conn_id)
            return redirect('dbmysql:index')
        return super().get(request, *args, **kwargs)


# ── 会话连接管理 ─────────────────────────────────────────

def _get_conns(request) -> dict:
    session = getattr(request, 'session', None)
    if session is None:
        return {}
    return session.get(SESSION_CONNS_KEY) or {}


def _save_conns(request, conns: dict):
    request.session[SESSION_CONNS_KEY] = conns
    request.session.modified = True
    _gc_secrets()


def get_conn_info(request, conn_id: str):
    """返回连接元信息（不含密码）；连接无效时返回 None 并顺手清理会话记录。"""
    conns = _get_conns(request)
    info = conns.get(conn_id)
    if not info:
        return None
    if _get_secret(conn_id) is None:
        # 进程重启 / 密码缓存过期 → 连接已失效
        conns.pop(conn_id, None)
        _save_conns(request, conns)
        return None
    return info


def _clear_instance_conns(request, instance_id: str) -> int:
    """清除会话中某实例的全部连接（含内存密码缓存），返回清理数量。"""
    if not instance_id:
        return 0
    conns = _get_conns(request)
    stale = [cid for cid, info in conns.items()
             if info.get('instance_id') == instance_id]
    if stale:
        for cid in stale:
            conns.pop(cid, None)
        _save_conns(request, conns)
        _drop_secrets(stale)
    return len(stale)


# ── 页面视图 ─────────────────────────────────────────────

class IndexView(DbmysqlMixin, TemplateView):
    """首页：实例列表（已连接实例显示“管理”，未连接显示“登录”）

    性能约定：本视图不做任何端口探测（TCP 探测会拖慢首屏渲染，远程 host
    或端口未监听时可能等数百毫秒至数秒）。运行状态由页面加载后异步调用
    api_instance_status 回填；失效连接的清理也在该异步接口里完成。

    这里只做零成本的纯内存清理：实例登记记录已被移除的会话连接直接剔除。
    """
    template_name = 'dbmysql/index.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'MySQL 数据库管理'

        instances = storage.list_instances()
        valid_ids = {i.get('id') for i in instances if i.get('id')}

        # 零 IO 清理：实例记录已不存在的会话连接直接剔除（纯内存操作）
        conns = _get_conns(self.request)
        stale = [cid for cid, info in conns.items()
                 if info.get('instance_id') and info['instance_id'] not in valid_ids]
        if stale:
            for cid in stale:
                conns.pop(cid, None)
            _save_conns(self.request, conns)
            _drop_secrets(stale)

        # 构建已连接实例映射：instance_id → conn_id
        connected_map = {}
        for cid, info in conns.items():
            iid = info.get('instance_id', '')
            if iid:
                connected_map[iid] = cid

        for item in instances:
            item['source_display'] = SOURCE_DISPLAY.get(item.get('source'), '未知')
            item['conn_id'] = connected_map.get(item.get('id', ''), '')
            item['can_operate'] = _is_local_host(item.get('host', '127.0.0.1'))
        context['instances'] = instances
        return context


@require_GET
def api_instance_status(request):
    """异步查询全部实例运行状态（线程池并发 + 实时探测），并顺手清理本会话失效连接。

    列表页状态列先渲染加载动画，页面打开后再调本接口回填徽章，避免同步
    探测阻塞首屏渲染。本接口同时承担“服务已停/进程退出/实例已删”的会话
    连接清理职责，返回 cleared 列出被清理的 conn_id。

    采用实时探测（use_cache=False），保证用户点刷新/切回页面看到的是当前
    状态而非 3 秒内缓存。
    """
    instances = storage.list_instances()
    statuses = _batch_port_status(instances, use_cache=False)
    by_id = {i.get('id'): i for i in instances}

    # 清理失效会话连接：实例已删除，或端口不可达
    conns = _get_conns(request)
    cleared = []
    for cid, info in list(conns.items()):
        iid = info.get('instance_id', '')
        if not iid:
            continue
        if iid not in by_id or not statuses.get(iid, False):
            cleared.append(cid)
            conns.pop(cid, None)
    if cleared:
        _save_conns(request, conns)
        _drop_secrets(cleared)

    return JsonResponse({'success': True, 'statuses': statuses,
                         'cleared': cleared})


class InstanceAddView(DbmysqlMixin, TemplateView):
    """添加实例页：选择安装目录，自动解析版本/端口等信息，确认后登记"""
    template_name = 'dbmysql/instance_add.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = '添加 MySQL 实例'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '添加实例',
                'href': None,
                'active': True
            },
        ]
        return context


class InstallView(DbmysqlMixin, TemplateView):
    """安装页：版本列表抓取，展示 zip/MSI 直链，点安装跳安装选项页"""
    template_name = 'dbmysql/install.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = '安装 MySQL - 版本列表'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse(f'dbmysql:index'),
                'active': False
            },
            {
                'title': '安装 MySQL - 版本列表',
                'href': '',
                'active': True
            },
        ]
        return context


# 安装选项页 version 参数格式校验
_VERSION_PARAM_RE = re.compile(r'^\d+\.\d+\.\d+$')

# Windows 服务名格式（与 mysql_installer.validate_options 保持一致）
_SERVICE_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_\-]{0,31}$')


class InstallOptionsView(DbmysqlMixin, TemplateView):
    """安装选项页：从版本列表带 version/channel 参数进入，配置后启动自动安装"""
    template_name = 'dbmysql/install_options.html'

    def get(self, request, *args, **kwargs):
        if not _VERSION_PARAM_RE.fullmatch(request.GET.get('version', '')):
            return redirect('dbmysql:install')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        version = self.request.GET.get('version', '')
        channel = self.request.GET.get('channel', '')
        context['entry'] = build_version_entry(version, is_current=(channel == 'current'))
        context['page_title'] = f'安装 MySQL {version}'
        context['default_install_dir'] = str(mysql_installer._get_default_install_root())
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse(f'dbmysql:index'),
                'active': False
            },
            {
                'title': '安装 MySQL - 版本列表',
                'href': reverse(f'dbmysql:install'),
                'active': False
            },
            {
                'title': '安装 MySQL - 设置',
                'href': None,
                'active': True
            },
        ]
        return context


class InstanceDetailView(DbmysqlMixin, TemplateView):
    """实例详情页：全部实例信息 + 服务启停/注册 + 打开目录 + 编辑配置文件"""
    template_name = 'dbmysql/instance_detail.html'

    def get(self, request, *args, **kwargs):
        if not storage.get_instance(kwargs['instance_id']):
            return redirect('dbmysql:index')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        instance = storage.get_instance(kwargs['instance_id'])
        # 详情页需要准确状态，实时探测一次（详情页单实例，耗时可控）
        instance['reachable'] = _port_reachable(instance.get('host', '127.0.0.1'),
                                                instance.get('port', 3306),
                                                use_cache=False)
        instance['source_display'] = SOURCE_DISPLAY.get(instance.get('source'), '未知')
        # 实时查询服务状态（服务可能已被外部启动/停止/删除）
        service_state = None
        if instance.get('service_name'):
            service_state = query_service_state(instance['service_name'])
        # 注册服务时的默认服务名建议
        version = (instance.get('version') or '').replace('.', '')
        context['default_service_name'] = (f'MySQL{version}' if version
                                           else f"MySQL{instance.get('port', 3306)}")
        context['page_title'] = f"实例详情 - {instance['name']}"
        context['instance'] = instance
        context['service_state'] = service_state
        context['is_remote'] = instance.get('source') == 'remote'
        conns = _get_conns(self.request)
        matched_conn_id = ''
        matched_info = None
        for cid, info in conns.items():
            if info.get('instance_id') == kwargs['instance_id']:
                matched_conn_id = cid
                matched_info = info
                break
        context['matched_conn_id'] = matched_conn_id
        context['matched_conn_info'] = matched_info
        context['can_operate'] = _is_local_host(instance.get('host', '127.0.0.1'))
        # 实时查询服务启动类型（仅服务模式实例；SCM 为唯一真相源，不读存储字段）
        start_type_info = None
        if instance.get('service_name') and instance.get('run_mode') == 'service':
            start_type_info = query_service_startup_type(instance['service_name'])
        context['start_type_info'] = start_type_info
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse(f'dbmysql:index'),
                'active': False
            },
            {
                'title': '实例详情',
                'href': None,
                'active': True
            },
        ]
        return context


class InstanceConfigView(DbmysqlMixin, TemplateView):
    """实例配置文件编辑页：在线编辑 my.ini 等配置文件"""
    template_name = 'dbmysql/instance_config.html'

    def get(self, request, *args, **kwargs):
        if not storage.get_instance(kwargs['instance_id']):
            return redirect('dbmysql:index')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        instance = storage.get_instance(kwargs['instance_id'])
        context['page_title'] = f"编辑配置文件 - {instance['name']}"
        context['instance'] = instance
        # 查找配置文件
        ini_path = find_my_ini(instance.get('install_dir', ''),
                               instance.get('data_dir', ''))
        context['ini_path'] = str(ini_path) if ini_path else ''
        context['ini_content'] = ''
        context['ini_error'] = ''
        if ini_path and ini_path.exists():
            try:
                context['ini_content'] = ini_path.read_text(encoding='utf-8', errors='replace')
            except Exception as e:
                context['ini_error'] = f'读取配置文件失败: {e}'
        elif not ini_path:
            context['ini_error'] = '未找到配置文件（my.ini），请确认实例安装目录正确'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '实例详情',
                'href': reverse('dbmysql:instance_detail', args=[kwargs['instance_id']]),
                'active': False
            },
            {
                'title': '编辑配置文件',
                'href': None,
                'active': True
            },
        ]
        return context


class LoginView(DbmysqlMixin, TemplateView):
    """登录页：支持从首页实例带参进入预填"""
    template_name = 'dbmysql/login.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = '登录 MySQL'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse(f'dbmysql:index'),
                'active': False
            },
            {
                'title': '登录',
                'href': None,
                'active': True
            },
        ]
        instance_id = self.request.GET.get('instance', '')
        instance = storage.get_instance(instance_id) if instance_id else None
        if instance:
            context['instance'] = instance
            context['form_host'] = instance.get('host', '127.0.0.1')
            context['form_port'] = instance.get('port', 3306)
            context['form_user'] = instance.get('default_user', 'root')
        else:
            context['form_host'] = self.request.GET.get('host', '127.0.0.1')
            context['form_port'] = self.request.GET.get('port', '3306')
            context['form_user'] = self.request.GET.get('user', 'root')
        return context


def _require_conn(view_func):
    """JSON API 装饰器：校验 conn_id 存在于会话中"""
    def _wrapper(request, conn_id, *args, **kwargs):
        if not get_conn_info(request, conn_id):
            return JsonResponse({'success': False, 'message': '连接不存在或已过期，请重新登录'})
        return view_func(request, conn_id, *args, **kwargs)
    return _wrapper


def _open_conn(request, conn_id: str, db: str = ''):
    """按会话连接信息建立 MySQL 连接（密码取自内存缓存，不落 session）"""
    info = get_conn_info(request, conn_id)
    if not info:
        raise mc.MysqlError('连接不存在或已过期，请重新登录')
    password = _get_secret(conn_id)
    if password is None:
        raise mc.MysqlError('连接凭据已过期，请重新登录')
    return mc.connect(info['host'], info['port'], info['user'], password, db=db)


class DatabasesView(ConnRequiredMixin, DbmysqlMixin, TemplateView):
    """库列表页"""
    template_name = 'dbmysql/databases.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        from .mysql_client import SYS_DB

        context = super().get_context_data(**kwargs)
        conn_id = kwargs['conn_id']
        info = get_conn_info(self.request, conn_id)
        context['page_title'] = f"数据库管理 - {info['host']}:{info['port']}"
        context['sys_db'] = SYS_DB
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse(f'dbmysql:index'),
                'active': False
            },
            {
                'title': '数据库管理',
                'href': None,
                'active': True
            },
        ]
        context['conn_id'] = conn_id
        context['conn_info'] = info
        try:
            conn = _open_conn(self.request, conn_id)
            try:
                context['databases'] = mc.list_databases(conn)
                context['server_version'] = mc.get_server_version(conn)
            finally:
                conn.close()
        except mc.MysqlError as e:
            context['db_error'] = str(e)
            context['databases'] = []
        return context


class TablesView(ConnRequiredMixin, DbmysqlMixin, TemplateView):
    """表列表页"""
    template_name = 'dbmysql/tables.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        conn_id, db = kwargs['conn_id'], kwargs['db']
        info = get_conn_info(self.request, conn_id)
        context['page_title'] = f'表管理 - {db}'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '数据库管理',
                'href': reverse('dbmysql:databases', args=[conn_id]),
                'active': False
            },
            {
                'title': '表管理',
                'href': None,
                'active': True
            },
        ]
        context['conn_id'] = conn_id
        context['conn_info'] = info
        context['db'] = db
        try:
            conn = _open_conn(self.request, conn_id, db=db)
            try:
                context['tables'] = mc.list_tables(conn, db)
            finally:
                conn.close()
        except mc.MysqlError as e:
            context['db_error'] = str(e)
            context['tables'] = []
        return context


class ColumnsView(ConnRequiredMixin, DbmysqlMixin, TemplateView):
    """字段列表页"""
    template_name = 'dbmysql/columns.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        conn_id, db, table = kwargs['conn_id'], kwargs['db'], kwargs['table']
        info = get_conn_info(self.request, conn_id)
        context['page_title'] = f'字段管理 - {db}.{table}'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '数据库管理',
                'href': reverse('dbmysql:databases', args=[conn_id]),
                'active': False
            },
            {
                'title': '表管理',
                'href': reverse('dbmysql:tables', args=[conn_id, db]),
                'active': False
            },
            {
                'title': '字段管理',
                'href': None,
                'active': True
            },
        ]
        context['conn_id'] = conn_id
        context['conn_info'] = info
        context['db'] = db
        context['table'] = table
        try:
            conn = _open_conn(self.request, conn_id, db=db)
            try:
                context['columns'] = mc.list_columns(conn, db, table)
            finally:
                conn.close()
        except mc.MysqlError as e:
            context['db_error'] = str(e)
            context['columns'] = []
        return context


class IndexesView(ConnRequiredMixin, DbmysqlMixin, TemplateView):
    """索引管理页"""
    template_name = 'dbmysql/indexes.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        conn_id, db, table = kwargs['conn_id'], kwargs['db'], kwargs['table']
        info = get_conn_info(self.request, conn_id)
        context['page_title'] = f'索引管理 - {db}.{table}'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '数据库管理',
                'href': reverse('dbmysql:databases', args=[conn_id]),
                'active': False
            },
            {
                'title': '表管理',
                'href': reverse('dbmysql:tables', args=[conn_id, db]),
                'active': False
            },
            {
                'title': '索引管理',
                'href': None,
                'active': True
            },
        ]
        context['conn_id'] = conn_id
        context['conn_info'] = info
        context['db'] = db
        context['table'] = table
        try:
            conn = _open_conn(self.request, conn_id, db=db)
            try:
                raw = mc.list_indexes(conn, db, table)
                grouped: dict[str, dict] = {}
                for idx in raw:
                    kn = idx['key_name']
                    if kn not in grouped:
                        grouped[kn] = {
                            'key_name': kn,
                            'columns': [],
                            'non_unique': idx['non_unique'],
                            'index_type': idx['index_type'],
                        }
                    grouped[kn]['columns'].append(idx['column_name'])
                context['indexes'] = list(grouped.values())
            finally:
                conn.close()
        except mc.MysqlError as e:
            context['db_error'] = str(e)
            context['indexes'] = []
        return context


# ── 新建/表单页面（从列表页链接跳转） ────────────────────

class DatabaseCreateView(ConnRequiredMixin, DbmysqlMixin, TemplateView):
    """新建数据库页"""
    template_name = 'dbmysql/db_create.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        conn_id = kwargs['conn_id']
        info = get_conn_info(self.request, conn_id)
        context['page_title'] = f"新建数据库 - {info['host']}:{info['port']}"
        context['conn_id'] = conn_id
        context['conn_info'] = info
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '数据库管理',
                'href': reverse('dbmysql:databases', args=[conn_id]),
                'active': False
            },
            {
                'title': '新建数据库',
                'href': None,
                'active': True
            },
        ]
        # 从 MySQL 获取字符集与排序规则
        try:
            conn = _open_conn(self.request, conn_id)
            try:
                charset_data = mc.get_charsets(conn)
            finally:
                conn.close()
            context['charset_data_json'] = json.dumps(charset_data, ensure_ascii=False)
        except mc.MysqlError as e:
            context['db_error'] = str(e)
            context['charset_data_json'] = '{}'
        return context


class UserGrantsView(ConnRequiredMixin, DbmysqlMixin, TemplateView):
    """用户权限管理独立页面"""
    template_name = 'dbmysql/user_grants.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        conn_id = kwargs['conn_id']
        user = kwargs['user']
        host = kwargs.get('host', '%')
        info = get_conn_info(self.request, conn_id)
        context['page_title'] = f'权限管理 - {user}@{host}'
        context['breadcrumb'] = [
            {'title': 'MySQL管理', 'href': reverse('dbmysql:index'), 'active': False},
            {'title': '数据库管理', 'href': reverse('dbmysql:databases', args=[conn_id]), 'active': False},
            {'title': '用户权限', 'href': None, 'active': True},
        ]
        context['conn_id'] = conn_id
        context['conn_info'] = info
        context['user'] = user
        context['host'] = host
        try:
            conn = _open_conn(self.request, conn_id)
            try:
                context['grants'] = mc.get_user_grants(conn, user, host)
            finally:
                conn.close()
        except mc.MysqlError as e:
            context['db_error'] = str(e)
            context['grants'] = []
        return context


class TableCreateView(ConnRequiredMixin, DbmysqlMixin, TemplateView):
    """新建数据表页"""
    template_name = 'dbmysql/table_create.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        conn_id, db = kwargs['conn_id'], kwargs['db']
        info = get_conn_info(self.request, conn_id)
        context['page_title'] = f'新建数据表 - {db}'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '数据库管理',
                'href': reverse('dbmysql:databases', args=[conn_id]),
                'active': False
            },
            {
                'title': '表管理',
                'href': reverse('dbmysql:tables', args=[conn_id, db]),
                'active': False
            },
            {
                'title': '新建数据表',
                'href': None,
                'active': True
            },
        ]
        context['conn_id'] = conn_id
        context['conn_info'] = info
        context['db'] = db
        # 从 MySQL 获取字符集与排序规则
        try:
            conn = _open_conn(self.request, conn_id)
            try:
                charset_data = mc.get_charsets(conn)
            finally:
                conn.close()
            context['charset_data_json'] = json.dumps(charset_data, ensure_ascii=False)
        except mc.MysqlError as e:
            context['db_error'] = str(e)
            context['charset_data_json'] = '{}'
        return context


class ColumnAddView(ConnRequiredMixin, DbmysqlMixin, TemplateView):
    """添加字段页：类型下拉 + 按类型动态配置"""
    template_name = 'dbmysql/column_add.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        conn_id, db, table = kwargs['conn_id'], kwargs['db'], kwargs['table']
        info = get_conn_info(self.request, conn_id)
        context['page_title'] = f'添加字段 - {db}.{table}'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '数据库管理',
                'href': reverse('dbmysql:databases', args=[conn_id]),
                'active': False
            },
            {
                'title': '表管理',
                'href': reverse('dbmysql:tables', args=[conn_id, db]),
                'active': False
            },
            {
                'title': '字段管理',
                'href': reverse('dbmysql:columns', args=[conn_id, db, table]),
                'active': False
            },
            {
                'title': '添加字段',
                'href': None,
                'active': True
            },
        ]
        context['conn_id'] = conn_id
        context['conn_info'] = info
        context['db'] = db
        context['table'] = table
        context['field_types'] = mc.FIELD_TYPES
        context['field_types_json'] = json.dumps(mc.FIELD_TYPES, ensure_ascii=False)
        try:
            conn = _open_conn(self.request, conn_id, db=db)
            try:
                context['columns'] = mc.list_columns(conn, db, table)
            finally:
                conn.close()
        except mc.MysqlError as e:
            context['db_error'] = str(e)
            context['columns'] = []
        return context


class ColumnEditView(ConnRequiredMixin, DbmysqlMixin, TemplateView):
    """编辑字段页：回填现有定义，类型下拉 + 按类型动态配置"""
    template_name = 'dbmysql/column_edit.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        conn_id, db, table, col_name = kwargs['conn_id'], kwargs['db'], kwargs['table'], kwargs['name']
        info = get_conn_info(self.request, conn_id)
        context['page_title'] = f'编辑字段 - {db}.{table}.{col_name}'
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '数据库管理',
                'href': reverse('dbmysql:databases', args=[conn_id]),
                'active': False
            },
            {
                'title': '表管理',
                'href': reverse('dbmysql:tables', args=[conn_id, db]),
                'active': False
            },
            {
                'title': '字段管理',
                'href': reverse('dbmysql:columns', args=[conn_id, db, table]),
                'active': False
            },
            {
                'title': '编辑字段',
                'href': None,
                'active': True
            },
        ]
        context['conn_id'] = conn_id
        context['conn_info'] = info
        context['db'] = db
        context['table'] = table
        context['col_name'] = col_name
        context['field_types'] = mc.FIELD_TYPES
        context['field_types_json'] = json.dumps(mc.FIELD_TYPES, ensure_ascii=False)
        try:
            conn = _open_conn(self.request, conn_id, db=db)
            try:
                columns = mc.list_columns(conn, db, table)
            finally:
                conn.close()
            column = next((c for c in columns if c['name'] == col_name), None)
            if column is None:
                context['db_error'] = f'字段不存在: {col_name}'
            else:
                column = dict(column)
                column['parsed'] = mc.parse_field_type(column['type'])
                context['column'] = column
                context['column_json'] = json.dumps(column, ensure_ascii=False, default=str)
        except mc.MysqlError as e:
            context['db_error'] = str(e)
        return context


# ── 实例管理 API ──────────────────────────────────

@require_POST
def api_instance_add(request):
    """手动添加实例：按安装目录自动解析并登记"""
    install_dir = request.POST.get('install_dir', '').strip()
    custom_name = request.POST.get('name', '').strip()
    try:
        info = resolve_instance(install_dir)
    except ResolveError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    except Exception as e:
        logger.exception('解析 MySQL 实例目录失败')
        return JsonResponse({'success': False, 'message': f'解析目录失败: {e}'})

    # 去重：安装目录 / 数据目录 / 服务名任一重合即视为已登记
    def _nd(d):
        return str(d or '').replace('/', '\\').rstrip('\\').lower()
    nd_info_install = _nd(info['install_dir'])
    nd_info_data = _nd(info['data_dir'])
    for item in storage.list_instances():
        dup = ((info['install_dir'] and _nd(item.get('install_dir')) == nd_info_install)
               or (info['data_dir'] and _nd(item.get('data_dir')) == nd_info_data)
               or (info['service_name'] and item.get('service_name') == info['service_name']))
        if dup:
            return JsonResponse({'success': False,
                                 'message': f"该实例已登记: {item.get('name')}"})

    if custom_name:
        info['name'] = custom_name
    info['source'] = 'manual'
    record = storage.add_instance(info)
    return JsonResponse({
        'success': True,
        'message': f"添加成功: {record['name']}（MySQL {record.get('version') or '版本未知'}，端口 {record['port']}）",
        'instance': record,
    })


@require_POST
def api_instance_resolve(request):
    """预览：根据安装目录自动解析实例信息，返回识别到的字段供前端展示"""
    install_dir = request.POST.get('install_dir', '').strip()
    try:
        info = resolve_instance(install_dir)
    except ResolveError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    except Exception as e:
        logger.exception('解析 MySQL 实例目录失败')
        return JsonResponse({'success': False, 'message': f'解析目录失败: {e}'})
    return JsonResponse({'success': True, 'info': info})


@require_POST
def api_instance_remove(request):
    """移除实例登记记录（不卸载软件）"""
    instance_id = request.POST.get('id', '')
    # 记录被移除 → 该实例的会话连接一并失效
    _clear_instance_conns(request, instance_id)
    if storage.remove_instance(instance_id):
        return JsonResponse({'success': True, 'message': '已移除该记录'})
    return JsonResponse({'success': False, 'message': '记录不存在'})


@require_POST
def api_instance_start(request):
    """启动实例：服务方式 net start，进程方式拉起 mysqld"""
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    host = instance.get('host', '127.0.0.1')
    port = int(instance.get('port') or 3306)
    if _port_reachable(host, port, use_cache=False):
        return JsonResponse({'success': True, 'message': '实例已在运行中'})

    # 服务方式（jiecore 公共能力）
    if instance.get('service_name'):
        result = start_service(instance['service_name'])
        log_operation('dbmysql', f'启动 MySQL {instance.get("version", "")}',
                      success=result.get('success', False),
                      detail=f'实例 {instance.get("name", "")}，端口 {port}：{result.get("message", "")}')
        if result.get('success'):
            _invalidate_port_cache(host, port)
        return JsonResponse(result)

    # 进程方式：有安装目录且含 my.ini/mysqld.exe 的实例（zip 版/本应用安装）
    install_dir = instance.get('install_dir', '')
    if not install_dir:
        return JsonResponse({'success': False, 'message': '该实例无安装目录信息，无法启动'})
    ini = find_my_ini(install_dir, instance.get('data_dir', ''))
    mysqld = pathlib.Path(install_dir) / 'bin' / 'mysqld.exe'
    if not ini or not ini.exists() or not mysqld.exists():
        return JsonResponse({'success': False, 'message': '安装目录中缺少 my.ini 或 mysqld.exe'})
    DETACHED = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    subprocess.Popen([str(mysqld), f'--defaults-file={ini}'], creationflags=DETACHED,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_operation('dbmysql', f'启动 MySQL {instance.get("version", "")}（进程模式）',
                  detail=f'实例 {instance.get("name", "")}，端口 {port}，配置 {ini}')
    _invalidate_port_cache(host, port)
    return JsonResponse({'success': True, 'message': 'MySQL 进程已启动，请稍候刷新页面'})


@require_POST
def api_instance_stop(request):
    """停止实例：服务方式 net stop（未注册服务的实例提示先注册）

    停止成功后同步清理本会话中该实例的登录连接，避免出现
    “服务已停但页面仍显示登录中”的状态。
    """
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    host = instance.get('host', '127.0.0.1')
    port = int(instance.get('port') or 3306)
    if not _port_reachable(host, port, use_cache=False):
        # 实例本就没运行，顺手把残留的失效连接清掉
        _clear_instance_conns(request, instance['id'])
        return JsonResponse({'success': True, 'message': '实例当前未在运行'})

    service_name = instance.get('service_name', '')
    if not service_name:
        return JsonResponse({'success': False,
                             'message': '该实例未注册 Windows 服务，请先注册服务后再启停'})
    result = stop_service(service_name)
    log_operation('dbmysql', f'停止 MySQL {instance.get("version", "")}',
                  success=result.get('success', False),
                  detail=f'实例 {instance.get("name", "")}，端口 {port}：{result.get("message", "")}')
    if result.get('success'):
        _invalidate_port_cache(host, port)
        cleared = _clear_instance_conns(request, instance['id'])
        if cleared:
            result['message'] = f"{result.get('message', '')}（已退出 {cleared} 个登录连接）"
    return JsonResponse(result)


@require_GET
def api_instance_startup_type(request):
    """查询实例对应 Windows 服务的启动类型（sc qc 实时查询，不读存储字段）。

    入参: instance_id（query string）
    返回: {exists, start_type} 或 {success:false, message}
    """
    instance = storage.get_instance(request.GET.get('instance_id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    service_name = instance.get('service_name', '')
    if not service_name or instance.get('run_mode') != 'service':
        return JsonResponse({'success': False, 'message': '该实例未注册为 Windows 服务'})
    return JsonResponse(query_service_startup_type(service_name))


@require_POST
def api_instance_set_startup_type(request):
    """切换实例对应 Windows 服务的启动类型（同步执行 sc config，权限不足自动 UAC 提权）。

    入参: instance_id, start_type（form data）
    返回: {success, message}
    """
    instance = storage.get_instance(request.POST.get('instance_id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    service_name = instance.get('service_name', '')
    if not service_name or instance.get('run_mode') != 'service':
        return JsonResponse({'success': False, 'message': '该实例未注册为 Windows 服务'})
    start_type = request.POST.get('start_type', '').strip()
    result = set_service_startup_type(service_name, start_type)
    log_operation('dbmysql', f'切换服务启动类型 {instance.get("version", "")}',
                  success=result.get('success', False),
                  detail=f'实例 {instance.get("name", "")}，服务 {service_name}，'
                         f'目标 {start_type}：{result.get("message", "")}')
    return JsonResponse(result)


@require_POST
def api_instance_service_task(request, action):
    """以任务方式启停实例服务（后台执行 + 逐步日志，前端轮询 api_task_status）

    返回体额外携带 instance_id，前端在任务成功后调用
    api_instance_conn_clear 即可即时清理本会话中该实例的登录连接。
    """
    if action not in ('start', 'stop', 'restart'):
        return JsonResponse({'success': False, 'message': f'未知操作: {action}'})
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    service_name = instance.get('service_name', '')
    verb_map = {'start': '启动', 'stop': '停止', 'restart': '重启'}
    verb = verb_map[action]
    # 停止/重启操作必须有服务名
    if action in ('stop', 'restart') and not service_name:
        return JsonResponse({'success': False,
                             'message': '该实例未注册 Windows 服务，请先注册服务后再启停'})
    # 启动操作：无服务名时检查是否具备进程启动条件
    if action == 'start' and not service_name:
        install_dir = instance.get('install_dir', '')
        if not install_dir:
            return JsonResponse({'success': False,
                                 'message': '该实例无安装目录信息，无法启动'})
        ini = find_my_ini(install_dir, instance.get('data_dir', ''))
        mysqld = pathlib.Path(install_dir) / 'bin' / 'mysqld.exe'
        if not ini or not ini.exists() or not mysqld.exists():
            return JsonResponse({'success': False,
                                 'message': '安装目录中缺少 my.ini 或 mysqld.exe'})
    task = tasks.create_task(f'service_{action}',
                             meta={'service_name': service_name,
                                   'instance': instance.get('name', ''),
                                   'instance_id': instance.get('id', '')},
                             title=f'{verb} MySQL 服务')
    if service_name:
        tasks.append_log(task, f'{verb}服务任务已创建: {service_name}')
    else:
        tasks.append_log(task, f'{verb}任务已创建（进程模式）')
    tasks.run_in_thread(task, _run_service_task, action, instance)
    return JsonResponse({'success': True, 'task_id': task['task_id'],
                         'instance_id': instance['id']})


def _run_service_task(task: dict, action: str, instance: dict):
    """后台线程执行服务启停/重启，通过 on_step 回报逐步进度"""
    verb_map = {'start': '启动', 'stop': '停止', 'restart': '重启'}
    verb = verb_map.get(action, action)
    service_name = instance.get('service_name', '')
    host = instance.get('host', '127.0.0.1')
    port = int(instance.get('port') or 3306)

    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    # ── 服务模式 ──
    if service_name:
        state = query_service_state(service_name)
        if action == 'start' and state['running']:
            tasks.append_log(task, f'服务 {service_name} 已在运行中，无需启动')
            task['result'] = {'success': True, 'message': '实例已在运行中'}
            return
        if action == 'stop' and not state['running']:
            tasks.append_log(task, f'服务 {service_name} 当前未在运行')
            task['result'] = {'success': True, 'message': '实例当前未在运行'}
            return
        if action == 'restart':
            result = restart_service(service_name, on_step=on_step)
        else:
            handler = start_service if action == 'start' else stop_service
            result = handler(service_name, on_step=on_step)
        tasks.append_log(task, result['message'])
        _invalidate_port_cache(host, port)
        if result['success']:
            tasks.set_progress(task, progress=100, stage=f'服务{verb}成功')
            # 停止/重启后该实例的既有登录连接已失效，交给前端调
            # api_instance_conn_clear 清理（后台线程拿不到 request.session）
            result['instance_id'] = instance.get('id', '')
            result['conn_stale'] = action in ('stop', 'restart')
        task['result'] = result
        log_operation('dbmysql', f'{verb} MySQL {instance.get("version", "")}',
                      success=result['success'],
                      detail=f'实例 {instance.get("name", "")}，端口 {port}：{result.get("message", "")}')
        if not result['success']:
            raise RuntimeError(result['message'])
        return

    # ── 进程模式（仅启动） ──
    if _port_reachable(host, port, use_cache=False):
        tasks.append_log(task, f'端口 {port} 已在监听，实例已运行')
        task['result'] = {'success': True, 'message': '实例已在运行中'}
        return

    install_dir = instance.get('install_dir', '')
    ini = find_my_ini(install_dir, instance.get('data_dir', ''))
    mysqld = pathlib.Path(install_dir) / 'bin' / 'mysqld.exe'
    on_step(f'正在以进程方式启动 MySQL（端口 {port}）...')
    DETACHED = 0x00000008 | 0x00000200 | 0x08000000
    subprocess.Popen([str(mysqld), f'--defaults-file={ini}'], creationflags=DETACHED,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tasks.append_log(task, 'MySQL 后台进程已启动，等待端口就绪...')
    # 等待端口就绪（此处实时探测，不走缓存）
    for i in range(30):
        time.sleep(2)
        if _port_reachable(host, port, use_cache=False):
            tasks.append_log(task, f'端口 {port} 已就绪')
            tasks.set_progress(task, progress=100, stage='启动成功')
            task['result'] = {'success': True, 'message': f'MySQL 已启动（端口 {port}）',
                              'instance_id': instance.get('id', '')}
            _invalidate_port_cache(host, port)
            return
    tasks.append_log(task, f'等待端口 {port} 就绪超时，请检查日志')
    task['result'] = {'success': False, 'message': 'MySQL 启动超时'}
    raise RuntimeError('MySQL 启动超时，请检查实例日志')


@require_POST
def api_instance_process_task(request, action: str):
    """进程模式实例启停/重启任务入口（校验链 + 防重 + 建任务，幂等与执行在任务内）"""
    if action not in ('start', 'stop', 'restart'):
        return JsonResponse({'success': False, 'message': f'未知操作: {action}'})
    verb_map = {'start': '启动', 'stop': '停止', 'restart': '重启'}
    verb = verb_map[action]
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    # 已注册 Windows 服务的实例走服务启停链路
    if instance.get('service_name', ''):
        return JsonResponse({'success': False,
                             'message': '该实例已注册为 Windows 服务，请通过服务方式启停'})
    # 远程实例仅支持状态查看，无法在本机做进程启停
    host = instance.get('host', '127.0.0.1')
    if not _is_local_host(host):
        return JsonResponse({'success': False,
                             'message': f'该实例为远程连接，仅支持状态查看，无法在本机{verb}'})
    # 任务防重：同实例已有进行中的进程操作任务时不再重复创建
    for t in tasks.list_tasks():
        meta = t.get('meta', {})
        if (meta.get('instance_id') == instance.get('id')
                and str(t.get('type', '')).startswith('process_')
                and t.get('status') in ('pending', 'running')):
            return JsonResponse({'success': False,
                                 'message': '该实例已有进行中的操作任务，请等待完成'})
    # 启动条件校验（仅 start/restart 需要）
    if action in ('start', 'restart'):
        install_dir = instance.get('install_dir', '')
        if not install_dir:
            return JsonResponse({'success': False,
                                 'message': '该实例无安装目录信息，无法启动'})
        ini = find_my_ini(install_dir, instance.get('data_dir', ''))
        mysqld = pathlib.Path(install_dir) / 'bin' / 'mysqld.exe'
        if not ini or not ini.exists() or not mysqld.exists():
            return JsonResponse({'success': False,
                                 'message': '安装目录中缺少 my.ini 或 mysqld.exe'})
    task = tasks.create_task(f'process_{action}',
                             meta={'instance_id': instance.get('id', ''),
                                   'instance': instance.get('name', ''),
                                   'service_name': ''},
                             title=f'{verb} MySQL（进程模式）')
    tasks.append_log(task, f'{verb}任务已创建（进程模式）')
    tasks.run_in_thread(task, _run_process_task, action, instance)
    return JsonResponse({'success': True, 'task_id': task['task_id'],
                         'instance_id': instance['id']})


def _is_local_host(host: str) -> bool:
    """判断 host 是否为本机地址（白名单 + 本机网卡地址比对）"""
    if host in ('127.0.0.1', 'localhost', '::1'):
        return True
    try:
        import socket
        local_ips = {info[4][0] for info in
                     socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        return host in local_ips
    except Exception:
        return False


def _run_process_task(task: dict, action: str, instance: dict):
    """后台线程执行进程模式启停/重启，on_step 闭包回报逐步进度（同 _run_service_task 模式）"""
    verb_map = {'start': '启动', 'stop': '停止', 'restart': '重启'}
    verb = verb_map.get(action, action)
    host = instance.get('host', '127.0.0.1')
    port = int(instance.get('port') or 3306)
    audit_action = f'进程模式{verb} MySQL {instance.get("version", "")}'

    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    def audit(success: bool, detail: str):
        log_operation('dbmysql', audit_action, success=success,
                      detail=f'实例 {instance.get("name", "")}，端口 {port}'
                            + (f'：{detail}' if detail else ''))

    def do_start():
        # 幂等检查：端口已监听则视为已在运行（实时探测，不走缓存）
        if check_port(host, port):
            on_step(f'端口 {port} 已在监听，实例已在运行')
            task['result'] = {'success': True, 'message': '实例已在运行中',
                              'instance_id': instance.get('id', ''),
                              'conn_stale': False}
            return
        install_dir = instance.get('install_dir', '')
        ini = find_my_ini(install_dir, instance.get('data_dir', ''))
        mysqld = pathlib.Path(install_dir) / 'bin' / 'mysqld.exe'
        if not ini or not ini.exists() or not mysqld.exists():
            on_step('安装目录中缺少 my.ini 或 mysqld.exe，无法启动')
            raise RuntimeError('安装目录中缺少 my.ini 或 mysqld.exe')
        on_step(f'正在以进程方式启动 MySQL（端口 {port}）...')
        tasks.set_progress(task, progress=50, stage='启动进程')
        pid = launch_mysqld(mysqld, ini)
        tasks.append_log(task, f'MySQL 后台进程已启动（PID {pid}），等待端口就绪...')
        tasks.set_progress(task, progress=70, stage='等待端口就绪')
        if not wait_port_ready(host, port, START_READY_TIMEOUT):
            tasks.append_log(task, f'等待端口 {port} 就绪超时，请检查实例日志')
            raise RuntimeError('MySQL 启动超时，请检查实例日志')
        tasks.append_log(task, f'端口 {port} 已就绪')
        tasks.set_progress(task, progress=100, stage='启动成功')
        _invalidate_port_cache(host, port)
        # 启动成功后 run_mode 归一（仅动单字段）
        if instance.get('run_mode') != 'service':
            storage.update_instance(instance.get('id', ''), run_mode='process')
        task['result'] = {'success': True, 'message': f'MySQL 已启动（端口 {port}）',
                          'instance_id': instance.get('id', ''),
                          'conn_stale': False}

    def do_stop():
        # 幂等检查：端口未监听则视为未在运行
        if not check_port(host, port):
            on_step(f'端口 {port} 未在监听，实例未在运行')
            task['result'] = {'success': True, 'message': '实例当前未在运行',
                              'instance_id': instance.get('id', ''),
                              'conn_stale': True}
            return
        # 端口 + 映像名双重校验，防止误杀占用同端口的其他程序
        loc = locate_mysqld_process(port)
        if not loc.get('found'):
            reason = loc.get('reason', f'未定位到端口 {port} 上的 mysqld 进程')
            on_step(reason)
            raise RuntimeError(reason)
        pid = loc['pid']
        tasks.append_log(task, f"已定位 MySQL 进程: PID {pid} ({loc.get('image_name', '')})")
        tasks.set_progress(task, progress=50, stage='正在停止进程')
        t_start = time.time()
        # 阶段一：温和终止 + 宽限期等待
        ok, detail = terminate_process(pid, force=False)
        graceful = False
        if not ok:
            on_step(f'温和终止未生效: {detail}')
        elif wait_port_released(host, port, GRACE_PERIOD):
            tasks.append_log(task, f'进程已退出，端口 {port} 已释放')
            graceful = True
        else:
            tasks.append_log(task, f'温和终止未生效（宽限期 {GRACE_PERIOD}s 内未释放）')
        if not graceful:
            # 阶段二：强制终止（主根因修复：补上被遗漏的强制终止调用）
            tasks.append_log(task, f'已执行强制终止（PID {pid}）（可能中断未完成事务）')
            ok2, detail2 = terminate_process(pid, force=True)
            if not ok2:
                on_step(f'强制终止失败: {detail2}')
                raise RuntimeError(f'强制终止 MySQL 失败: {detail2}')
            # 剩余确认期按实耗计算，总耗时 ≤ STOP_CONFIRM_TIMEOUT
            remaining = max(MIN_FORCE_CONFIRM, STOP_CONFIRM_TIMEOUT - (time.time() - t_start))
            if not wait_port_released(host, port, remaining):
                reason = f'停止超时：强制终止后端口 {port} 仍被占用'
                tasks.append_log(task, reason)
                raise RuntimeError(reason)
            # 双条件判定：端口释放 + PID 真实退出（防"假释放"误报成功）
            if process_exists(pid):
                reason = f'端口已释放但原 PID {pid} 仍存在，停止确认失败'
                tasks.append_log(task, reason)
                raise RuntimeError('端口已释放但目标 PID 仍存在，请检查进程状态')
            tasks.append_log(task, f'进程 {pid} 已退出')
        _invalidate_port_cache(host, port)
        tasks.set_progress(task, progress=100, stage='停止成功')
        # 停止任务不修改 run_mode
        task['result'] = {'success': True, 'message': f'MySQL 已停止（端口 {port}）',
                          'instance_id': instance.get('id', ''),
                          'conn_stale': True}

    try:
        if action == 'start':
            on_step('=== 进程模式启动 ===')
            tasks.set_progress(task, progress=30, stage='校验启动条件')
            do_start()
        elif action == 'stop':
            on_step('=== 进程模式停止 ===')
            tasks.set_progress(task, progress=30, stage='定位进程')
            do_stop()
        else:
            on_step('=== 阶段一: 停止 ===')
            tasks.set_progress(task, progress=20, stage='停止阶段')
            do_stop()
            on_step('=== 阶段二: 启动 ===')
            tasks.set_progress(task, progress=60, stage='启动阶段')
            do_start()
            # 重启含停止阶段，连接已失效
            if isinstance(task.get('result'), dict):
                task['result']['conn_stale'] = True
        audit(True, task['result'].get('message', ''))
    except Exception as e:
        # run_in_thread 兜底写"任务失败"日志并置 error；此处补审计记录（每任务一条）
        audit(False, str(e))
        raise


@require_GET
def api_task_status(request, task_id):
    """轮询后台任务状态与增量日志（通用，含安装/服务启停任务）。

    返回体由 jiecore.tasks.task_payload() 统一构造，与 jiecore 通用接口
    /jiecore/api/task-status/<task_id>/ 完全一致；保留本应用路由是为了不改动
    模板里已有的 {% url "dbmysql:api_task_status" %}。
    """
    task = tasks.get_task(task_id)
    if not task:
        return JsonResponse({'success': False, 'message': '任务不存在'})
    return JsonResponse(tasks.task_payload(task, request.GET.get('after', 0)))


@require_POST
def api_instance_register_service(request):
    """将实例注册为 Windows 服务（需安装目录含 mysqld.exe，并能定位 my.ini）"""
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    # 已有可用服务则无需重复注册
    if instance.get('service_name') and query_service_state(instance['service_name'])['exists']:
        return JsonResponse({'success': False,
                             'message': f"该实例已注册服务: {instance['service_name']}"})

    install_dir = instance.get('install_dir', '')
    if not install_dir:
        return JsonResponse({'success': False, 'message': '该实例无安装目录信息，无法注册服务'})
    base = pathlib.Path(install_dir)
    mysqld = base / 'bin' / 'mysqld.exe'
    if not mysqld.exists():
        return JsonResponse({'success': False,
                             'message': f'安装目录中未找到 mysqld.exe: {mysqld}'})
    ini = find_my_ini(install_dir, instance.get('data_dir', ''))
    if not ini:
        return JsonResponse({'success': False, 'message': '未找到 my.ini 配置文件，无法注册服务'})

    service_name = request.POST.get('service_name', '').strip()
    if not service_name:
        version = (instance.get('version') or '').replace('.', '')
        service_name = f'MySQL{version}' if version else f"MySQL{instance.get('port', 3306)}"

    result = register_mysql_service(str(mysqld), service_name, str(ini))
    if result['success']:
        new_name = _build_service_instance_name(instance.get('version'), service_name)
        storage.update_instance(instance['id'],
                                service_name=service_name, run_mode='service',
                                name=new_name)
        return JsonResponse({'success': True,
                             'message': f'服务注册成功: {service_name}，现可通过服务方式启动/停止'})
    return JsonResponse({'success': False, 'message': result['message']})


@require_POST
def api_instance_register_service_task(request):
    """以任务方式将实例注册为 Windows 服务（后台执行 + 逐步日志，前端轮询 api_task_status）"""
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    if instance.get('service_name') and query_service_state(instance['service_name'])['exists']:
        return JsonResponse({'success': False,
                             'message': f"该实例已注册服务: {instance['service_name']}"})

    install_dir = instance.get('install_dir', '')
    if not install_dir:
        return JsonResponse({'success': False, 'message': '该实例无安装目录信息，无法注册服务'})
    base = pathlib.Path(install_dir)
    mysqld = base / 'bin' / 'mysqld.exe'
    if not mysqld.exists():
        return JsonResponse({'success': False,
                             'message': f'安装目录中未找到 mysqld.exe: {mysqld}'})
    ini = find_my_ini(install_dir, instance.get('data_dir', ''))
    if not ini:
        return JsonResponse({'success': False, 'message': '未找到 my.ini 配置文件，无法注册服务'})

    service_name = request.POST.get('service_name', '').strip()
    if not service_name:
        version = (instance.get('version') or '').replace('.', '')
        service_name = f'MySQL{version}' if version else f"MySQL{instance.get('port', 3306)}"
    if not _SERVICE_NAME_RE.match(service_name):
        return JsonResponse({'success': False,
                             'message': '服务名必须以字母开头，仅含字母/数字/下划线/中划线'})
    if query_service_state(service_name)['exists']:
        return JsonResponse({'success': False, 'message': f'服务名已存在: {service_name}'})

    task = tasks.create_task('register_service',
                             meta={'service_name': service_name,
                                   'instance': instance.get('name', '')},
                             title=f'注册 MySQL 服务 {service_name}')
    tasks.append_log(task, f'注册服务任务已创建: {service_name}')
    tasks.run_in_thread(task, _run_register_service_task,
                        instance, str(mysqld), service_name, str(ini))
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _run_register_service_task(task: dict, instance: dict,
                                mysqld_path: str, service_name: str,
                                ini_path: str):
    """后台线程执行服务注册，通过 on_step 回报逐步进度"""
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'正在注册 MySQL 服务 {service_name}...')
    result = register_mysql_service(mysqld_path, service_name, ini_path)
    tasks.append_log(task, result['message'])

    if result['success']:
        new_name = _build_service_instance_name(instance.get('version'), service_name)
        storage.update_instance(instance['id'],
                                service_name=service_name, run_mode='service',
                                name=new_name)
        tasks.set_progress(task, progress=100, stage='服务注册成功')
        task['result'] = {'success': True,
                          'message': f'服务注册成功: {service_name}'}
    else:
        task['result'] = {'success': False, 'message': result['message']}
        raise RuntimeError(result['message'])


@require_POST
def api_instance_remove_service(request):
    """以任务方式删除实例的 Windows 服务"""
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    service_name = instance.get('service_name', '')
    if not service_name:
        return JsonResponse({'success': False, 'message': '该实例未注册 Windows 服务'})

    install_dir = instance.get('install_dir', '')
    mysqld = str(pathlib.Path(install_dir) / 'bin' / 'mysqld.exe') if install_dir else ''

    task = tasks.create_task('remove_service',
                             meta={'service_name': service_name,
                                   'instance': instance.get('name', '')},
                             title=f'删除 MySQL 服务 {service_name}')
    tasks.append_log(task, f'删除服务任务已创建: {service_name}')
    tasks.run_in_thread(task, _run_remove_service_task,
                        instance, mysqld, service_name)
    return JsonResponse({'success': True, 'task_id': task['task_id'],
                         'instance_id': instance['id']})


def _run_remove_service_task(task: dict, instance: dict,
                              mysqld_path: str, service_name: str):
    """后台线程执行服务删除"""
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    on_step(f'正在删除 MySQL 服务 {service_name}...')
    result = remove_mysql_service(mysqld_path, service_name)
    tasks.append_log(task, result['message'])

    if result['success']:
        storage.update_instance(instance['id'],
                                service_name='', run_mode='process')
        tasks.set_progress(task, progress=100, stage='服务已删除')
        task['result'] = {'success': True, 'message': result['message']}
    else:
        task['result'] = {'success': False, 'message': result['message']}
        raise RuntimeError(result['message'])


@require_POST
def api_instance_uninstall(request):
    """以任务方式卸载 MySQL：停止服务 → 删除服务 → 删除安装目录 → 移除记录"""
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    keep_data = request.POST.get('keep_data') == '1'

    # 检查并退出该实例的所有活跃会话连接
    cleared = _clear_instance_conns(request, instance.get('id', ''))

    task = tasks.create_task('uninstall',
                             meta={'instance': instance.get('name', ''),
                                   'instance_id': instance.get('id', ''),
                                   'keep_data': keep_data},
                             title=f'卸载 {instance.get("name", "MySQL")}')
    if cleared:
        tasks.append_log(task, f'检测到 {cleared} 个活跃连接，已自动退出')
    tasks.append_log(task, f'卸载任务已创建: {instance.get("name", "")}')
    tasks.run_in_thread(task, _run_uninstall_task, instance, keep_data)
    return JsonResponse({'success': True, 'task_id': task['task_id'],
                         'instance_id': instance['id']})


def _mysqld_running() -> bool:
    """检查是否还有 mysqld.exe 进程在运行（用 CSV 格式解析，避免本地化问题）"""
    try:
        proc = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/FI', 'IMAGENAME eq mysqld.exe'],
            capture_output=True, timeout=10,
            creationflags=0x08000000)
        out = (proc.stdout or b'').decode(get_cmd_encoding(), errors='replace')
        # CSV 格式：第一行是表头，后续行是进程；只要数据行含 mysqld 即表示存在
        for line in out.splitlines()[1:]:
            if 'mysqld' in line.lower():
                return True
        return False
    except Exception:
        return False


def _kill_all_mysqld(on_step=None, service_name: str = ''):
    """强制结束系统中所有 mysqld.exe 进程。
    依次尝试：taskkill /IM → taskkill by service → 提权 taskkill。
    返回 True 表示所有进程已终止。"""
    # 第一步：当前权限 taskkill /IM
    try:
        subprocess.run(['taskkill', '/IM', 'mysqld.exe', '/F', '/T'],
                       capture_output=True, timeout=15,
                       creationflags=0x08000000)
    except Exception:
        pass
    if not _mysqld_running():
        return True

    # 第二步：通过服务名终止（更精确，适用于服务进程）
    if service_name:
        try:
            subprocess.run(
                ['taskkill', '/F', '/FI', f'SERVICES eq {service_name}'],
                capture_output=True, timeout=15,
                creationflags=0x08000000)
        except Exception:
            pass
        if not _mysqld_running():
            return True

    # 第三步：提权 taskkill
    if on_step:
        on_step('普通权限无法终止 mysqld，申请管理员权限...')
    from django.conf import settings as _settings
    bat_file = _settings.TMP_DIR / f'kill_mysqld_{os.getpid()}.bat'
    try:
        cmds = 'taskkill /IM mysqld.exe /F /T >nul 2>&1\n'
        if service_name:
            cmds += f'taskkill /F /FI "SERVICES eq {service_name}" >nul 2>&1\n'
        bat_file.write_text(f'@echo off\n{cmds}', encoding='gbk')
    except OSError:
        return not _mysqld_running()
    try:
        run_elevated('cmd.exe', f'/c "{bat_file}"', 30)
    except PermissionError as e:
        if on_step:
            on_step(f'提权终止进程被拒绝: {e}')
    except Exception as e:
        if on_step:
            on_step(f'提权终止进程失败: {e}')
    finally:
        try:
            os.remove(bat_file)
        except OSError:
            pass
    return not _mysqld_running()


def _rmtree_readonly_handler(func, path, exc_info):
    """shutil.rmtree 错误处理：遇到只读文件时去掉只读属性后重试"""
    import stat
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        try:
            func(path)
            return
        except OSError:
            pass
    logger.warning('无法删除文件（可能被占用）: %s', path)


def _rmdir_cmd(dir_path: str) -> tuple:
    """用 cmd.exe /c rmdir /s /q 删除目录，返回 (success, stderr)"""
    try:
        proc = subprocess.run(
            ['cmd.exe', '/c', 'rmdir', '/s', '/q', dir_path],
            capture_output=True, timeout=30,
            creationflags=0x08000000)
        ok = not os.path.exists(dir_path)
        err = ''
        if not ok:
            err = ((proc.stderr or b'') + (proc.stdout or b'')).decode(
                'gbk', errors='replace').strip()
        return ok, err
    except Exception as e:
        return False, str(e)


def _dir_empty(dir_path: str) -> bool:
    """检查目录是否存在且为空（无文件无子目录）"""
    try:
        if not os.path.exists(dir_path):
            return True
        return not any(os.listdir(dir_path))
    except OSError:
        return False


def _schedule_delete_dir(dir_path: str, task: dict, service_name: str = ''):
    """直接通过 schtasks /ru SYSTEM 创建计划任务，以 SYSTEM 身份强制删除目录。
    绕过 run_elevated（ShellExecuteEx 在桌面壳中可能未真正提权），
    确保以最高权限执行：杀进程 → 等退出 → 删服务 → 删目录。"""
    import uuid
    from django.conf import settings as _settings
    bat_file = _settings.TMP_DIR / f'delete_mysql_{os.getpid()}.bat'
    log_file = _settings.TMP_DIR / f'delete_mysql_{os.getpid()}.log'
    task_name = f'JienvUninstall_{os.getpid()}_{uuid.uuid4().hex[:8]}'

    # ── 构建全面的清理批处理脚本 ──
    lines = [
        '@echo off',
        'chcp 65001 >nul 2>&1',
        f'set "LOGFILE={log_file}"',
        f'echo [%date% %time%] === Start cleanup === > "%LOGFILE%"',
        '',
        ':: Force kill all mysqld processes',
        'taskkill /IM mysqld.exe /F /T >> "%LOGFILE%" 2>&1',
    ]
    if service_name:
        lines.append(
            f'taskkill /F /FI "SERVICES eq {service_name}" >> "%LOGFILE%" 2>&1')
    lines += [
        '',
        ':: Wait for all mysqld processes to exit (up to 20s)',
        'set /a WAIT_COUNT=0',
        ':WAIT_LOOP',
        'tasklist /FI "IMAGENAME eq mysqld.exe" /NH 2>nul | find /I "mysqld.exe" >nul',
        'if errorlevel 1 goto :PROCESS_GONE',
        'if %WAIT_COUNT% GEQ 20 goto :PROCESS_GONE',
        'timeout /t 1 /nobreak >nul',
        'set /a WAIT_COUNT+=1',
        'goto :WAIT_LOOP',
        ':PROCESS_GONE',
        f'echo [%date% %time%] mysqld processes cleared >> "%LOGFILE%"',
        '',
    ]
    if service_name:
        lines += [
            ':: Remove Windows service registration',
            f'sc delete {service_name} >> "%LOGFILE%" 2>&1',
            f'echo [%date% %time%] Service removal attempted >> "%LOGFILE%"',
            '',
        ]
    lines += [
        ':: Delete installation directory with retries',
        'set /a DEL_COUNT=0',
        ':DEL_LOOP',
        f'rmdir /s /q "{dir_path}" >> "%LOGFILE%" 2>&1',
        f'if not exist "{dir_path}" goto :DEL_OK',
        'set /a DEL_COUNT+=1',
        'if %DEL_COUNT% GEQ 8 goto :DEL_DONE',
        'timeout /t 3 /nobreak >nul',
        'goto :DEL_LOOP',
        ':DEL_DONE',
        f'if exist "{dir_path}" (',
        f'  echo [%date% %time%] FAIL: directory still exists >> "%LOGFILE%"',
        f'  echo Remaining files: >> "%LOGFILE%"',
        f'  dir /b /s "{dir_path}" >> "%LOGFILE%" 2>&1',
        ') else (',
        f'  echo [%date% %time%] OK: directory deleted >> "%LOGFILE%"',
        ')',
        ':DEL_OK',
        f'echo [%date% %time%] === Cleanup finished === >> "%LOGFILE%"',
    ]
    bat_content = '\r\n'.join(lines) + '\r\n'

    try:
        bat_file.write_text(bat_content, encoding='gbk')
    except OSError as e:
        tasks.append_log(task, f'无法写入清理脚本: {e}')
        ok, _ = _rmdir_cmd(dir_path)
        if ok:
            tasks.append_log(task, f'已删除安装目录: {dir_path}')
        return

    # ── 直接通过 schtasks 以 SYSTEM 身份执行 ──
    try:
        tasks.append_log(
            task, '正在创建 SYSTEM 权限计划任务（无需 UAC 弹窗）...')

        # 1. 创建计划任务（/ru SYSTEM = 以 SYSTEM 账户运行，无需 UAC）
        create_proc = subprocess.run(
            ['schtasks', '/create', '/tn', task_name,
             '/tr', str(bat_file),
             '/sc', 'once', '/st', '00:00',
             '/ru', 'SYSTEM', '/f'],
            capture_output=True, timeout=30,
            creationflags=0x08000000)
        if create_proc.returncode != 0:
            err = ((create_proc.stdout or b'') + (create_proc.stderr or b''))\
                .decode(get_cmd_encoding(), errors='replace').strip()
            tasks.append_log(task, f'创建计划任务失败: {err[:200]}')
            # 回退：尝试普通 rmdir
            ok, _ = _rmdir_cmd(dir_path)
            if ok:
                tasks.append_log(task, f'已删除安装目录: {dir_path}')
            return

        # 2. 立即触发运行
        subprocess.run(
            ['schtasks', '/run', '/tn', task_name],
            capture_output=True, timeout=30,
            creationflags=0x08000000)

        # 3. 轮询等待任务完成（最长 120 秒）
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            q = subprocess.run(
                ['schtasks', '/query', '/tn', task_name],
                capture_output=True, timeout=15,
                creationflags=0x08000000)
            q_out = (q.stdout or b'').decode(get_cmd_encoding(), errors='replace')
            if 'Running' not in q_out:
                break

        # 4. 读取 bat 脚本的日志输出
        try:
            if log_file.exists():
                log_text = log_file.read_text(
                    encoding=get_cmd_encoding(), errors='replace').strip()
                if log_text:
                    for log_line in log_text.splitlines():
                        log_line = log_line.strip()
                        if log_line:
                            tasks.append_log(task, f'  {log_line}')
        except OSError:
            pass

        # 5. 检查结果
        if not os.path.exists(dir_path):
            tasks.append_log(task, f'SYSTEM 权限已成功删除: {dir_path}')
        elif _dir_empty(dir_path):
            tasks.append_log(
                task, f'目录已清空（残留空目录可手动删除）: {dir_path}')
        else:
            tasks.append_log(
                task, f'SYSTEM 权限删除后目录仍存在: {dir_path}')

    except Exception as e:
        tasks.append_log(task, f'计划任务执行失败: {e}')
    finally:
        # 清理：删除计划任务 + 临时文件
        subprocess.run(
            ['schtasks', '/delete', '/tn', task_name, '/f'],
            capture_output=True, timeout=15,
            creationflags=0x08000000)
        for f in (bat_file, log_file):
            try:
                os.remove(f)
            except OSError:
                pass


def _run_uninstall_task(task: dict, instance: dict, keep_data: bool):
    """后台线程执行完整卸载流程"""
    def on_step(message):
        tasks.append_log(task, message)
        tasks.set_progress(task, stage=message)

    service_name = instance.get('service_name', '')
    install_dir = instance.get('install_dir', '')
    mysqld_path = ''
    if install_dir:
        mysqld_path = str(pathlib.Path(install_dir) / 'bin' / 'mysqld.exe')

    # 1. 停止服务（如果在运行）
    if service_name:
        state = query_service_state(service_name)
        if state['exists'] and state['running']:
            on_step(f'正在停止服务 {service_name}...')
            stop_result = stop_service(service_name, on_step=on_step)
            tasks.append_log(task, stop_result['message'])
            if not stop_result['success']:
                raise RuntimeError(f'停止服务失败: {stop_result["message"]}')
    # 无论是否有服务，都强制确保 mysqld 进程已终止
    if _mysqld_running():
        on_step('正在结束 MySQL 进程...')
        _kill_all_mysqld(on_step=on_step, service_name=service_name)
    on_step('等待 MySQL 进程完全退出...')
    deadline = time.time() + 15
    while time.time() < deadline:
        if not _mysqld_running():
            break
        time.sleep(1)
    if _mysqld_running():
        raise RuntimeError(
            'mysqld 进程无法终止，无法继续卸载。'
            '请手动关闭 MySQL 相关进程后重试')
    time.sleep(1)
    # 端口已释放，失效缓存
    _invalidate_port_cache(instance.get('host', '127.0.0.1'),
                           instance.get('port', 3306))

    # 2. 删除服务注册（进程已确保退出）
    if service_name and mysqld_path:
        on_step(f'正在删除服务 {service_name}...')
        result = remove_mysql_service(mysqld_path, service_name)
        tasks.append_log(task, result['message'])
        if not result['success']:
            raise RuntimeError(f'删除服务失败: {result["message"]}')

    # 2.5 MSI 安装版：通过 msiexec 卸载（清理注册表 + 程序和功能列表）
    install_method = instance.get('install_method', '')
    msi_uninstalled = False
    if install_method == 'msi' and install_dir:
        on_step('检测到 MSI Installer 安装，尝试通过 Windows 卸载程序卸载...')
        product_code = find_msi_product_code(install_dir)
        if product_code:
            tasks.append_log(task, f'找到 MSI 产品代码: {product_code}')
            msi_ok, msi_msg = msi_uninstall_product(product_code)
            tasks.append_log(task, msi_msg)
            if msi_ok:
                msi_uninstalled = True
                # MSI 卸载通常会清除安装目录，等待一下
                time.sleep(2)
        else:
            tasks.append_log(task, '未在注册表中找到 MSI 产品代码，将按普通方式卸载')

    # 3. 删除安装目录（保留数据目录时先移出再删；MSI 卸载已成功则跳过）
    saved_data_path = ''
    data_dir = instance.get('data_dir', '')
    if msi_uninstalled and install_dir and not pathlib.Path(install_dir).exists():
        tasks.append_log(task, 'MSI 卸载已清除安装目录，跳过手动删除')
    elif install_dir:
        install_path = pathlib.Path(install_dir)
        saved_data_path = ''
        if keep_data and data_dir:
            data_path = pathlib.Path(data_dir)
            if data_path.exists() and data_path.is_dir():
                saved_data_path = str(install_path.parent / f'data_{install_path.name}')
                try:
                    shutil.move(str(data_path), saved_data_path)
                    tasks.append_log(task, f'已将数据目录移至: {saved_data_path}')
                except OSError as e:
                    tasks.append_log(task, f'移动数据目录失败（将继续删除）: {e}')
                    saved_data_path = ''

        on_step(f'正在删除安装目录 {install_dir}...')
        if install_path.exists():
            deleted = False
            # 方式一：shutil.rmtree + 只读属性处理
            try:
                shutil.rmtree(install_path,
                              onerror=_rmtree_readonly_handler)
                if not install_path.exists():
                    deleted = True
                    tasks.append_log(task, f'已删除安装目录: {install_dir}')
            except OSError:
                pass

            # 方式二：cmd.exe rmdir（当前权限）
            if not deleted and install_path.exists():
                ok, err = _rmdir_cmd(str(install_path))
                if ok:
                    deleted = True
                    tasks.append_log(task, f'已删除安装目录: {install_dir}')
                elif err:
                    tasks.append_log(task, f'rmdir: {err[:200]}')

            # 方式三：重试（等待文件句柄释放）
            if not deleted and install_path.exists():
                for attempt in range(1, 3):
                    time.sleep(3)
                    tasks.append_log(task, f'目录仍被占用，第{attempt}次重试...')
                    ok, _ = _rmdir_cmd(str(install_path))
                    if ok:
                        deleted = True
                        tasks.append_log(task, f'已删除安装目录: {install_dir}')
                        break

            # 方式四：SYSTEM 权限计划任务删除（杀进程+删服务+删目录）
            if not deleted and install_path.exists():
                tasks.append_log(
                    task, '当前权限无法删除，启动 SYSTEM 权限清理...')
                _schedule_delete_dir(
                    install_dir, task, service_name=service_name)
                if not install_path.exists():
                    deleted = True
                elif _dir_empty(str(install_path)):
                    deleted = True
                    tasks.append_log(
                        task,
                        f'目录已清空（残留空目录可手动删除）: {install_dir}')

            # 方式五：MoveFileEx 重启后删除（终极兜底）
            if not deleted and install_path.exists():
                remaining = []
                try:
                    for root, dirs, files in os.walk(str(install_path)):
                        for f in files:
                            remaining.append(
                                os.path.relpath(
                                    os.path.join(root, f),
                                    str(install_path)))
                        if len(remaining) > 20:
                            remaining.append('...')
                            break
                except Exception:
                    pass
                if remaining:
                    tasks.append_log(
                        task,
                        f'残留文件({len(remaining)}个): '
                        f'{", ".join(remaining[:10])}')
                import ctypes
                _MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004
                reboot_count = 0
                try:
                    for _root, _dirs, _files in os.walk(str(install_path)):
                        for _fname in _files:
                            _fpath = os.path.join(_root, _fname)
                            try:
                                if ctypes.windll.kernel32.MoveFileExW(
                                        _fpath, None, _MOVEFILE_DELAY_UNTIL_REBOOT):
                                    reboot_count += 1
                            except Exception:
                                pass
                except Exception:
                    pass
                if reboot_count > 0:
                    deleted = True
                    tasks.append_log(task, '已标记残留文件重启后自动删除')
                else:
                    tasks.append_log(
                        task,
                        '警告：无法标记文件（可能需要管理员权限），请手动删除或重启后手动删除')

            if not deleted and install_path.exists():
                tasks.append_log(
                    task,
                    f'警告：安装目录暂时无法删除，将在重启后自动清理: '
                    f'{install_dir}')
        else:
            tasks.append_log(task, '安装目录已不存在，跳过')

    # 3.5 删除独立数据目录（不在安装目录内且不保留数据时）
    if not keep_data and data_dir and install_dir:
        data_path = pathlib.Path(data_dir)
        install_path = pathlib.Path(install_dir)
        try:
            data_in_install = data_path.resolve().is_relative_to(
                install_path.resolve())
        except (ValueError, OSError):
            data_in_install = False
        if not data_in_install and data_path.exists() and data_path.is_dir():
            on_step(f'正在删除数据目录 {data_dir}...')
            data_deleted = False
            try:
                shutil.rmtree(data_path, onerror=_rmtree_readonly_handler)
                if not data_path.exists():
                    data_deleted = True
                    tasks.append_log(task, f'已删除数据目录: {data_dir}')
            except OSError:
                pass
            if not data_deleted and data_path.exists():
                ok, err = _rmdir_cmd(str(data_path))
                if ok:
                    data_deleted = True
                    tasks.append_log(task, f'已删除数据目录: {data_dir}')
                elif err:
                    tasks.append_log(task, f'rmdir: {err[:200]}')
            if not data_deleted and data_path.exists():
                tasks.append_log(
                    task, '当前权限无法删除数据目录，启动 SYSTEM 权限清理...')
                _schedule_delete_dir(data_dir, task)
                if not data_path.exists():
                    data_deleted = True
            if not data_deleted and data_path.exists():
                tasks.append_log(
                    task,
                    f'警告：数据目录暂时无法删除，将在重启后自动清理: {data_dir}')

    # 4. 移除实例记录
    storage.remove_instance(instance['id'])
    _invalidate_port_cache(instance.get('host', '127.0.0.1'),
                           instance.get('port', 3306))
    tasks.set_progress(task, progress=100, stage='卸载完成')
    msg = 'MySQL 已卸载'
    if saved_data_path:
        msg += f'，数据目录已保留至: {saved_data_path}'
    if install_dir and pathlib.Path(install_dir).exists():
        msg += '（安装目录将在重启后自动清理）'
    task['result'] = {'success': True, 'message': msg,
                      'instance_id': instance.get('id', ''),
                      'conn_stale': True}
    log_operation('dbmysql', f'卸载 MySQL {instance.get("version", "")}',
                  detail=f'实例 {instance.get("name", "")}'
                        + (f'，数据目录已保留: {saved_data_path}' if saved_data_path else ''))


@require_GET
def api_instance_config_read(request):
    """读取实例配置文件内容"""
    instance = storage.get_instance(request.GET.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    ini_path = find_my_ini(instance.get('install_dir', ''),
                           instance.get('data_dir', ''))
    if not ini_path or not ini_path.exists():
        return JsonResponse({'success': False, 'message': '未找到配置文件'})
    try:
        content = ini_path.read_text(encoding='utf-8', errors='replace')
        return JsonResponse({'success': True, 'path': str(ini_path), 'content': content})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'读取失败: {e}'})


@require_POST
def api_instance_config_save(request):
    """保存实例配置文件内容"""
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    content = request.POST.get('content', '')
    ini_path = find_my_ini(instance.get('install_dir', ''),
                           instance.get('data_dir', ''))
    if not ini_path:
        return JsonResponse({'success': False, 'message': '未找到配置文件路径'})
    try:
        # 备份原文件
        backup_path = ini_path.with_suffix('.ini.bak')
        if ini_path.exists():
            import shutil
            shutil.copy2(str(ini_path), str(backup_path))
        # 写入新内容
        ini_path.write_text(content, encoding='utf-8')
        return JsonResponse({'success': True,
                             'message': f'配置文件已保存（备份至 {backup_path.name}）'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'保存失败: {e}'})


@require_POST
def api_instance_change_port(request):
    """修改实例端口：更新记录 + 修改 my.ini 中的 port 配置"""
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    new_port_str = request.POST.get('port', '').strip()
    try:
        new_port = int(new_port_str)
        if not 1 <= new_port <= 65535:
            raise ValueError
    except ValueError:
        return JsonResponse({'success': False, 'message': '端口必须为 1~65535 的整数'})
    old_port = int(instance.get('port') or 3306)
    if new_port == old_port:
        return JsonResponse({'success': False, 'message': '新端口与当前端口相同'})
    # 检查端口是否被其他实例占用
    reason = _is_port_occupied(new_port, exclude_instance_id=instance['id'])
    if reason:
        return JsonResponse({'success': False, 'message': reason})
    # 更新实例记录
    storage.update_instance(instance['id'], port=new_port)
    # 端口已变，旧连接必然失效
    _invalidate_port_cache(instance.get('host', '127.0.0.1'), old_port)
    _invalidate_port_cache(instance.get('host', '127.0.0.1'), new_port)
    _clear_instance_conns(request, instance['id'])
    # 尝试修改 my.ini
    ini_msg = ''
    ini_path = find_my_ini(instance.get('install_dir', ''),
                           instance.get('data_dir', ''))
    if ini_path and ini_path.exists():
        try:
            text = ini_path.read_text(encoding='utf-8', errors='replace')
            # 替换 [mysqld] 和 [client] 段中的 port= 行
            new_text = re.sub(
                r'(^\s*port\s*=\s*)\d+',
                rf'\g<1>{new_port}',
                text, flags=re.MULTILINE
            )
            if new_text != text:
                backup = ini_path.with_suffix('.ini.bak')
                import shutil
                shutil.copy2(str(ini_path), str(backup))
                ini_path.write_text(new_text, encoding='utf-8')
                ini_msg = f'，配置文件已同步修改（备份至 {backup.name}）'
            else:
                ini_msg = '，配置文件中未找到 port 配置行，请手动修改'
        except Exception as e:
            ini_msg = f'，但修改配置文件失败: {e}'
    else:
        ini_msg = '，未找到配置文件，请手动修改 my.ini'
    # 提示需要重启
    restart_hint = '。修改后需重启 MySQL 服务才能生效'
    return JsonResponse({
        'success': True,
        'message': f'端口已更新: {old_port} → {new_port}{ini_msg}{restart_hint}'
    })


@require_POST
def api_instance_rename(request):
    """修改实例显示名称：仅更新 name 字段，不触碰任何运行时属性"""
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    new_name = request.POST.get('name', '').strip()
    if not new_name:
        return JsonResponse({'success': False, 'message': '名称不能为空'})
    if len(new_name) > 50:
        return JsonResponse({'success': False, 'message': '名称长度不能超过 50 个字符'})
    if re.search(r'[\x00-\x1f\x7f]', new_name):
        return JsonResponse({'success': False, 'message': '名称包含非法字符'})
    old_name = instance.get('name', '')
    if new_name == old_name:
        return JsonResponse({'success': False, 'message': '新名称与当前名称相同'})
    try:
        storage.update_instance(instance['id'], name=new_name)
    except Exception as e:
        log_operation('dbmysql', f'重命名 MySQL 实例: {old_name} → {new_name}',
                      success=False, detail=f'实例 ID {instance.get("id", "")}，保存失败: {e}')
        return JsonResponse({'success': False, 'message': f'保存失败: {e}'})
    hint = ''
    for other in storage.list_instances():
        if other.get('id') != instance['id'] and other.get('name', '') == new_name:
            hint = '（提示：存在同名实例）'
            break
    log_operation('dbmysql', f'重命名 MySQL 实例: {old_name} → {new_name}',
                  detail=f'实例 ID {instance.get("id", "")}')
    return JsonResponse({
        'success': True,
        'message': f'名称已更新: {old_name} → {new_name}{hint}'
    })


@require_GET
def api_port_check(request):
    """检查端口是否可用，并返回建议端口"""
    port_str = request.GET.get('port', '3306').strip()
    exclude_id = request.GET.get('exclude', '').strip()
    try:
        port = int(port_str)
    except ValueError:
        return JsonResponse({'success': False, 'message': '端口必须为数字'})
    reason = _is_port_occupied(port, exclude_instance_id=exclude_id)
    if not reason:
        return JsonResponse({'success': True, 'available': True, 'port': port, 'message': ''})
    suggested = _find_free_port(port, exclude_instance_id=exclude_id)
    return JsonResponse({'success': True, 'available': False, 'port': port,
                         'suggested': suggested, 'message': reason})


@require_POST
def api_instance_open_dir(request):
    """在资源管理器中打开实例的安装目录/数据目录"""
    instance = storage.get_instance(request.POST.get('id', ''))
    if not instance:
        return JsonResponse({'success': False, 'message': '实例不存在'})
    kind = request.POST.get('kind', 'install')
    if kind not in ('install', 'data'):
        return JsonResponse({'success': False, 'message': '未知的目录类型'})
    path = instance.get('install_dir', '') if kind == 'install' else instance.get('data_dir', '')
    if not path:
        return JsonResponse({'success': False, 'message': '该实例没有登记此目录信息'})
    return JsonResponse(open_directory(path))


@require_POST
def api_clear_downloads(request):
    """清空已下载的 MySQL 安装包（tmp/mysql_download 目录下的文件）"""
    download_dir = mysql_installer.DOWNLOAD_DIR
    if not download_dir.exists():
        return JsonResponse({'success': True, 'message': '下载目录不存在，无需清理',
                             'freed_bytes': 0})
    total_size = 0
    file_count = 0
    for entry in download_dir.iterdir():
        try:
            if entry.is_file():
                total_size += entry.stat().st_size
                entry.unlink()
                file_count += 1
        except OSError as e:
            logger.warning('删除下载文件失败 %s: %s', entry, e)
    size_mb = total_size / 1024 / 1024
    msg = f'已清理 {file_count} 个文件，释放 {size_mb:.1f} MB' if file_count else '下载目录已为空'
    return JsonResponse({'success': True, 'message': msg,
                         'freed_bytes': total_size})


# ── 版本抓取 / 安装 API ──────────────────────────────────

@require_GET
def api_versions(request):
    """版本列表（默认读本地 JSON 缓存，refresh=1 时重新从官网抓取）"""
    refresh = request.GET.get('refresh') == '1'
    return JsonResponse(get_mysql_versions(refresh=refresh))


def _install_worker(task, options):
    """安装 MySQL 的后台任务包装器，完成后记录操作日志。"""
    from apps.dbmysql import mysql_installer
    version = options.get('version', '')
    try:
        mysql_installer.run_install(task, options)
        # run_install 成功时 task 状态已由内部设置为 success
        if task.get('status') == tasks.STATUS_SUCCESS:
            log_operation('dbmysql', f'安装 MySQL {version}',
                          detail=f'安装目录 {options.get("install_dir", "")}，端口 {options.get("port", "")}')
        else:
            log_operation('dbmysql', f'安装 MySQL {version}', success=False,
                          detail=f'安装目录 {options.get("install_dir", "")}，安装未完成')
    except Exception as e:
        log_operation('dbmysql', f'安装 MySQL {version}', success=False, detail=str(e))
        raise


@require_POST
def api_install_start(request):
    """启动自动安装任务"""
    options = {
        'version': request.POST.get('version', '').strip(),
        'url': request.POST.get('url', '').strip(),
        'install_dir': request.POST.get('install_dir', '').strip(),
        'port': request.POST.get('port', '3306').strip(),
        'root_password': request.POST.get('root_password', ''),
        'service_name': request.POST.get('service_name', '').strip(),
        'as_service': request.POST.get('as_service') == '1',
        'charset': request.POST.get('charset', 'utf8mb4').strip(),
    }
    # 服务名留空时自动生成默认名（MySQL<版本去点>），不再报"服务名不正确"
    if not options['service_name']:
        options['service_name'] = f"MySQL{options['version'].replace('.', '')}"
    err = mysql_installer.validate_options(options)
    if err:
        return JsonResponse({'success': False, 'message': err})

    # 端口冲突检测：检查运行中程序 + 已保存实例记录，占用时自动递增
    requested_port = int(options['port'])
    occupied_reason = _is_port_occupied(requested_port)
    if occupied_reason:
        free_port = _find_free_port(requested_port)
        options['port'] = str(free_port)
        logger.info('端口 %d 被占用（%s），自动切换为 %d', requested_port, occupied_reason, free_port)

    # 同一版本重复安装检查
    default_root = str(mysql_installer._get_default_install_root())
    if storage.find_instance(version=options['version'], source='installed',
                             install_dir=str(mysql_installer.Path(options['install_dir'] or default_root) / f"mysql-{options['version']}-winx64")):
        return JsonResponse({'success': False,
                             'message': f'版本 {options["version"]} 已在该目录安装过'})

    task = tasks.create_task('mysql_install', meta={'version': options['version']},
                             title=f'安装 MySQL {options["version"]}')
    tasks.append_log(task, f"安装任务已创建: MySQL {options['version']} -> "
                           f"{options['install_dir'] or default_root}")
    tasks.run_in_thread(task, _install_worker, options)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


@require_GET
def api_install_status(request, task_id):
    """轮询安装任务状态与增量日志。

    安装与服务启停任务共用同一份注册表，本接口与 api_task_status 是同构实现，
    已收敛到 tasks.task_payload()；保留独立路由名仅为兼容模板里的
    {% url "dbmysql:api_install_status" %}。
    """
    task = tasks.get_task(task_id)
    if not task:
        return JsonResponse({'success': False, 'message': '任务不存在'})
    return JsonResponse(tasks.task_payload(task, request.GET.get('after', 0)))


# ── 登录 / 登出 API ──────────────────────────────────────

@require_POST
def api_login(request):
    """登录测试并建立会话连接，未登记的实例自动添加到记录列表

    密码只写入进程内存缓存（_CONN_SECRETS），不落 session。
    """
    host = request.POST.get('host', '').strip() or '127.0.0.1'
    port = request.POST.get('port', '').strip() or '3306'
    user = request.POST.get('user', '').strip()
    password = request.POST.get('password', '')
    instance_id = request.POST.get('instance_id', '').strip()
    name = request.POST.get('name', '').strip()

    if not user:
        return JsonResponse({'success': False, 'message': '请输入用户名'})
    try:
        port = int(port)
    except ValueError:
        return JsonResponse({'success': False, 'message': '端口必须为数字'})

    try:
        info = mc.test_login(host, port, user, password)
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})

    # 若未关联已有实例，按 host:port 查找或自动登记
    if not instance_id or not storage.get_instance(instance_id):
        existing = storage.find_instance(host=host, port=port)
        if existing:
            instance_id = existing['id']
            if not name:
                name = existing.get('name', '')
        else:
            # 自动记录到实例列表（远程连接也记录下来便于下次连接）
            auto_name = name or f'{user}@{host}:{port}'
            record = storage.add_instance({
                'name': auto_name,
                'host': host,
                'port': port,
                'version': info.get('version', ''),
                'source': 'remote',
                'default_user': user,
            })
            instance_id = record['id']
            if not name:
                name = auto_name
            logger.info('自动登记远程 MySQL 实例: %s:%s', host, port)

    import uuid
    conns = _get_conns(request)
    if not name and instance_id:
        instance = storage.get_instance(instance_id)
        name = instance.get('name', '') if instance else ''

    # 同一实例不重复创建连接：若已有活跃连接则复用其 conn_id
    existing_conn_id = ''
    if instance_id:
        for cid, cinfo in conns.items():
            if cinfo.get('instance_id') == instance_id:
                existing_conn_id = cid
                break

    if existing_conn_id:
        # 复用已有连接（更新凭据，避免重复登录同一实例）
        conn_id = existing_conn_id
        conns[conn_id].update({
            'host': host, 'port': port, 'user': user,
            'name': name or f'{user}@{host}:{port}',
            'version': info.get('version', ''),
        })
        _save_conns(request, conns)
        _set_secret(conn_id, password)
    else:
        conn_id = uuid.uuid4().hex[:10]
        conns[conn_id] = {
            'host': host, 'port': port, 'user': user,
            'name': name or f'{user}@{host}:{port}',
            'version': info.get('version', ''),
            'instance_id': instance_id,
        }
        _save_conns(request, conns)
        _set_secret(conn_id, password)

    return JsonResponse({
        'success': True,
        'message': f"登录成功，MySQL 版本 {info.get('version', '')}",
        'redirect': reverse('dbmysql:databases', kwargs={'conn_id': conn_id}),
    })


@require_POST
def api_logout(request):
    """关闭会话连接"""
    conn_id = request.POST.get('conn_id', '')
    conns = _get_conns(request)
    if conn_id in conns:
        del conns[conn_id]
        _save_conns(request, conns)
        _drop_secret(conn_id)
    return JsonResponse({'success': True, 'message': '已退出连接',
                         'redirect': reverse('dbmysql:index')})


@require_POST
def api_instance_conn_clear(request):
    """清理指定实例在本会话中的所有连接。

    供前端在「停止 / 重启 / 卸载」任务轮询到成功后调用，即时把该实例
    的登录状态置为未登录（后台任务线程拿不到 request.session，只能由
    前端回调触发主线程清理）。首页本身也有惰性清理兜底。
    """
    instance_id = request.POST.get('id', '')
    if not instance_id:
        return JsonResponse({'success': False, 'message': '缺少实例 id'})
    cleared = _clear_instance_conns(request, instance_id)
    return JsonResponse({'success': True, 'cleared': cleared})


@require_POST
@_require_conn
def api_change_password(request, conn_id):
    """修改当前登录用户的密码"""
    new_password = request.POST.get('new_password', '')
    confirm_password = request.POST.get('confirm_password', '')
    if not new_password:
        return JsonResponse({'success': False, 'message': '新密码不能为空'})
    if new_password != confirm_password:
        return JsonResponse({'success': False, 'message': '两次输入的密码不一致'})
    conn = None
    try:
        conn = _open_conn(request, conn_id)
        mc.change_password(conn, new_password)
        # 更新内存中保存的密码（不写入 session）
        _set_secret(conn_id, new_password)
        return JsonResponse({'success': True, 'message': '密码修改成功'})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


# ── 库 / 表 / 字段管理 API ───────────────────────────────

@require_GET
@_require_conn
def api_charsets(request, conn_id):
    """获取当前 MySQL 实例可用字符集及其排序规则"""
    conn = None
    try:
        conn = _open_conn(request, conn_id)
        data = mc.get_charsets(conn)
        return JsonResponse({'success': True, **data})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


@require_GET
@_require_conn
def api_db_list(request, conn_id):
    """列出数据库（排除 MySQL 系统库）"""
    SYSTEM_DBS = {'mysql', 'information_schema', 'performance_schema', 'sys'}
    conn = None
    try:
        conn = _open_conn(request, conn_id)
        dbs = mc.list_databases(conn)
        names = [d['name'] for d in dbs if d.get('name', '').lower() not in SYSTEM_DBS]
        return JsonResponse({'success': True, 'databases': sorted(names)})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


@require_GET
@_require_conn
def api_table_list(request, conn_id, db):
    """列出指定库的表"""
    conn = None
    try:
        conn = _open_conn(request, conn_id, db=db)
        tables = mc.list_tables(conn, db)
        names = [t['name'] for t in tables]
        return JsonResponse({'success': True, 'tables': names})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


@require_POST
@_require_conn
def api_db_action(request, conn_id):
    """数据库操作: create / drop"""
    action = request.POST.get('action', '')
    conn = None
    try:
        conn = _open_conn(request, conn_id)
        if action == 'create':
            mc.create_database(conn, request.POST.get('name', '').strip(),
                               request.POST.get('charset', 'utf8mb4'),
                               request.POST.get('collation', ''))
            return JsonResponse({'success': True, 'message': '数据库创建成功'})
        if action == 'drop':
            mc.drop_database(conn, request.POST.get('name', '').strip())
            return JsonResponse({'success': True, 'message': '数据库已删除'})
        return JsonResponse({'success': False, 'message': f'未知操作: {action}'})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


@require_POST
@_require_conn
def api_table_action(request, conn_id):
    """表操作: create / drop / truncate"""
    action = request.POST.get('action', '')
    db = request.POST.get('db', '').strip()
    table = request.POST.get('table', '').strip()
    conn = None
    try:
        conn = _open_conn(request, conn_id, db=db)
        if action == 'create':
            mc.create_table(conn, db, table,
                            request.POST.get('engine', 'InnoDB'),
                            request.POST.get('charset', 'utf8mb4'),
                            request.POST.get('collation', ''),
                            request.POST.get('comment', ''))
            return JsonResponse({'success': True, 'message': '表创建成功'})
        if action == 'drop':
            mc.drop_table(conn, db, table)
            return JsonResponse({'success': True, 'message': '表已删除'})
        if action == 'truncate':
            mc.truncate_table(conn, db, table)
            return JsonResponse({'success': True, 'message': '表数据已清空'})
        return JsonResponse({'success': False, 'message': f'未知操作: {action}'})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


@require_POST
@_require_conn
def api_column_action(request, conn_id):
    """字段操作: add / modify / drop"""
    action = request.POST.get('action', '')
    db = request.POST.get('db', '').strip()
    table = request.POST.get('table', '').strip()
    name = request.POST.get('name', '').strip()
    conn = None
    try:
        conn = _open_conn(request, conn_id, db=db)
        if action == 'drop':
            mc.drop_column(conn, db, table, name)
            return JsonResponse({'success': True, 'message': '字段已删除'})
        if action in ('add', 'modify'):
            field_type = request.POST.get('type', '').strip()
            nullable = request.POST.get('nullable') == '1'
            has_default = request.POST.get('has_default') == '1'
            default = request.POST.get('default', '')
            comment = request.POST.get('comment', '')
            primary_key = request.POST.get('primary_key') == '1'
            position = request.POST.get('position', '')
            after_column = request.POST.get('after_column', '').strip()
            if action == 'add':
                mc.add_column(conn, db, table, name, field_type, nullable,
                              has_default, default, comment, primary_key,
                              position, after_column)
                return JsonResponse({'success': True, 'message': '字段添加成功'})
            mc.modify_column(conn, db, table, name, field_type, nullable,
                             has_default, default, comment,
                             position, after_column)
            return JsonResponse({'success': True, 'message': '字段修改成功'})
        return JsonResponse({'success': False, 'message': f'未知操作: {action}'})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


# ── 用户管理 API ────────────────────────────────────

@require_GET
@_require_conn
def api_users_list(request, conn_id):
    """列出 MySQL 用户"""
    conn = None
    try:
        conn = _open_conn(request, conn_id)
        users = mc.list_users(conn)
        return JsonResponse({'success': True, 'users': users})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


@require_POST
@_require_conn
def api_user_action(request, conn_id):
    """用户操作: create / drop / password"""
    action = request.POST.get('action', '')
    user = request.POST.get('user', '').strip()
    host = request.POST.get('host', '').strip() or '%'
    conn = None
    try:
        conn = _open_conn(request, conn_id)
        if action == 'create':
            password = request.POST.get('password', '')
            if not password:
                return JsonResponse({'success': False, 'message': '请设置初始密码'})
            mc.create_user(conn, user, host, password)
            return JsonResponse({'success': True, 'message': f'用户 {user}@{host} 创建成功'})
        if action == 'drop':
            mc.drop_user(conn, user, host)
            return JsonResponse({'success': True, 'message': f'用户 {user}@{host} 已删除'})
        if action == 'password':
            new_pwd = request.POST.get('new_password', '')
            if not new_pwd:
                return JsonResponse({'success': False, 'message': '请输入新密码'})
            mc.alter_user_password(conn, user, host, new_pwd)
            return JsonResponse({'success': True, 'message': f'用户 {user}@{host} 密码已更新'})
        return JsonResponse({'success': False, 'message': f'未知操作: {action}'})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


@require_POST
@_require_conn
def api_user_grants(request, conn_id):
    """权限操作: grant / revoke / list_grants"""
    action = request.POST.get('action', '')
    user = request.POST.get('user', '').strip()
    host = request.POST.get('host', '').strip() or '%'
    conn = None
    try:
        conn = _open_conn(request, conn_id)
        if action == 'list_grants':
            grants = mc.get_user_grants(conn, user, host)
            return JsonResponse({'success': True, 'grants': grants})
        if action in ('grant', 'revoke'):
            privileges = request.POST.get('privileges', '').strip()
            db = request.POST.get('db', '*').strip() or '*'
            table = request.POST.get('table', '*').strip() or '*'
            if not privileges:
                return JsonResponse({'success': False, 'message': '请指定要授权的权限'})
            if action == 'grant':
                mc.grant_privileges(conn, user, host, privileges, db, table)
                return JsonResponse({'success': True, 'message': f'已授予 {user}@{host} 权限: {privileges}'})
            mc.revoke_privileges(conn, user, host, privileges, db, table)
            return JsonResponse({'success': True, 'message': f'已撤销 {user}@{host} 权限: {privileges}'})
        return JsonResponse({'success': False, 'message': f'未知操作: {action}'})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


# ── 表数据浏览 API ────────────────────────────────────

@require_GET
@_require_conn
def api_table_data(request, conn_id, db, table):
    """分页查询表数据（带大表 OFFSET 自动降级）"""
    page = request.GET.get('page', '1')
    page_size = request.GET.get('page_size', '100')
    try:
        page = int(page)
        page_size = int(page_size)
    except ValueError:
        page, page_size = 1, 100
    conn = None
    try:
        conn = _open_conn(request, conn_id, db=db)
        data = mc.query_table_data(conn, db, table, page=page, page_size=page_size)
        # 转义 datetime 为字符串，便于 JSON 序列化
        for row in data['rows']:
            for i, v in enumerate(row):
                if hasattr(v, 'isoformat'):
                    row[i] = v.isoformat()
                elif v is None:
                    row[i] = None
                else:
                    try:
                        row[i] = str(v) if isinstance(v, (bytes, bytearray)) else v
                    except Exception:
                        row[i] = str(v)
        return JsonResponse({'success': True, 'data': data})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


@require_GET
@_require_conn
def api_table_show_create(request, conn_id, db, table):
    """获取建表语句"""
    conn = None
    try:
        conn = _open_conn(request, conn_id, db=db)
        ddl = mc.show_create_table(conn, db, table)
        return JsonResponse({'success': True, 'ddl': ddl})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


# ── 索引管理 API ──────────────────────────────────────

@require_GET
@_require_conn
def api_index_list(request, conn_id, db, table):
    """列出表的索引"""
    conn = None
    try:
        conn = _open_conn(request, conn_id, db=db)
        indexes = mc.list_indexes(conn, db, table)
        # 按 key_name 分组
        grouped = {}
        for idx in indexes:
            kn = idx['key_name']
            if kn not in grouped:
                grouped[kn] = {'key_name': kn, 'columns': [],
                               'non_unique': idx['non_unique'],
                               'index_type': idx['index_type']}
            grouped[kn]['columns'].append(idx['column_name'])
        return JsonResponse({'success': True, 'indexes': list(grouped.values())})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


@require_POST
@_require_conn
def api_index_action(request, conn_id):
    """索引操作: create / drop"""
    action = request.POST.get('action', '')
    db = request.POST.get('db', '').strip()
    table = request.POST.get('table', '').strip()
    conn = None
    try:
        conn = _open_conn(request, conn_id, db=db)
        if action == 'create':
            idx_name = request.POST.get('index_name', '').strip()
            columns = request.POST.get('columns', '').strip()
            idx_type = request.POST.get('index_type', 'INDEX').strip()
            if not idx_name or not columns:
                return JsonResponse({'success': False, 'message': '请指定索引名和字段'})
            col_list = [c.strip() for c in columns.split(',') if c.strip()]
            mc.create_index(conn, db, table, idx_name, col_list, idx_type)
            return JsonResponse({'success': True, 'message': f'索引 {idx_name} 创建成功'})
        if action == 'drop':
            idx_name = request.POST.get('index_name', '').strip()
            if not idx_name:
                return JsonResponse({'success': False, 'message': '请指定索引名'})
            mc.drop_index(conn, db, table, idx_name)
            return JsonResponse({'success': True, 'message': f'索引 {idx_name} 已删除'})
        return JsonResponse({'success': False, 'message': f'未知操作: {action}'})
    except mc.MysqlError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    finally:
        if conn:
            conn.close()


# ── 导入导出 API（mysqldump / source） ────────────────

@require_POST
@_require_conn
def api_db_export(request, conn_id):
    """数据库导出（mysqldump）"""
    db = request.POST.get('db', '').strip()
    if not db:
        return JsonResponse({'success': False, 'message': '请指定数据库'})

    info = get_conn_info(request, conn_id)
    if not info:
        return JsonResponse({'success': False, 'message': '连接已过期'})
    password = _get_secret(conn_id)
    if password is None:
        return JsonResponse({'success': False, 'message': '连接凭据已过期'})

    # 找 mysqldump（和 mysqld 在同一目录）
    import pathlib as pl
    mysqldump = ''
    for inst in storage.list_instances():
        inst_src = inst.get('source', '')
        inst_host = inst.get('host', '127.0.0.1')
        inst_port = int(inst.get('port') or 3306)
        if inst_host == info.get('host', '127.0.0.1') and inst_port == int(info.get('port') or 3306):
            install_dir = inst.get('install_dir', '')
            if install_dir:
                p = pl.Path(install_dir) / 'bin' / 'mysqldump.exe'
                if p.exists():
                    mysqldump = str(p)
                    break

    if not mysqldump:
        import shutil as _sh
        mysqldump = _sh.which('mysqldump.exe') or _sh.which('mysqldump') or ''
        if not mysqldump:
            return JsonResponse({'success': False,
                                 'message': '未找到 mysqldump.exe，请确认 MySQL 安装完整'})

    import subprocess as sp
    import tempfile
    import uuid
    out_file = pl.Path(tempfile.gettempdir()) / f'dbmysql_export_{db}_{uuid.uuid4().hex[:8]}.sql'
    env = os.environ.copy()
    env['MYSQL_PWD'] = password

    cmd = [mysqldump,
           '-h', info.get('host', '127.0.0.1'),
           '-P', str(info.get('port', 3306)),
           '-u', info.get('user', 'root'),
           '--single-transaction', '--routines', '--triggers',
           db]
    try:
        proc = sp.run(cmd, capture_output=True, timeout=300,
                      creationflags=_CREATE_NO_WINDOW, env=env)
        out_file.write_bytes(proc.stdout)
        if proc.returncode != 0:
            err = (proc.stderr or b'').decode(get_cmd_encoding(), errors='replace')
            return JsonResponse({'success': False, 'message': f'导出失败: {err[:300]}'})
    except sp.TimeoutExpired:
        return JsonResponse({'success': False, 'message': '导出超时（5 分钟）'})

    from django.http import FileResponse
    response = FileResponse(open(str(out_file), 'rb'),
                            as_attachment=True,
                            filename=f'{db}_backup.sql')
    # 下载后清理临时文件
    def _cleanup():
        import time as _t; _t.sleep(60)
        try: out_file.unlink()
        except: pass
    import threading as _th
    _th.Thread(target=_cleanup, daemon=True).start()
    return response


# ── 应用设置 ───────────────────────────────────────

class SettingsView(DbmysqlMixin, TemplateView):
    """应用设置页：MySQL 默认安装目录等配置"""
    template_name = 'dbmysql/settings.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'MySQL 设置'
        context['config'] = app_config.load_config()
        context['breadcrumb'] = [
            {
                'title': 'MySQL管理',
                'href': reverse('dbmysql:index'),
                'active': False
            },
            {
                'title': '设置',
                'href': '',
                'active': True
            },
        ]
        return context


@require_GET
def api_settings(request):
    """获取当前应用配置"""
    return JsonResponse({'success': True, 'config': app_config.load_config()})


@require_POST
def api_settings_save(request):
    """保存应用配置"""
    config = app_config.load_config()
    install_dir = request.POST.get('install_dir', '').strip()
    if install_dir:
        p = pathlib.Path(install_dir)
        if not p.exists():
            return JsonResponse({'success': False,
                                 'message': f'目录不存在: {install_dir}'})
        config['install_dir'] = install_dir
    app_config.save_config(config)
    return JsonResponse({'success': True, 'message': '设置已保存'})