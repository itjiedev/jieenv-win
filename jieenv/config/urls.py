import json
import logging
from pathlib import Path

# from django.contrib import admin
from django.urls import path, re_path, include
from django.conf import settings

logger = logging.getLogger(__name__)


def _load_enabled_apps():
    """从缓存文件加载已启用的应用列表。
    缓存不可读时返回空字典，此时不会有扩展应用路由。"""
    cache_file = settings.ENABLED_APPS_CACHE
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            logger.warning("_load_enabled_apps: 缓存文件内容格式异常: %s", cache_file)
        except (json.JSONDecodeError, IOError):
            logger.warning("_load_enabled_apps: Failed to load enabled apps cache file: %s", cache_file)
    return {}


from jiecore import views as jiecore_views

# 先构建已启用应用的路由（应用路由优先于 root_view，Django 先匹配先生效）
_app_urlpatterns = []
_enabled = _load_enabled_apps()
for _app_name, _info in _enabled.items():
    _url_module = f'apps.{_app_name}.urls'
    try:
        if _info.get('is_root'):
            _app_urlpatterns.append(path('', include(_url_module)))
        else:
            _prefix = _info.get('url_prefix', '')
            if _prefix:
                _app_urlpatterns.append(path(f'{_prefix}/{_app_name}/', include(_url_module)))
            else:
                _app_urlpatterns.append(path(f'{_app_name}/', include(_url_module)))
    except Exception as e:
        logger.warning(f"Failed to load app urls: {_url_module} 报错信息： {e}")

urlpatterns = _app_urlpatterns + [
    path('', jiecore_views.root_view),
    path('jiecore/', include('jiecore.urls')),
]
if settings.USING_AUTH:
    from django.contrib import admin
    urlpatterns.append(path('admin/', admin.site.urls))

# 服务器启动时全量扫描加载各应用的扩展钩子（ext_hooks 为内存注册表，
# 重启后清空，需在路由建立后重新加载）
# 数据库未初始化时（如首次 jieinit）跳过，迁移完成后再启动即可正常加载
from jiecore.ext_hook_registry import scan_all_ext_hooks
try:
    scan_all_ext_hooks()
except Exception as e:
    logger.warning('扩展钩子扫描跳过（数据库可能未初始化）: %s', e)

# 非 DEBUG（生产/waitress/桌面壳）时 Django 不再自动提供静态文件，
# 用 staticfiles 的 finder 视图实时提供：直接检索各应用 static/ 目录，
# 无需 collectstatic，新安装应用的静态文件也自动生效
from django.contrib.staticfiles.views import serve as _static_serve
urlpatterns.append(
    re_path(r'^static/(?P<path>.*)$', _static_serve, {'insecure': True})
)
