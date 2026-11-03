"""
扩展钩子注册表

设计目标：
- 宿主应用声明命名扩展点，其他应用可注册处理器实现扩展
- 一个扩展点可被多个应用同时挂载（1:N 关系）
- 宿主应用调用扩展点时，按 sort 排序依次执行所有处理器，收集返回值
- 处理器返回的数据格式由宿主应用与贡献者约定，框架不做强制约束

与 app_services 的区别：
- app_services：1:1 服务调用（应用 A 暴露 → 应用 B 调用）
- ext_hook_registry：1:N 扩展点（宿主声明 → 多个应用挂载 → 宿主收集）

约定文件：
- 各应用可在根目录提供 ext_hooks.py，定义 register() 函数
- register() 中调用 register_hook() 将处理器注册到目标扩展点
- 框架在应用启用时自动加载 ext_hooks.py，禁用/卸载时自动清理

使用示例：

# ── 宿主应用（如 dbmysql）声明扩展点并调用 ──
# views.py
from jiecore.ext_hook_registry import call_hooks

def instance_detail_view(request):
    ext_actions = call_hooks('dbmysql.instance_detail.actions', context={
        'instance_id': instance['id'],
        'instance': instance,
    })
    return render(request, 'detail.html', {'ext_actions': ext_actions})

# ── 贡献者应用（如 sqlyze）注册处理器 ──
# ext_hooks.py
from jiecore.ext_hook_registry import register_hook

def _add_sql_analyze(context):
    return {
        'label': 'SQL 分析',
        'url': f"/sqlyze/analyze/?instance={context['instance_id']}",
        'icon': 'fa-search',
    }

def register():
    register_hook(
        'dbmysql.instance_detail.actions',
        _add_sql_analyze,
        app_name='sqlyze',
        description='在实例详情页添加 SQL 分析入口',
        sort=10,
    )
"""
import importlib.util
import logging
from typing import Any, Callable, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# 全局注册表：{hook_name: [{'app_name': str, 'handler': callable,
#               'description': str, 'sort': int}, ...]}
_EXT_HOOK_REGISTRY: dict[str, list[dict]] = {}

# 已加载 ext_hooks.py 的应用集合（防止重复加载）
_LOADED_APPS: set[str] = set()

# 排序缓存：{hook_name: [已排序的 entries 列表]}
_SORTED_CACHE: dict[str, list[dict]] = {}

# 需要重新排序的扩展点集合（脏标记）
_DIRTY_HOOKS: set[str] = set()


# ── 注册 / 注销 ─────────────────────────────────────────────

def register_hook(hook_name: str, handler: Callable, app_name: str,
                  description: str = '', sort: int = 0) -> None:
    """
    注册一个扩展点处理器。

    参数:
        hook_name: 扩展点名称（约定格式: {app_name}.{page}.{position}）
        handler: 处理器函数，接收 context 字典参数，返回值由宿主应用约定
        app_name: 贡献者应用名称
        description: 处理器描述（可选，用于调试和文档）
        sort: 排序权重（越小越靠前，默认 0）

    注意:
        - 同一应用对同一扩展点可注册多个不同处理器
        - 重复注册相同的 handler 函数会覆盖（允许热重载时重新注册）
    """
    if hook_name not in _EXT_HOOK_REGISTRY:
        _EXT_HOOK_REGISTRY[hook_name] = []

    # 检查是否重复注册同一处理函数
    for entry in _EXT_HOOK_REGISTRY[hook_name]:
        if entry['handler'] is handler:
            entry['description'] = description
            entry['sort'] = sort
            _DIRTY_HOOKS.add(hook_name)
            logger.debug('扩展钩子 %s 的处理器 %s 被重新注册（覆盖）',
                         hook_name, app_name)
            return

    _EXT_HOOK_REGISTRY[hook_name].append({
        'app_name': app_name,
        'handler': handler,
        'description': description,
        'sort': sort,
    })
    _DIRTY_HOOKS.add(hook_name)
    logger.debug('注册扩展钩子: %s <- %s', hook_name, app_name)


def unregister_hooks(app_name: str) -> None:
    """
    注销指定应用的所有扩展点处理器。
    应用禁用/卸载/删除时由框架自动调用。

    参数:
        app_name: 应用名称
    """
    empty_hooks = []
    for hook_name, entries in _EXT_HOOK_REGISTRY.items():
        old_len = len(entries)
        _EXT_HOOK_REGISTRY[hook_name] = [
            e for e in entries if e['app_name'] != app_name
        ]
        if len(_EXT_HOOK_REGISTRY[hook_name]) != old_len:
            _DIRTY_HOOKS.add(hook_name)
        if not _EXT_HOOK_REGISTRY[hook_name]:
            empty_hooks.append(hook_name)

    # 清理空的扩展点
    for hook_name in empty_hooks:
        del _EXT_HOOK_REGISTRY[hook_name]
        _SORTED_CACHE.pop(hook_name, None)
        _DIRTY_HOOKS.discard(hook_name)

    # 从已加载集合中移除
    _LOADED_APPS.discard(app_name)
    logger.info('已清除应用 %s 的所有扩展钩子', app_name)


