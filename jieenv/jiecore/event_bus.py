"""
event_bus — jieEnv 事件总线
极简发布/订阅机制，供 provider_catalog / app_manager / 各应用之间解耦通信。

设计目标：
- 回调 try/except 全包，单个订阅者异常不阻塞后续订阅者
- 线程安全（RLock 保护订阅表）
- 快照订阅者列表：emit 期间新订阅的不会收到本次事件
"""

import threading
import traceback
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════
# 框架自动发射的事件名常量
# ═══════════════════════════════════════════════════════════════

EVENT_PROVIDER_REGISTERED     = 'provider.registered'
EVENT_PROVIDER_UNREGISTERED   = 'provider.unregistered'
EVENT_PROVIDER_HEALTH_CHANGED = 'provider.health_changed'
EVENT_CATEGORY_REGISTERED     = 'category.registered'
EVENT_APP_ENABLED             = 'app.enabled'
EVENT_APP_DISABLED            = 'app.disabled'
EVENT_APP_UNINSTALLED         = 'app.uninstalled'


class _EventBus:
    """线程安全的发布/订阅中心。全局单例，通过模块级便捷函数暴露。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._subscribers: defaultdict[str, list] = defaultdict(list)

    # ── 公共 API ────────────────────────────────────────

    def subscribe(self, event_name: str, callback) -> None:
        if not callable(callback):
            raise TypeError(
                f"subscribe callback must be callable, got {type(callback).__name__}")
        with self._lock:
            self._subscribers[event_name].append(callback)

    def unsubscribe(self, event_name: str, callback) -> None:
        with self._lock:
            subs = self._subscribers.get(event_name)
            if callback in subs:
                subs.remove(callback)

    def emit(self, event_name: str, payload: dict = None) -> None:
        # 快照：锁内拷贝，锁外调用，避免回调阻塞后续 emit 或 subscribe
        with self._lock:
            subs = list(self._subscribers.get(event_name, []))

        for cb in subs:
            try:
                cb(payload or {})
            except Exception:
                traceback.print_exc()

    # ── 内部工具（测试/调试用） ─────────────────────────

    def _clear_all(self) -> None:
        with self._lock:
            self._subscribers.clear()

    def _subscriber_count(self, event_name: str = None) -> int:
        with self._lock:
            if event_name:
                return len(self._subscribers.get(event_name, []))
            return sum(len(s) for s in self._subscribers.values())


# ── 全局单例 ────────────────────────────────────────────

_bus = _EventBus()


# ── 模块级便捷函数 ──────────────────────────────────────

def subscribe(event_name: str, callback) -> None:
    return _bus.subscribe(event_name, callback)


def emit(event_name: str, payload: dict = None) -> None:
    return _bus.emit(event_name, payload)


def unsubscribe(event_name: str, callback) -> None:
    return _bus.unsubscribe(event_name, callback)