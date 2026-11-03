"""
运行时热加载模块
在应用安装、启用、禁用、卸载、删除、升级后，尽量免重启让变更立即生效。

处理内容：
- 动态注册应用到 Django app registry（模型、模板目录可用）
- 重建根 URLconf 并清除路由解析缓存（新路由立即可访问）
- 重置模板引擎缓存（新应用的 templates 目录生效）
- 清理已升级/已移除应用的旧模块缓存

若热加载失败，调用方应将 restart_required 置为 True，由前端触发重启。
"""
import sys
import importlib
import logging

from django.conf import settings
from django.urls import clear_url_caches

logger = logging.getLogger(__name__)


def purge_app_modules(app_name: str):
    """从 sys.modules 清除指定应用的全部已导入模块（升级/移除后强制重新导入）
    支持扩展应用(apps.xxx)和核心应用(jiecore)。
    """
    if app_name == 'jiecore':
        prefix = app_name
    else:
        prefix = f'apps.{app_name}'
    stale = [m for m in list(sys.modules) if m == prefix or m.startswith(prefix + '.')]
    for m in stale:
        del sys.modules[m]
    if stale:
        logger.debug('已清理应用 %s 的 %d 个旧模块', app_name, len(stale))


def sync_template_dirs():
    """
    将已启用应用的 templates 目录显式加入 TEMPLATES['DIRS']。
    兼容保障：APP_DIRS 依赖引擎创建时扫描 app_configs，动态注册场景下
    可能遗漏；DIRS 优先级更高且不依赖注册状态，确保模板始终可被发现。
    仅追加不删除：目录不存在时加载器自动跳过，无副作用。
    """
    from . import cache_handler as cache
    dirs = settings.TEMPLATES[0].setdefault('DIRS', [])
    existing = {str(d) for d in dirs}
    for app_name in cache.load_enabled_apps():
        tpl_dir = settings.APPS_DIR / app_name / 'templates'
        if tpl_dir.is_dir() and str(tpl_dir) not in existing:
            dirs.append(tpl_dir)
            existing.add(str(tpl_dir))


def reset_template_engines():
    """重置模板引擎缓存，使新的模板目录配置生效"""
    try:
        from django.template import engines as template_engines
        # 清除 TEMPLATES 配置缓存（cached_property），下次访问重新读取 settings
        try:
            del template_engines.templates
        except AttributeError:
            pass
        # Django 各版本内部引擎缓存属性名不同，逐个尝试清理
        for attr in ('engines', '_engines'):
            cache = getattr(template_engines, attr, None)
            if isinstance(cache, dict):
                cache.clear()
                return
    except Exception as e:
        logger.warning('重置模板引擎缓存失败(可忽略): %s', e)


def rebuild_urlconf() -> bool:
    """
    重新加载根 URLconf 模块并清除 URL 解析缓存。
    config.urls 模块顶层会根据最新的已启用应用缓存重建 urlpatterns，
    因此重载后新启用/禁用/升级的应用路由立即生效。
    """
    root = settings.ROOT_URLCONF
    try:
        if root in sys.modules:
            importlib.reload(sys.modules[root])
        else:
            importlib.import_module(root)
        clear_url_caches()
        sync_template_dirs()
        reset_template_engines()
        return True
    except Exception:
        logger.exception('热加载路由失败，需要重启服务')
        return False


def activate_app(app_name: str) -> bool:
    """
    安装/启用应用后调用：注册到 Django app registry 并重建路由。
    返回是否全部成功（False 表示需要重启服务）。
    """
    from .app_manager import ensure_app_registered
    registered = ensure_app_registered(app_name)
    if not registered:
        logger.warning('应用 %s 未能注册到 Django app registry', app_name)
    url_ok = rebuild_urlconf()
    return registered and url_ok


def deactivate_app(app_name: str) -> bool:
    """
    禁用/卸载/删除应用后调用：清理旧模块并重建路由（移除其路由）。
    返回是否成功（False 表示需要重启服务）。
    """
    purge_app_modules(app_name)
    return rebuild_urlconf()
