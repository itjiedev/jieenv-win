from django.urls import path

from . import views

app_name = 'python_main'

urlpatterns = [
    path('', views.IndexView.as_view(), name='index'),
    path('dev-tools/', views.DevToolsView.as_view(), name='dev_tools'),
    path('dev-tools/pycharm/versions/', views.PycharmVersionsView.as_view(), name='pycharm_versions'),
    path('dev-tools/pycharm/versions/<str:main_version>/', views.PycharmVersionsView.as_view(), name='pycharm_versions_param'),
    path('dev-tools/pycharm/refresh/', views.PycharmVersionsRefreshView.as_view(), name='pycharm_versions_refresh'),
    path('dev-tools/pycharm/detail/', views.PycharmDetailView.as_view(), name='pycharm_detail'),
    path('dev-tools/pycharm/uninstall/', views.PycharmUninstallView.as_view(), name="pycharm_uninstall"),
    path('dev-tools/pycharm/PycharmInstallView/<str:version>/', views.PycharmInstallView.as_view(), name="pycharm_install"),
    
    path('dev-tools/api/pycharm/version/refresh/', views.api_fetch_pycharm_version, name='api_pycharm_version_refresh'),
    path('dev-tools/api/pycharm/detail/refresh/', views.api_pycharm_info_refresh, name='api_pycharm_detail_refresh'),
    path('dev-tools/api/pycharm/start/', views.api_start_pycharm, name='api_start_pycharm'),
    path('dev-tools/api/pycharm/uninstall/', views.api_pycharm_uninstall, name='api_pycharm_uninstall'),
    path('dev-tools/api/pycharm/install/', views.api_pycharm_install, name='api_pycharm_install'),
    path('dev-tools/api/pycharm/reset/', views.api_pycharm_reset, name='api_pycharm_reset'),

    # VSCode（Visual Studio Code）：无版本区分，取官方最新稳定版
    path('dev-tools/vscode/install/', views.VscodeInstallView.as_view(), name='vscode_install'),
    path('dev-tools/vscode/detail/', views.VscodeDetailView.as_view(), name='vscode_detail'),
    path('dev-tools/vscode/uninstall/', views.VscodeUninstallView.as_view(), name='vscode_uninstall'),
    path('dev-tools/api/vscode/detail/refresh/', views.api_vscode_info_refresh, name='api_vscode_detail_refresh'),
    path('dev-tools/api/vscode/latest/refresh/', views.api_fetch_vscode_latest, name='api_vscode_latest_refresh'),
    path('dev-tools/api/vscode/start/', views.api_start_vscode, name='api_start_vscode'),
    path('dev-tools/api/vscode/install/', views.api_vscode_install, name='api_vscode_install'),
    path('dev-tools/api/vscode/uninstall/', views.api_vscode_uninstall, name='api_vscode_uninstall'),
    path('dev-tools/api/vscode/reset/', views.api_vscode_reset, name='api_vscode_reset'),

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