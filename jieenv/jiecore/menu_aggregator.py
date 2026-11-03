"""
菜单聚合器
从各应用的 app_meta.py 收集菜单配置，构建多级菜单树。
聚合时根据 path 配置预生成链接，运行时直接读取缓存/数据库。
菜单数据同步写入 AppMenuItem 表，缓存写入 .cache/menus.json
"""
import importlib
import re
import sys
import logging
from collections import defaultdict
from urllib.parse import urlencode

from django.conf import settings

from . import cache_handler as cache

logger = logging.getLogger(__name__)


def load_app_metas() -> list:
    """
    加载所有已启用应用的菜单配置。
    返回扁平的菜单项列表，每项附带 _app 标记来源。
    """
    from .app_manager import AppManager
    enabled = AppManager.get_enabled_apps()
    items = []

    # 始终加载 jiecore 的菜单
    for core_app in ('jiecore',):
        meta = read_meta(core_app)
        if meta:
            collect_menus(meta, core_app, items)

    # 加载扩展应用
    for app_name in enabled:
        if app_name == 'jiecore':
            continue
        meta = read_meta(app_name)
        if meta:
            collect_menus(meta, app_name, items)

    return items


def read_meta(app_name: str) -> dict:
    """读取应用的 app_meta.py 中的 meta 字典"""
    if app_name == 'jiecore':
        module_path = f'{app_name}.app_meta'
    else:
        module_path = f'apps.{app_name}.app_meta'
    try:
        if module_path in sys.modules:
            del sys.modules[module_path]
        mod = importlib.import_module(module_path)
        if hasattr(mod, 'meta') and isinstance(mod.meta, dict):
            return mod.meta
    except (ImportError, AttributeError) as e:
        logger.debug('读取 %s 菜单失败: %s', app_name, e)
    return {}


def collect_menus(meta: dict, app_name: str, items: list):
    """从 meta 中提取菜单项并加入列表"""
    menus = meta.get('menus', [])
    if isinstance(menus, dict):
        menus = [menus]
    for item in menus:
        if isinstance(item, dict) and 'menu_id' in item:
            entry = dict(item)
            entry['_app'] = app_name
            items.append(entry)


def resolve_menu_link(path_config: dict, app_name: str = '') -> str:
    """
    根据 path 配置生成菜单链接。
    path 为空或 viewname 为空时返回 '#'。
    支持 query 参数和 fragment 锚点。
    reverse 失败时按应用挂载约定回退构建 URL。
    """
    if not path_config:
        return '#'
    viewname = path_config.get('viewname', '')
    if not viewname:
        return '#'

    from django.urls import reverse

    urlconf = path_config.get('urlconf') or None
    args = path_config.get('args') or []
    kwargs = path_config.get('kwargs') or {}
    current_app = path_config.get('current_app') or None
    query = path_config.get('query') or None
    fragment = path_config.get('fragment') or None

    try:
        url = reverse(viewname, args=args, kwargs=kwargs,
                      urlconf=urlconf, current_app=current_app, query=query, fragment=fragment)
        logger.debug('生成菜单链接 [%s]: %s', viewname, url)
    except Exception as e:
        logger.warning('reverse 失败 [%s] (%s: %s)，尝试 fallback', viewname, type(e).__name__, e)
        # reverse 失败（如应用刚启用、URL 尚未注册），按约定回退构建
        url = fallback_menu_url(viewname, app_name, args, kwargs)
        if not url:
            logger.warning('解析菜单 URL 失败 [%s]，已使用回退链接', viewname)
            return '#'
        logger.info('fallback 成功 [%s]: %s', viewname, url)

    return url


