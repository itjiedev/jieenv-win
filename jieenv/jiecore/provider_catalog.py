"""
provider_catalog — jieEnv 能力提供者注册表
===============================================
三层积木架构第一层（发现层），配合 event_bus（事件层）+ app_services（调用层）。

核心设计原则：
  1. provider_key = "{category}.{provider_id}"，如 "db.mysql"、"runtime.python"
  2. 外部应用只通过 provider_catalog.list_providers() 等查询，零 import 具体扩展应用
  3. invoke_provider 走 lifecycle.{capability} → capabilities.{capability} 两级查找，带健康缓存降级
  4. provider 注册/注销自动 emit event_bus 事件 + 写入 _EventJournal 防丢
"""
import os
import logging
import threading
import time
import inspect
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Any, Optional

from . import event_bus
from .app_services import call_service

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# ProviderResult：invoke_provider 的统一返回结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class ProviderResult:
    success: bool
    data: Any = None
    error_code: str = ''
    error_msg: str = ''

    # 错误码常量
    E_NOT_FOUND    = 'PROVIDER_NOT_FOUND'
    E_UNHEALTHY    = 'PROVIDER_UNHEALTHY'
    E_NO_CAPABILITY = 'CAPABILITY_NOT_FOUND'
    E_CALL_FAILED  = 'CALL_FAILED'
    E_APP_DISABLED = 'APP_DISABLED'

    @classmethod
    def ok(cls, data):
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, code: str, msg: str):
        return cls(success=False, error_code=code, error_msg=msg)


# ═══════════════════════════════════════════════════════════════
# 内部模块
# ═══════════════════════════════════════════════════════════════

class _ProviderHealthCache:
    """provider 健康状态缓存。30s 后台定时刷新，不每次 invoke 都做实时 health_check。"""

    def __init__(self):
        self._states: dict[str, str] = {}
        self._lock = threading.RLock()
        self._timer: Optional[threading.Thread] = None
        self._interval = 30
        self._stop = threading.Event()

    # ── 公共 API ────────────────────────────────────────

    def get_state(self, provider_key: str) -> str:
        with self._lock:
            return self._states.get(provider_key, 'UNKNOWN')

    def refresh(self, provider_key: str = None) -> None:
        """刷新单个或全部 provider 健康状态。"""
        from . import provider_catalog as _mod  # 延迟引用避免循环

        with self._lock:
            if provider_key:
                target_keys = [provider_key]
            else:
                target_keys = list(_PROVIDERS.keys())

        for pk in target_keys:
            provider = _PROVIDERS.get(pk)
            if not provider:
                with self._lock:
                    self._states.pop(pk, None)
                continue

            new_state = self._check_one(pk, provider)
            old_state = self._states.get(pk, 'UNKNOWN')

            with self._lock:
                self._states[pk] = new_state

            if new_state != old_state:
                event_bus.emit(event_bus.EVENT_PROVIDER_HEALTH_CHANGED, {
                    'provider_key': pk,
                    'state': new_state,
                    'prev_state': old_state,
                })

    def clear(self, provider_key: str) -> None:
        with self._lock:
            self._states.pop(provider_key, None)

    # ── 后台线程 ─────────────────────────────────────────

    def start_auto_refresh(self, interval: int = 30) -> None:
        """启动后台定时刷新线程。幂等。"""
        if self._timer and self._timer.is_alive():
            return
        self._interval = interval
        self._stop.clear()

        def _loop():
            while not self._stop.wait(self._interval):
                try:
                    self.refresh()
                except Exception:
                    logger.exception('_ProviderHealthCache 定时刷新异常')

        self._timer = threading.Thread(target=_loop, daemon=True)
        self._timer.start()

    def stop_auto_refresh(self) -> None:
        self._stop.set()

    # ── 内部 ─────────────────────────────────────────────

    def _check_one(self, provider_key: str, provider: dict) -> str:
        lifecycle = provider.get('lifecycle') or {}
        health_entry = lifecycle.get('health')
        if not health_entry:
            return 'OK'
        svc = health_entry.get('service')
        if not svc:
            return 'OK'

        handler = None
        try:
            from .app_services import _SERVICE_REGISTRY
            app_name, svc_name = svc[0], svc[1]
            handler = _SERVICE_REGISTRY.get(app_name, {}).get(svc_name, {}).get('handler')
        except Exception:
            handler = None

        if handler is not None:
            try:
                sig = inspect.signature(handler)
                required_positional = sum(
                    1 for p in sig.parameters.values()
                    if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                  inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    and p.default is inspect.Parameter.empty
                    and p.name != 'self'
                )
                if required_positional > 0:
                    logger.warning(
                        'provider %s health service %s 有 %d 个必填参数，'
                        '健康缓存无法自动检测，请改为 provider 级别无参数的健康检测',
                        provider_key, svc, required_positional
                    )
                    return 'UNKNOWN'
            except (TypeError, ValueError):
                pass

        try:
            result = call_service(*svc)
            if isinstance(result, dict) and result.get('success'):
                return 'OK'
            if isinstance(result, dict) and 'success' in result:
                return 'DEGRADED'
            return 'OK'
        except Exception:
            return 'OK'


