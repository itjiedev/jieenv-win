"""
跨应用安全引用工具
提供扩展应用之间安全互相引用的机制。
所有跨应用导入都通过此模块进行，确保依赖关系正确。
"""
import importlib
import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


def import_app_module(app_name: str, submodule: str = '') -> Any:
    """
    安全导入指定应用的模块。
    
    用法:
        models = import_app_module('myapp', 'models')
        views = import_app_module('myapp', 'views')
    
    参数:
        app_name: 应用包名（如 'myapp'）
        submodule: 子模块名（如 'models', 'views', 'utils'）
    
    返回: 导入的模块对象
    
    异常: ImportError, DependencyError
    """
    if app_name == 'jiecore':
        module_path = f'{app_name}.{submodule}' if submodule else app_name
    else:
        module_path = f'apps.{app_name}.{submodule}' if submodule else f'apps.{app_name}'

    try:
        return importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(f'无法导入应用 {app_name} 的模块 {submodule}: {e}')


def get_app_models(app_name: str):
    """获取指定应用的所有模型"""
    return import_app_module(app_name, 'models')


def get_app_views(app_name: str):
    """获取指定应用的视图模块"""
    return import_app_module(app_name, 'views')


def get_app_utils(app_name: str):
    """获取指定应用的工具模块"""
    return import_app_module(app_name, 'utils')


def check_app_available(app_name: str) -> bool:
    """检查应用是否可用（已安装且已启用）"""
    from .app_manager import AppManager
    info = AppManager.get_app(app_name)
    return bool(info and info.get('is_enabled'))


def safe_import(app_name: str, submodule: str = '') -> Any:
    """
    安全导入，先检查应用是否可用再导入。
    如果应用不可用，返回 None 并记录警告。
    """
    if not check_app_available(app_name):
        logger.warning('应用 %s 不可用，跳过导入', app_name)
        return None
    try:
        return import_app_module(app_name, submodule)
    except ImportError as e:
        logger.warning('导入应用 %s 失败: %s', app_name, e)
        return None


def get_app_url(app_name: str, viewname: str, *args, **kwargs) -> str:
    """
    安全获取另一个应用的 URL。
    使用 Django 的 reverse 解析。
    """
    from django.urls import reverse
    try:
        return reverse(viewname, args=args, kwargs=kwargs)
    except Exception as e:
        logger.warning('解析 URL %s 失败: %s', viewname, e)
        return ''