def fallback_menu_url(viewname: str, app_name: str, args: list, kwargs: dict) -> str:
    """
    reverse 失败时的回退 URL 构建。
    优先从应用 urls.py 中查找匹配的 URL pattern 路径；
    查找失败则按项目挂载约定 /{app_name}/ 构建基础路径。
    支持 url_prefix：若应用声明了 url_prefix，则路径前追加该前缀。
    """
    # 从 viewname 提取应用名和 URL name
    if ':' in viewname:
        ns, url_name = viewname.rsplit(':', 1)
    else:
        ns, url_name = '', viewname

    # 尝试从应用 urls.py 中直接查找 pattern 路径
    target_app = ns or app_name
    if target_app:
        try:
            urls_mod = importlib.import_module(f'apps.{target_app}.urls')
            for pattern in getattr(urls_mod, 'urlpatterns', []):
                if hasattr(pattern, 'name') and pattern.name == url_name:
                    url_pattern = str(pattern.pattern)
                    # 替换路径参数：先处理 kwargs，再处理 args
                    for k, v in kwargs.items():
                        url_pattern = re.sub(rf'<(?:\w+:)?{k}>', str(v), url_pattern)
                    if args:
                        for arg in args:
                            url_pattern = re.sub(r'<(?:\w+:)?\w+>', str(arg), url_pattern, count=1)
                    # 查找应用的 url_prefix
                    prefix = _get_app_url_prefix(target_app)
                    if prefix:
                        result = f'/{prefix}/{target_app}/{url_pattern}'
                    else:
                        result = f'/{target_app}/{url_pattern}'
                    logger.debug('fallback 构建 [%s]: pattern=%s -> %s', viewname, str(pattern.pattern), result)
                    return result
        except (ImportError, AttributeError) as e:
            logger.debug('fallback 导入 urls 失败 [%s]: %s', target_app, e)

    # 回退：按项目挂载约定 /{app_name}/
    if target_app:
        prefix = _get_app_url_prefix(target_app)
        if prefix:
            return f'/{prefix}/{target_app}/'
        return f'/{target_app}/'
    return ''


def _get_app_url_prefix(app_name: str) -> str:
    """从已启用应用缓存中获取应用的 url_prefix"""
    try:
        enabled = cache.load_enabled_apps()
        info = enabled.get(app_name, {})
        return info.get('url_prefix', '')
    except Exception:
        return ''


def build_menu_tree(flat_items: list) -> list:
    """
    将扁平菜单项列表构建为多级树结构。
    按 parent_id 分组，按 sort 排序。
    为每个节点预生成 link 字段。
    """
    menu_dict = {}
    children_map = defaultdict(list)

    for item in flat_items:
        mid = item['menu_id']
        menu_dict[mid] = item
        pid = item.get('parent_id', '')
        children_map[pid].append(mid)

    # 排序
    for pid in children_map:
        children_map[pid].sort(key=lambda x: menu_dict[x].get('sort', 0))

    def _has_valid_link(item):
        """判断菜单项是否有有效的链接配置"""
        path_config = item.get('path', {})
        if not path_config:
            return False
        return bool(path_config.get('viewname', ''))

    def _build(parent_id=''):
        tree = []
        for mid in children_map.get(parent_id, []):
            src = menu_dict[mid]
            node = {k: v for k, v in src.items() if not k.startswith('_')}
            # 预生成链接
            path_config = node.get('path', {})
            node['link'] = resolve_menu_link(path_config, src.get('_app', ''))
            node['children'] = _build(mid)
            # 过滤：path/viewname 为空且无子菜单的项不加入树中
            if not node['children'] and not _has_valid_link(src):
                continue
            tree.append(node)
        return tree

    return _build('')


