"""
权限处理模块
基于 Django 原生权限系统，提供统一的权限检查和同步功能。
"""
import importlib
import logging

from django.apps import apps
from django.conf import settings

logger = logging.getLogger(__name__)


def has_perm(user, perm_code: str, obj=None) -> bool:
    """
    检查用户是否拥有指定权限。
    - 空 perm_code 表示无需权限，返回 True
    - 未启用权限系统（USING_AUTH=False）时，所有权限检查直接返回 True
    - 超级用户拥有所有权限
    """
    if not perm_code:
        return True
    if not getattr(settings, 'USING_AUTH', False):
        return True
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    return user.has_perm(perm_code, obj)


def filter_menus_by_permission(menu_tree: list, user) -> list:
    """根据用户权限过滤菜单树"""
    result = []
    for node in menu_tree:
        perm = node.get('perm_code', '')
        if has_perm(user, perm):
            children = node.get('children', [])
            if children:
                filtered_children = filter_menus_by_permission(children, user)
                # 父菜单过滤后无子菜单则不显示
                if not filtered_children and not is_leaf_node(node):
                    continue
                node = dict(node)
                node['children'] = filtered_children
            result.append(node)
    return result


def is_leaf_node(node: dict) -> bool:
    """判断原始节点是否为叶子（无子菜单定义）"""
    return not node.get('children')


def sync_permissions():
    """
    同步所有已安装应用的自定义权限到数据库。
    仅在启用权限系统时生效。
    """
    if not getattr(settings, 'USING_AUTH', False):
        return
    try:
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        for app_config in apps.get_app_configs():
            name = app_config.name
            if not (name == 'jiecore' or name.startswith('apps.')):
                continue
            try:
                model_module = importlib.import_module(f'{name}.models')
                for attr_name in dir(model_module):
                    obj = getattr(model_module, attr_name)
                    if (hasattr(obj, '_meta')
                            and hasattr(obj.Meta, 'permissions')
                            and hasattr(obj, '__module__')):
                        ct = ContentType.objects.get_for_model(obj)
                        for codename, name_str in obj.Meta.permissions:
                            Permission.objects.get_or_create(
                                codename=codename,
                                content_type=ct,
                                defaults={'name': name_str}
                            )
            except (ImportError, AttributeError):
                continue
    except Exception as e:
        logger.warning('同步权限失败(可能尚未迁移): %s', e)
