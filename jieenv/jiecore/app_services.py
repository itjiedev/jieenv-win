"""
应用间服务注册与调用机制

设计目标：
- 应用之间不直接 import，避免循环依赖和耦合
- 应用通过 register_service() 暴露功能（函数）
- 其他应用通过 call_service() 调用已注册的服务
- 支持服务发现（list_services）和参数校验

使用示例：

# 应用 A 暴露服务（在 apps.py 的 ready() 或 services.py 中注册）
from jiecore.app_services import register_service

def get_user_info(user_id):
    '''根据 ID 获取用户信息'''
    # ... 实现逻辑
    return {'id': user_id, 'name': '张三'}

register_service('myapp', 'get_user_info', get_user_info)

# 应用 B 调用服务
from jiecore.app_services import call_service

result = call_service('myapp', 'get_user_info', user_id=123)
# result = {'id': 123, 'name': '张三'}
"""
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# 全局服务注册表：{app_name: {service_name: callable}}
_SERVICE_REGISTRY: dict[str, dict[str, Callable]] = {}


class ServiceError(Exception):
    """服务调用异常"""
    pass


class ServiceNotFoundError(ServiceError):
    """服务未找到"""
    pass


class ServiceCallError(ServiceError):
    """服务调用失败"""
    pass


def register_service(app_name: str, service_name: str, handler: Callable,
                     description: str = '') -> None:
    """
    注册应用服务。

    参数:
        app_name: 应用名称（与 app_meta.py 中的 app_name 一致）
        service_name: 服务名称（应用内唯一）
        handler: 可调用的函数
        description: 服务描述（可选，用于文档和调试）

    注意:
        - 同一应用可以注册多个服务
        - 重复注册同名服务会覆盖旧服务（允许应用热重载时重新注册）
        - handler 应当是纯函数，避免副作用依赖调用方上下文

    示例:
        register_service('myapp', 'get_user', get_user_func, '获取用户信息')
    """
    if app_name not in _SERVICE_REGISTRY:
        _SERVICE_REGISTRY[app_name] = {}

    if service_name in _SERVICE_REGISTRY[app_name]:
        logger.debug('应用 %s 的服务 %s 被重新注册（覆盖）', app_name, service_name)

    _SERVICE_REGISTRY[app_name][service_name] = {
        'handler': handler,
        'description': description,
    }
    logger.debug('注册服务: %s.%s', app_name, service_name)


def unregister_service(app_name: str, service_name: str = None) -> None:
    """
    注销应用服务。

    参数:
        app_name: 应用名称
        service_name: 服务名称，为 None 时注销该应用的所有服务
    """
    if app_name not in _SERVICE_REGISTRY:
        return

    if service_name is None:
        # 注销所有服务
        del _SERVICE_REGISTRY[app_name]
        logger.debug('注销应用 %s 的所有服务', app_name)
    else:
        _SERVICE_REGISTRY[app_name].pop(service_name, None)
        if not _SERVICE_REGISTRY[app_name]:
            del _SERVICE_REGISTRY[app_name]
        logger.debug('注销服务: %s.%s', app_name, service_name)


def call_service(app_name: str, service_name: str, *args, **kwargs) -> Any:
    """
    调用其他应用暴露的服务。

    参数:
        app_name: 目标应用名称
        service_name: 服务名称
        *args, **kwargs: 传递给服务的参数

    返回:
        服务的返回值

    异常:
        ServiceNotFoundError: 服务未注册
        ServiceCallError: 服务调用失败（内部异常）

    示例:
        user = call_service('myapp', 'get_user', user_id=123)
    """
    if app_name not in _SERVICE_REGISTRY:
        raise ServiceNotFoundError(f'应用 {app_name} 未注册任何服务')

    services = _SERVICE_REGISTRY[app_name]
    if service_name not in services:
        available = ', '.join(services.keys())
        raise ServiceNotFoundError(
            f'应用 {app_name} 未注册服务 {service_name}，可用服务: {available or "(无)"}'
        )

    handler = services[service_name]['handler']
    try:
        return handler(*args, **kwargs)
    except Exception as e:
        logger.exception('调用服务 %s.%s 失败', app_name, service_name)
        raise ServiceCallError(
            f'调用服务 {app_name}.{service_name} 失败: {e}'
        ) from e


def has_service(app_name: str, service_name: str) -> bool:
    """
    检查服务是否已注册。

    参数:
        app_name: 应用名称
        service_name: 服务名称

    返回:
        bool: 服务是否存在
    """
    return (app_name in _SERVICE_REGISTRY and
            service_name in _SERVICE_REGISTRY[app_name])


def get_service_info(app_name: str, service_name: str = None) -> dict:
    """
    获取服务信息。

    参数:
        app_name: 应用名称
        service_name: 服务名称，为 None 时返回该应用所有服务信息

    返回:
        dict: 服务信息（handler, description）或服务列表
    """
    if app_name not in _SERVICE_REGISTRY:
        return {}

    if service_name is None:
        return {
            name: {'description': info['description']}
            for name, info in _SERVICE_REGISTRY[app_name].items()
        }

    service = _SERVICE_REGISTRY[app_name].get(service_name)
    if service:
        return {'description': service['description']}
    return {}


def list_services(app_name: str = None) -> dict:
    """
    列出已注册的服务。

    参数:
        app_name: 应用名称，为 None 时列出所有应用的所有服务

    返回:
        dict: {app_name: {service_name: description}}
    """
    if app_name:
        if app_name not in _SERVICE_REGISTRY:
            return {}
        return {
            app_name: {
                name: info['description']
                for name, info in _SERVICE_REGISTRY[app_name].items()
            }
        }

    return {
        app: {
            name: info['description']
            for name, info in services.items()
        }
        for app, services in _SERVICE_REGISTRY.items()
    }


def clear_app_services(app_name: str) -> None:
    """
    清除应用的所有服务注册（应用禁用/卸载时调用）。

    参数:
        app_name: 应用名称
    """
    if app_name in _SERVICE_REGISTRY:
        del _SERVICE_REGISTRY[app_name]
        logger.info('已清除应用 %s 的所有服务注册', app_name)


def reload_app_services(app_name: str) -> None:
    """
    重注册应用的 service 注册表（enable_app 时调用）。

    与 clear_app_services 对称：disable_app 时清掉注册表，
    enable_app 时需要把 ready() 里的 register_service 再执行一遍。
    由于 Django AppConfig.ready() 只在应用首次加载时跑一次，
    这里手动重置 ready 标记后调用。

    参数:
        app_name: 应用名称
    """
    import importlib
    import sys

    # 先清旧数据（干净启动，避免重复残留）
    clear_app_services(app_name)

    # 重置 Django 的 ready 标记，让 ready() 里的 register_service 重跑
    try:
        from django.apps import apps as django_apps
        app_config = django_apps.get_app_config(app_name)
    except LookupError:
        logger.warning('应用 %s 未注册到 Django apps，无法重加载 service', app_name)
        return

    try:
        # 重载 apps.py 模块，确保 handler 函数是最新版本
        apps_module_key = f'apps.{app_name}.apps'
        if apps_module_key in sys.modules:
            importlib.reload(sys.modules[apps_module_key])
        app_config._ready = False
        app_config.ready()
        logger.info('已重注册应用 %s 的 service', app_name)
    except Exception as e:
        logger.exception('重注册应用 %s 的 service 失败: %s', app_name, e)