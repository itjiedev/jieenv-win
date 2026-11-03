"""
应用生命周期钩子机制

设计目标：
- 应用可提供 hooks.py 文件定义生命周期钩子函数
- jiecore 在应用安装、启用、禁用、卸载、删除时自动调用对应钩子
- 钩子函数可选，未定义则跳过
- 钩子执行失败时记录日志但不阻塞主流程（除非显式抛出 HookAbortError）

钩子文件约定：
- 位置：应用根目录下的 hooks.py
- 函数签名：def hook_name(context: dict) -> None
- context 包含操作上下文信息（见各钩子说明）

支持的钩子：
- on_install(context): 安装完成后调用
- on_enable(context): 启用完成后调用
- on_disable(context): 禁用完成后调用
- on_uninstall(context): 卸载完成后调用
- on_delete(context): 删除完成后调用

使用示例：

# apps/myapp/hooks.py

def on_install(context):
    '''安装后初始化默认配置'''
    app_dir = context['app_dir']
    # ... 初始化逻辑

def on_enable(context):
    '''启用后执行数据迁移检查'''
    # ... 检查逻辑

def on_disable(context):
    '''禁用前清理缓存'''
    # ... 清理逻辑

def on_uninstall(context):
    '''卸载前备份自定义数据'''
    # ... 备份逻辑

def on_delete(context):
    '''删除前执行最终清理'''
    # ... 清理逻辑
"""
import importlib.util
import logging
from pathlib import Path
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# 支持的钩子名称
HOOK_NAMES = ('on_install', 'on_enable', 'on_disable', 'on_uninstall', 'on_delete')


class HookError(Exception):
    """钩子执行失败（默认不阻塞主流程）"""
    pass


class HookAbortError(HookError):
    """
    钩子主动中止操作（阻塞主流程）。
    应用可在钩子中抛出此异常来阻止后续操作。
    """
    pass


def get_hooks_module(app_name: str) -> Optional[object]:
    """
    加载应用的 hooks.py 模块。

    参数:
        app_name: 应用名称

    返回:
        hooks 模块对象，不存在则返回 None
    """
    # 确定应用目录
    from .app_manager import get_core_app_dir
    hooks_path = get_core_app_dir(app_name) / 'hooks.py'

    if not hooks_path.exists():
        return None

    # 动态加载模块
    module_name = f'_hooks_{app_name}'
    try:
        spec = importlib.util.spec_from_file_location(module_name, hooks_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    except Exception as e:
        logger.warning('加载应用 %s 的 hooks.py 失败: %s', app_name, e)

    return None


def has_hook(app_name: str, hook_name: str) -> bool:
    """
    检查应用是否定义了指定钩子。

    参数:
        app_name: 应用名称
        hook_name: 钩子名称

    返回:
        bool: 钩子是否存在且可调用
    """
    if hook_name not in HOOK_NAMES:
        return False

    mod = get_hooks_module(app_name)
    if mod is None:
        return False

    hook_func = getattr(mod, hook_name, None)
    return callable(hook_func)


def call_hook(app_name: str, hook_name: str, context: dict,
              abort_on_error: bool = False) -> dict:
    """
    调用应用的指定钩子。

    参数:
        app_name: 应用名称
        hook_name: 钩子名称（必须是 HOOK_NAMES 之一）
        context: 操作上下文，包含以下信息：
            - app_name: 应用名称
            - app_dir: 应用目录路径 (Path)
            - action: 操作名称 ('install'/'enable'/'disable'/'uninstall'/'delete')
            - version: 应用版本号（如可获取）
            - 其他操作特定信息
        abort_on_error: 钩子失败时是否抛出异常中止主流程（默认 False 仅记录日志）

    返回:
        dict: {'success': bool, 'message': str}
        - success=True: 钩子执行成功或钩子不存在
        - success=False: 钩子执行失败（仅当 abort_on_error=False 时返回）

    异常:
        HookAbortError: 钩子主动中止（无论 abort_on_error 设置）
        HookError: 钩子执行失败且 abort_on_error=True
    """
    if hook_name not in HOOK_NAMES:
        logger.warning('未知的钩子名称: %s', hook_name)
        return {'success': True, 'message': f'未知钩子 {hook_name}，已跳过'}

    mod = get_hooks_module(app_name)
    if mod is None:
        # 无 hooks.py 文件，正常情况
        return {'success': True, 'message': ''}

    hook_func = getattr(mod, hook_name, None)
    if not callable(hook_func):
        # hooks.py 存在但未定义该钩子，正常情况
        return {'success': True, 'message': ''}

    # 确保上下文包含应用名
    context.setdefault('app_name', app_name)

    logger.info('执行应用 %s 的钩子: %s', app_name, hook_name)

    try:
        hook_func(context)
        logger.info('应用 %s 的钩子 %s 执行成功', app_name, hook_name)
        return {'success': True, 'message': ''}

    except HookAbortError as e:
        # 钩子主动中止，必须抛出
        logger.error('应用 %s 的钩子 %s 主动中止操作: %s', app_name, hook_name, e)
        raise

    except Exception as e:
        logger.exception('应用 %s 的钩子 %s 执行失败', app_name, hook_name)
        if abort_on_error:
            raise HookError(f'钩子 {hook_name} 执行失败: {e}') from e
        return {'success': False, 'message': f'钩子 {hook_name} 执行失败: {e}'}


def build_context(app_name: str, action: str, **extra) -> dict:
    """
    构建钩子调用上下文。

    参数:
        app_name: 应用名称
        action: 操作名称
        **extra: 额外上下文信息

    返回:
        dict: 完整的操作上下文
    """
    # 确定应用目录
    from .app_manager import get_core_app_dir
    app_dir = get_core_app_dir(app_name)

    context = {
        'app_name': app_name,
        'app_dir': app_dir,
        'action': action,
    }

    # 尝试获取版本号
    try:
        from .app_manager import read_app_meta_from_disk
        meta = read_app_meta_from_disk(app_name)
        if meta:
            context['version'] = meta.get('version', '')
    except Exception:
        pass

    context.update(extra)
    return context