# ── 调用 / 查询 ─────────────────────────────────────────────

def _get_sorted_entries(hook_name: str) -> list[dict]:
    """
    获取指定扩展点的已排序处理器列表（带缓存）。
    仅在注册表变更（register/unregister）后重新排序，避免每次调用都排序。
    """
    if hook_name in _DIRTY_HOOKS or hook_name not in _SORTED_CACHE:
        _SORTED_CACHE[hook_name] = sorted(
            _EXT_HOOK_REGISTRY[hook_name], key=lambda e: e['sort']
        )
        _DIRTY_HOOKS.discard(hook_name)
    return _SORTED_CACHE[hook_name]


def call_hooks(hook_name: str, context: dict = None) -> list[Any]:
    """
    调用指定扩展点的所有处理器，按 sort 排序依次执行。

    参数:
        hook_name: 扩展点名称
        context: 传递给处理器的上下文字典

    返回:
        list: 各处理器的返回值列表（按 sort 排序），处理器异常时跳过并记录日志
    """
    if hook_name not in _EXT_HOOK_REGISTRY:
        return []

    if context is None:
        context = {}

    entries = _get_sorted_entries(hook_name)
    results = []

    for entry in entries:
        try:
            result = entry['handler'](context)
            if result is not None:
                results.append(result)
        except Exception as e:
            logger.exception('扩展钩子 %s 的处理器 %s 执行失败: %s',
                             hook_name, entry['app_name'], e)

    return results


def get_hook_handlers(hook_name: str) -> list[dict]:
    """
    获取指定扩展点的所有处理器信息（按 sort 排序）。

    参数:
        hook_name: 扩展点名称

    返回:
        list[dict]: 处理器信息列表，每项包含 app_name, description, sort
    """
    if hook_name not in _EXT_HOOK_REGISTRY:
        return []

    entries = _get_sorted_entries(hook_name)
    return [
        {
            'app_name': e['app_name'],
            'description': e['description'],
            'sort': e['sort'],
        }
        for e in entries
    ]


def has_hooks(hook_name: str) -> bool:
    """检查指定扩展点是否有处理器注册。"""
    return bool(_EXT_HOOK_REGISTRY.get(hook_name))


def list_hooks() -> dict[str, list[dict]]:
    """
    列出所有扩展点及其处理器。

    返回:
        dict: {hook_name: [{'app_name', 'description', 'sort'}, ...]}
    """
    result = {}
    for hook_name in _EXT_HOOK_REGISTRY:
        entries = _get_sorted_entries(hook_name)
        result[hook_name] = [
            {'app_name': e['app_name'],
             'description': e['description'],
             'sort': e['sort']}
            for e in entries
        ]
    return result


# ── 自动加载 ─────────────────────────────────────────────────

def load_app_ext_hooks(app_name: str) -> bool:
    """
    加载指定应用的 ext_hooks.py 并调用其 register() 函数。
    已加载过的应用不会重复加载。

    参数:
        app_name: 应用名称

    返回:
        bool: 是否成功加载（无 ext_hooks.py 也算成功）
    """
    if app_name in _LOADED_APPS:
        return True

    # 确定 ext_hooks.py 路径
    if app_name == 'jiecore':
        hooks_path = settings.BASE_DIR / app_name / 'ext_hooks.py'
    else:
        hooks_path = settings.APPS_DIR / app_name / 'ext_hooks.py'

    if not hooks_path.exists():
        _LOADED_APPS.add(app_name)
        return True

    # 动态加载模块
    module_name = f'_ext_hooks_{app_name}'
    try:
        spec = importlib.util.spec_from_file_location(module_name, hooks_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # 调用 register() 函数
            register_func = getattr(mod, 'register', None)
            if callable(register_func):
                register_func()
                logger.info('已加载应用 %s 的扩展钩子', app_name)
            else:
                logger.debug('应用 %s 的 ext_hooks.py 无 register() 函数', app_name)
    except Exception as e:
        logger.warning('加载应用 %s 的 ext_hooks.py 失败: %s', app_name, e)
        return False

    _LOADED_APPS.add(app_name)
    return True


def scan_all_ext_hooks() -> None:
    """
    扫描所有已启用应用，加载各自的 ext_hooks.py。
    在 refresh_menu_cache() 或应用启用时调用。
    """
    from .app_manager import AppManager
    enabled = AppManager.get_enabled_apps()

    # 始终扫描 jiecore
    load_app_ext_hooks('jiecore')

    for app_name in enabled:
        if app_name == 'jiecore':
            continue
        load_app_ext_hooks(app_name)


def reset_loaded() -> None:
    """
    重置已加载集合（仅用于测试或全量重新加载场景）。
    """
    _LOADED_APPS.clear()
    _EXT_HOOK_REGISTRY.clear()
    _SORTED_CACHE.clear()
    _DIRTY_HOOKS.clear()