def filter_menu_by_permission(menu_tree: list, user) -> list:
    """
    根据用户权限过滤菜单树。
    - 未启用权限系统（USING_AUTH=False）时，所有菜单直接返回
    - 超级用户看到所有菜单
    - 未配置 perm_code 的菜单对所有用户可见
    - 配置了 perm_code 的菜单需要用户拥有对应权限
    """
    if not getattr(settings, 'USING_AUTH', False):
        return menu_tree
    if user and getattr(user, 'is_authenticated', False) and getattr(user, 'is_superuser', False):
        return menu_tree

    filtered = []
    for node in menu_tree:
        perm = node.get('perm_code', '')
        # 无权限要求或有权限则显示
        if not perm or (user and getattr(user, 'is_authenticated', False) and user.has_perm(perm)):
            children = filter_menu_by_permission(node.get('children', []), user)
            node_copy = dict(node)
            node_copy['children'] = children
            # 如果是父菜单且过滤后没有子菜单，则不显示
            if node_copy.get('children') is not None and not node_copy['children'] and not is_leaf(node):
                continue
            filtered.append(node_copy)
    return filtered


def is_leaf(node: dict) -> bool:
    """判断节点是否为叶子节点（没有 children 或 children 为空列表表示原本就没有子菜单）"""
    return 'children' not in node or (isinstance(node.get('children'), list) and len(node.get('children', [])) == 0)


def refresh_menu_cache():
    """
    刷新菜单缓存。
    聚合所有已启用应用的菜单，构建树结构，写入缓存/数据库。
    同时全量扫描加载各应用的扩展钩子。
    """
    # 全量扫描加载扩展钩子（已加载过的应用自动跳过）
    from .ext_hook_registry import scan_all_ext_hooks
    scan_all_ext_hooks()

    # 启动时扫描注册 provider_catalog provider（Phase 0 v2.0）
    from .provider_catalog import bootstrap_providers
    bootstrap_providers()

    flat_items = load_app_metas()
    menu_tree = build_menu_tree(flat_items)

    # 同步写入 AppMenuItem 表
    try:
        from .models import AppMenuItem
        AppMenuItem.objects.all().delete()
        for node in menu_tree:
            save_menu_node(node, parent_id='')
    except Exception as e:
        logger.warning('写入菜单数据库失败(可能尚未迁移): %s', e)

    # 写入 JSON 缓存
    cache.save_menu_data(menu_tree)


def save_menu_node(node: dict, parent_id: str):
    """递归保存菜单节点到数据库"""
    from .models import AppMenuItem
    obj = AppMenuItem.objects.create(
        menu_id=node['menu_id'],
        parent_id=parent_id,
        app_name=node.get('_app', ''),
        name=node.get('name', ''),
        icon=node.get('icon', ''),
        sort=node.get('sort', 0),
        is_show=node.get('is_show', True),
        perm_code=node.get('perm_code', ''),
        path_config=node.get('path', {}),
        link=node.get('link', '#'),
    )
    for child in node.get('children', []):
        save_menu_node(child, parent_id=node['menu_id'])


def get_menu_tree(user=None) -> list:
    """
    获取菜单树（优先从缓存读取）。
    如果传入 user，则进行权限过滤。
    """
    menu_data = cache.load_menu_data()
    if not menu_data:
        # 缓存为空则刷新
        refresh_menu_cache()
        menu_data = cache.load_menu_data()

    if user:
        menu_data = filter_menu_by_permission(menu_data, user)

    return menu_data



def mark_active_menu(menu_tree: list, current_menu_id: str, parent_menu_id: str = '') -> list:
    """
    为菜单树标记高亮状态。
    current_menu_id: 当前页面的菜单ID
    parent_menu_id: 当前页面的父菜单ID
    返回标记了 _active 的菜单树。
    """
    for node in menu_tree:
        node['active'] = (node['menu_id'] == current_menu_id)
        node['parent_active'] = (node['menu_id'] == parent_menu_id)
        children = node.get('children', [])
        if children:
            mark_active_menu(children, current_menu_id, parent_menu_id)
            # 如果子菜单中有激活项，父菜单也标记为展开
            if any(has_active(c) for c in children):
                node['parent_active'] = True
    return menu_tree


def has_active(node: dict) -> bool:
    """递归检查节点或其子节点是否有激活状态"""
    if node.get('active') or node.get('parent_active'):
        return True
    for child in node.get('children', []):
        if has_active(child):
            return True
    return False