from django.urls import path

from . import views

app_name = 'python_installer'

urlpatterns = [
    # Tab 内容页（AJAX 加载）
    path('index/', views.ContentView.as_view(), name='index'),

    # 官网版本列表页（独立页面）
    path('versions/', views.VersionListView.as_view(), name='version_list'),

    # 安装选项页
    path('install/options/', views.InstallOptionsView.as_view(), name='install_options'),

    # 下载源设置页
    path('download-source/', views.DownloadSourceView.as_view(), name='download_source'),

    # 应用设置页
    path('settings/', views.SettingsView.as_view(), name='settings'),

    # pip 包管理页
    path('pip/list/<str:id>/', views.PipListView.as_view(), name='pip_list'),

    # pip 安装新包页（完整页面，非弹窗）
    path('pip/install/<str:id>/', views.PipInstallView.as_view(), name='pip_install'),

    # 虚拟环境页面
    path('venvs/<str:record_id>/', views.VenvListView.as_view(), name='venv_list'),
    path('venvs/pip/list/<str:venv_id>/', views.PipListVenvView.as_view(), name='pip_list_venv'),
    path('venvs/pip/install/<str:venv_id>/', views.PipInstallVenvView.as_view(), name='pip_install_venv'),

    # JSON API
    path('api/py/uninstall/', views.api_uninstall_py, name='api_py_uninstall'),
    path('api/versions/', views.api_versions, name='api_versions'),
    path('api/installed/', views.api_installed, name='api_installed'),
    path('api/del_record/', views.api_del_record, name='api_del_record'),
    path('api/import/', views.api_import_installed, name='api_import_installed'),
    path('api/install/start/', views.api_install_start, name='api_install_start'),
    path('api/uninstall/', views.api_uninstall, name='api_uninstall'),
    path('api/set-py-default/', views.api_set_py_default, name='api_set_py_default'),
    path('api/add-path/', views.api_add_path, name='api_add_path'),
    path('api/open-terminal/', views.api_open_terminal, name='api_open_terminal'),
    path('api/uninstall-gui/', views.api_uninstall_gui, name='api_uninstall_gui'),
    path('api/install-py-launcher/', views.api_install_py_launcher, name='api_install_py_launcher'),
    path('api/download-sources/', views.api_download_sources, name='api_download_sources'),
    path('api/set-download-source/', views.api_set_download_source, name='api_set_download_source'),
    path('api/settings/', views.api_settings, name='api_settings'),
    path('api/settings/save/', views.api_settings_save, name='api_settings_save'),

    # venv JSON API
    path('api/venvs/list/<str:record_id>/', views.api_venv_list, name='api_venv_list'),
    path('api/venvs/create/', views.api_venv_create, name='api_venv_create'),
    path('api/venvs/import/', views.api_venv_import, name='api_venv_import'),
    path('api/venvs/delete/', views.api_venv_delete, name='api_venv_delete'),
    path('api/venvs/rename/', views.api_venv_rename, name='api_venv_rename'),
    path('api/venvs/verify/<str:venv_id>/', views.api_venv_verify, name='api_venv_verify'),
    path('api/venvs/verify-l2/<str:venv_id>/', views.api_venv_verify_l2, name='api_venv_verify_l2'),
    path('api/venvs/sync/', views.api_venv_sync, name='api_venv_sync'),
    path('api/venvs/open-terminal/', views.api_venv_open_terminal, name='api_venv_open_terminal'),

    # 通用操作
    path('api/toggle-lock/', views.api_toggle_lock, name='api_toggle_lock'),

    # 供 python 应用顶部“清理缓存”按钮在本 Tab 激活时调用
    path('api/clear-cache/', views.api_clear_cache, name='api_clear_cache'),
]
