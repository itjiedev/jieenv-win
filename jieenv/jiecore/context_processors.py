from django.conf import settings
from .menu_aggregator import get_menu_tree, mark_active_menu


def global_site(request):
    """全局上下文处理器，注入站点标题、皮肤、平台版本和菜单数据"""
    context = {
        'site_title': getattr(settings, 'SITE_TITLE', 'JieCore'),
        'platform_version': getattr(settings, 'PLATFORM_VERSION', ''),
    }

    # 构建菜单
    user = getattr(request, 'user', None)
    menu_tree = get_menu_tree(user)

    # 从 request 读取视图设置的菜单ID，标记激活菜单
    # 视图 Mixin 在 dispatch 中将 menu_id 存到 request 上
    if menu_tree:
        current_menu_id = getattr(request, 'current_menu_id', '')
        parent_menu_id = getattr(request, 'parent_menu_id', '')
        mark_active_menu(menu_tree, current_menu_id, parent_menu_id)

    context['site_menu'] = menu_tree
    return context

def global_site_skin(request):
    return {'site_skin': getattr(settings, 'SITE_SKIN', {}),}