class _EventJournal:
    """轻量级事件日志，环形队列防丢。供 project_mgmt 启动时补漏。"""

    def __init__(self, maxlen: int = 1000):
        self._journal: deque = deque(maxlen=maxlen)
        self._lock = threading.RLock()
        self._cursor = 0

    def append(self, event_name: str, payload: dict) -> None:
        with self._lock:
            self._cursor += 1
            entry = {
                'cursor': self._cursor,
                'event': event_name,
                'payload': payload or {},
                'ts': time.time(),
            }
            self._journal.append(entry)
        # emit 在锁外，避免订阅者回调阻塞写入
        event_bus.emit(event_name, payload)

    def get_since(self, cursor: int) -> list[dict]:
        with self._lock:
            return [e for e in self._journal if e['cursor'] > cursor]

    @property
    def current_cursor(self) -> int:
        with self._lock:
            return self._cursor


# ═══════════════════════════════════════════════════════════════
# 全局注册表（模块级私有）
# ═══════════════════════════════════════════════════════════════

_PROVIDERS: dict[str, dict] = {}
_CATEGORIES: dict[str, dict] = {}
_PROVIDER_APP_INDEX: dict[str, str] = {}
_REGISTRY_LOCK = threading.RLock()
_bootstrap_done = False
_bootstrap_lock = threading.Lock()

_health_cache = _ProviderHealthCache()
_event_journal = _EventJournal()


def _ensure_bootstrap() -> None:
    global _bootstrap_done
    if _bootstrap_done:
        return
    with _bootstrap_lock:
        if not _bootstrap_done and not _PROVIDERS:
            bootstrap_providers()
            _bootstrap_done = True


# ═══════════════════════════════════════════════════════════════
# 公共 API：Category 动态注册
# ═══════════════════════════════════════════════════════════════

def register_category(category_id: str, label: str,
                      icon: str = '', description: str = '') -> None:
    with _REGISTRY_LOCK:
        _CATEGORIES[category_id] = {
            'category_id': category_id,
            'label': label,
            'icon': icon,
            'description': description,
        }
    _event_journal.append(event_bus.EVENT_CATEGORY_REGISTERED, {
        'category_id': category_id, 'label': label, 'icon': icon,
    })


def list_categories() -> list:
    _ensure_bootstrap()
    with _REGISTRY_LOCK:
        return [dict(v) for v in _CATEGORIES.values()]


# ═══════════════════════════════════════════════════════════════
# 公共 API：Provider 注册/注销
# ═══════════════════════════════════════════════════════════════

def register_provider(provider_def: dict) -> str:
    """注册 provider，返回 provider_key。

    自动行为：
      - category 未注册则先调 register_category
      - emit('provider.registered') + _event_journal.append(...)
      - _health_cache.refresh(provider_key)
    """
    category = provider_def.get('category')
    provider_id = provider_def.get('provider_id')
    if not category or not provider_id:
        raise ValueError("provider_def must contain 'category' and 'provider_id'")

    provider_key = f"{category}.{provider_id}"

    with _REGISTRY_LOCK:
        if category not in _CATEGORIES:
            register_category(category, label=category)
        _PROVIDERS[provider_key] = provider_def
        _PROVIDER_APP_INDEX[provider_key] = provider_def.get('app_name', '')

    _health_cache.refresh(provider_key)
    _event_journal.append(event_bus.EVENT_PROVIDER_REGISTERED, {
        'provider_key': provider_key,
        'provider_def': provider_def,
    })

    return provider_key


