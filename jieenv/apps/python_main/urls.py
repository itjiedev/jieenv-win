from django.urls import path

from . import views

app_name = 'python_main'

urlpatterns = [
    path('', views.IndexView.as_view(), name='index'),
    path('pypi/list/', views.PypiListView.as_view(), name='pypi_list'),
    path('pypi/api/get/', views.api_pypi_get, name='api_pypi_get'),
    path('pypi/api/set/', views.api_pypi_set, name='api_pypi_set'),
    path('pypi/api/reset/', views.api_pypi_reset, name='api_pypi_reset'),
    path('pypi/api/add_custom/', views.api_pypi_add_custom, name='api_pypi_add_custom'),
    path('pypi/api/remove_custom/', views.api_pypi_remove_custom, name='api_pypi_remove_custom'),

    # 通用 pip 包管理 API（宿主提供，跨环境复用）：env_id 由各环境提供者
    # 通过 python_main.pip_envs 扩展点解析为解释器路径（如 python_installer 的安装记录 id）。
    path('pip/api/list/<str:env_id>/', views.api_pip_list, name='api_pip_list'),
    path('pip/api/outdated/<str:env_id>/', views.api_pip_outdated, name='api_pip_outdated'),
    path('pip/api/install/<str:env_id>/', views.api_pip_install, name='api_pip_install'),
    path('pip/api/upgrade/<str:env_id>/', views.api_pip_upgrade, name='api_pip_upgrade'),
    path('pip/api/uninstall/<str:env_id>/', views.api_pip_uninstall, name='api_pip_uninstall'),
    path('pip/api/requirements/parse/<str:env_id>/', views.api_pip_requirements_parse, name='api_pip_requirements_parse'),
    path('pip/api/requirements/install/<str:env_id>/', views.api_pip_requirements_install, name='api_pip_requirements_install'),
    path('pip/api/requirements/export/<str:env_id>/', views.api_pip_requirements_export, name='api_pip_requirements_export'),

    # 下载缓存清理（layout.html 顶部“清理下载缓存”按钮，全应用页面共用）
    path('api/clear-cache/', views.api_clear_cache, name='api_clear_cache'),
]