def unregister_providers_by_app(app_name: str) -> list:
    """注销归属某应用的所有 provider。返回被注销的 provider_key 列表。"""
    removed_keys = []
    with _REGISTRY_LOCK:
        for pk, pdef in list(_PROVIDERS.items()):
            if pdef.get('app_name') == app_name:
                removed_keys.append(pk)
                del _PROVIDERS[pk]

    for pk in removed_keys:
        _health_cache.clear(pk)
        _event_journal.append(event_bus.EVENT_PROVIDER_UNREGISTERED, {
            'provider_key': pk, 'app_name': app_name,
        })

    return removed_keys


def _cleanup_provider_app_index(app_name: str) -> None:
    """卸载应用时清理 provider→app 索引（禁用/enable 时不清，保留声明痕迹）"""
    global _PROVIDER_APP_INDEX
    _PROVIDER_APP_INDEX = {
        pk: an for pk, an in _PROVIDER_APP_INDEX.items()
        if an != app_name
    }


def find_provider_app(provider_key: str) -> Optional[str]:
    """反查 provider 归属哪个应用（包括已禁用的，基于 _PROVIDER_APP_INDEX）"""
    if provider_key in _PROVIDERS:
        return _PROVIDERS[provider_key].get('app_name')
    return _PROVIDER_APP_INDEX.get(provider_key)


# ═══════════════════════════════════════════════════════════════
# 公共 API：发现/查询
# ═══════════════════════════════════════════════════════════════

def list_providers(category: str = None) -> list:
    _ensure_bootstrap()
    with _REGISTRY_LOCK:
        result = []
        for pk, pdef in _PROVIDERS.items():
            if category and pdef.get('category') != category:
                continue
            result.append({**pdef, 'provider_key': pk})
        return result


def get_provider(provider_key: str) -> Optional[dict]:
    _ensure_bootstrap()
    with _REGISTRY_LOCK:
        pdef = _PROVIDERS.get(provider_key)
        if pdef:
            return {**pdef, 'provider_key': provider_key}
        return None


def has_provider(provider_key: str) -> bool:
    return get_provider(provider_key) is not None


# ═══════════════════════════════════════════════════════════════
# query_projects_by_bindings（Phase 1 stub）
# ═══════════════════════════════════════════════════════════════

def query_projects_by_bindings(provider_key: str = None,
                               category: str = None,
                               health_state: str = None,
                               all_bindings: list = None) -> list:
    """组合查询：哪些项目正在使用匹配条件的 provider。

    ⚠️ Phase 0 v2.0 预埋签名 — 实际实现依赖 project_mgmt/storage.py
    （项目 bindings 存储在 project_mgmt 里，provider_catalog 不反向 import）。
    Phase 1 storage.py 落地后由 project_mgmt 注册具体实现到此 stub。
    """
    logger.debug(
        'query_projects_by_bindings called with pk=%s cat=%s state=%s '
        '(Phase 0 stub — 依赖 Phase 1 project_mgmt storage.py)',
        provider_key, category, health_state,
    )
    return []


# ═══════════════════════════════════════════════════════════════
# 公共 API：安全调用（invoke_provider）
# ═══════════════════════════════════════════════════════════════

def invoke_provider(provider_key: str, capability: str,
                    *args, **kwargs) -> ProviderResult:
    """安全调用 provider 的某个 capability。带降级保护。

    查找顺序：
      ① provider 不存在 → PROVIDER_NOT_FOUND
      ② 查健康缓存 → DEGRADED 直接返回 PROVIDER_UNHEALTHY
      ③ 优先查 lifecycle.{capability}（统一接口）
      ④ 找不到再查 capabilities.{capability}（自由扩展）
      ⑤ 还找不到 → CAPABILITY_NOT_FOUND
      ⑥ 调 service → try/except → CALL_FAILED
      ⑦ 成功 → ProviderResult.ok(data)
    """
    _ensure_bootstrap()
    provider = get_provider(provider_key)
    if not provider:
        owner_app = _PROVIDER_APP_INDEX.get(provider_key)
        if owner_app:
            from .app_manager import AppManager
            all_apps = AppManager.get_all_apps()
            if owner_app in all_apps:
                return ProviderResult.fail(
                    ProviderResult.E_APP_DISABLED,
                    f'provider 已禁用: {provider_key}（应用 {owner_app} 未启用，请前往应用管理启用）'
                )
        return ProviderResult.fail(
            ProviderResult.E_NOT_FOUND,
            f'provider 未安装或不存在: {provider_key}'
        )

    state = _health_cache.get_state(provider_key)
    if state == 'DEGRADED' and capability != 'verify':
        return ProviderResult.fail(
            ProviderResult.E_UNHEALTHY,
            f'provider {provider_key!r} 健康状态异常'
        )

    lifecycle = provider.get('lifecycle') or {}
    capabilities = provider.get('capabilities') or {}

    entry = lifecycle.get(capability) or capabilities.get(capability)
    if not entry:
        return ProviderResult.fail(
            ProviderResult.E_NO_CAPABILITY,
            f'provider {provider_key!r} 没有 capability {capability!r}'
        )

    svc = entry.get('service')
    if not svc or not isinstance(svc, (tuple, list)) or len(svc) != 2:
        return ProviderResult.fail(
            ProviderResult.E_NO_CAPABILITY,
            f'capability {capability!r} 缺少有效的 service 入口'
        )

    user_config = kwargs.pop('config', {}) or {}
    project_name = kwargs.pop('project_name', '')
    project_dir = kwargs.pop('project_dir', '')

    schema_fields = entry.get('config_schema', []) or []

    # ── 扩展：预取 select + options_from 字段的动态选项 ──
    for field in schema_fields:
        if field.get('type') == 'select' and 'options_from' in field:
            src = field['options_from']
            try:
                cap = src.get('capability')
                if not cap:
                    field['options'] = []
                    continue
                if isinstance(cap, (list, tuple)):
                    raw = call_service(*cap)
                else:
                    rr = invoke_provider(provider_key, cap)
                    raw = rr.data if rr.success else []
                field['options'] = raw if isinstance(raw, list) else []
            except Exception:
                field['options'] = []

    # ── 扩展：解析 default.match 动态预选 ──
    for field in schema_fields:
        if (field.get('type') == 'select'
                and isinstance(field.get('default'), dict)
                and 'match' in field['default']):
            match = field['default']['match']
            options = field.get('options', [])
            if options:
                if match == 'default':
                    picked = next((o for o in options if o.get('default')), None)
                else:
                    picked = next((o for o in options if match in str(o.get('label', ''))), None)
                if picked:
                    field['_resolved_default'] = picked.get('value')

    service_kwargs = {}

    for field in schema_fields:
        key = field.get('key', '')
        if not key:
            continue

        if key in user_config and user_config[key] not in (None, ''):
            val = user_config[key]
        elif field.get('default_from') == 'project_name':
            val = project_name
        elif field.get('default_from') == 'project_dir':
            raw_default = field.get('default', '')
            if raw_default and not os.path.isabs(str(raw_default)):
                val = os.path.join(project_dir, str(raw_default))
            else:
                val = project_dir
        elif '_resolved_default' in field:
            val = field['_resolved_default']
        elif 'default' in field and not isinstance(field['default'], dict):
            val = field['default']
        else:
            continue

        if field.get('type') == 'checkbox':
            val = bool(val) if isinstance(val, bool) else str(val).lower() in ('true', '1', 'on')

        service_kwargs[key] = val

    if not schema_fields:
        service_kwargs.update(kwargs)

    service_mode = entry.get('service_mode', 'kwargs')
    try:
        if service_mode == 'context_dict':
            data = call_service(svc[0], svc[1], service_kwargs)
        else:
            data = call_service(svc[0], svc[1], **service_kwargs)
        if isinstance(data, dict) and data.get('success') is False:
            return ProviderResult(
                success=False,
                error_code=ProviderResult.E_CALL_FAILED,
                error_msg=data.get('error') or data.get('message') or '业务执行失败',
                data=data,
            )
        return ProviderResult.ok(data)
    except Exception as e:
        logger.warning('invoke_provider %s.%s failed: %s', provider_key, capability, e, exc_info=True)
        return ProviderResult.fail(
            ProviderResult.E_CALL_FAILED,
            f'调用 {provider_key}.{capability} 失败: {e}'
        )


# ═══════════════════════════════════════════════════════════════
# 内部访问器（供 project_mgmt apps.py ready() 使用）
# ═══════════════════════════════════════════════════════════════

def _get_health_cache() -> _ProviderHealthCache:
    return _health_cache


def _get_event_journal() -> _EventJournal:
    return _event_journal


# ═══════════════════════════════════════════════════════════════
# 启动时扫描注册（与 scan_all_ext_hooks 同时机）
# ═══════════════════════════════════════════════════════════════

def bootstrap_providers() -> None:
    """扫描所有已启用应用的 hooks.py，调 on_enable 注册 provider。

    在 refresh_menu_cache() 里紧随 scan_all_ext_hooks() 之后调用，
    确保 Django 启动/refreshcache 时 provider_catalog 里有完整注册。

    同时扫描所有已安装应用（含禁用）的 hooks.py 源码，用 AST 静态提取
    provider_key → app_name 映射，写入 _PROVIDER_APP_INDEX，使
    find_provider_app() 能区分"已禁用" vs "未安装"。
    """
    import ast as _ast

    from .app_manager import AppManager, app_dir
    from .app_hooks import call_hook, build_context

    enabled = AppManager.get_enabled_apps()

    for app_name in enabled:
        if app_name in ('jiecore',):
            continue

        hook_ctx = build_context(app_name, 'bootstrap')
        try:
            call_hook(app_name, 'on_enable', hook_ctx)
        except Exception:
            logger.exception('bootstrap_providers: %s on_enable 异常', app_name)

    # 静态扫描所有已安装应用（包括禁用的），补齐 _PROVIDER_APP_INDEX
    # 只写索引，不真正注册到 _PROVIDERS（禁用应用不应该可用）
    all_apps = AppManager.get_all_apps()
    for app_name in all_apps:
        if app_name in enabled or app_name in ('jiecore',):
            continue
        hooks_path = app_dir(app_name) / 'hooks.py'
        if not hooks_path.exists():
            continue
        try:
            _source = hooks_path.read_text(encoding='utf-8')
            _tree = _ast.parse(_source)
            for _node in _ast.walk(_tree):
                if (isinstance(_node, _ast.Call)
                        and isinstance(_node.func, _ast.Name)
                        and _node.func.id == 'register_provider'
                        and _node.args
                        and isinstance(_node.args[0], _ast.Dict)):
                    _d = {}
                    for _k, _v in zip(_node.args[0].keys, _node.args[0].values):
                        if isinstance(_k, _ast.Constant) and isinstance(_v, _ast.Constant):
                            _d[_k.value] = _v.value
                    _cat = _d.get('category', '')
                    _pid = _d.get('provider_id', '')
                    _aname = _d.get('app_name', app_name)
                    if _cat and _pid:
                        _PROVIDER_APP_INDEX[f'{_cat}.{_pid}'] = _aname
        except Exception:
            logger.debug('bootstrap_providers: AST scan %s hooks.py 跳过', app_name)


# ═══════════════════════════════════════════════════════════════
# 公共 API：健康缓存 + 事件日志访问（供 project_mgmt 使用）
# ═══════════════════════════════════════════════════════════════

def start_health_auto_refresh(interval: int = 30) -> None:
    """启动 provider 健康状态后台定时刷新。幂等——重复调用安全。"""
    _health_cache.start_auto_refresh(interval)


def get_provider_health(provider_key: str) -> str:
    """返回 provider 当前健康状态: OK / DEGRADED / UNKNOWN。"""
    return _health_cache.get_state(provider_key)


def get_event_cursor() -> int:
    """返回当前事件日志最新 cursor 值（供 project_mgmt 启动补漏）。"""
    return _event_journal.current_cursor


def get_events_since(cursor: int) -> list:
    """返回 cursor 之后的所有事件。"""
    return _event_journal.get_since(cursor)