from django.urls import path

from . import views

app_name = 'python_archive'

urlpatterns = [
    # 页面
    path('index/', views.IndexView.as_view(), name='index'),
    path('settings/', views.SettingsView.as_view(), name='settings'),
    path('versions/', views.VersionListView.as_view(), name='version_list'),
    path('download-source/', views.DownloadSourceView.as_view(), name='download_source'),
    path('install/options/', views.InstallOptionsView.as_view(), name='install_options'),
    path('pip/list/<str:id>/', views.PipListView.as_view(), name='pip_list'),
    path('pip/install/<str:id>/', views.PipInstallView.as_view(), name='pip_install'),

    # venv 页面
    path('venvs/<str:record_id>/', views.VenvListView.as_view(), name='venv_list'),
    path('venvs/pip/list/<str:venv_id>/', views.PipListVenvView.as_view(), name='pip_list_venv'),
    path('venvs/pip/install/<str:venv_id>/', views.PipInstallVenvView.as_view(), name='pip_install_venv'),

    # JSON API
    path('api/versions/', views.api_versions, name='api_versions'),
    path('api/install/start/', views.api_install_start, name='api_install_start'),
    path('api/uninstall/', views.api_uninstall, name='api_uninstall'),
    path('api/uninstall-gui/', views.api_uninstall_gui, name='api_uninstall_gui'),
    path('api/add-path/', views.api_add_path, name='api_add_path'),
    path('api/rename/', views.api_rename, name='api_rename'),
    path('api/open-terminal/', views.api_open_terminal, name='api_open_terminal'),
    path('api/del-record/', views.api_del_record, name='api_del_record'),
    path('api/import/', views.api_import_installed, name='api_import_installed'),
    path('api/clear-cache/', views.api_clear_cache, name='api_clear_cache'),
    path('api/download-sources/', views.api_download_sources, name='api_download_sources'),
    path('api/set-download-source/', views.api_set_download_source, name='api_set_download_source'),
    path('api/settings/save/', views.api_settings_save, name='api_settings_save'),

    # venv JSON API
    path('api/venv/list/<str:record_id>/', views.api_venv_list, name='api_venv_list'),
    path('api/venv/create/', views.api_venv_create, name='api_venv_create'),
    path('api/venv/import/', views.api_venv_import, name='api_venv_import'),
    path('api/venv/delete/', views.api_venv_delete, name='api_venv_delete'),
    path('api/venv/rename/', views.api_venv_rename, name='api_venv_rename'),
    path('api/venv/open-terminal/', views.api_venv_open_terminal, name='api_venv_open_terminal'),
    path('api/venv/verify-l1/<str:venv_id>/', views.api_venv_verify_l1, name='api_venv_verify_l1'),
    path('api/venv/verify/<str:venv_id>/', views.api_venv_verify, name='api_venv_verify'),

    # 通用操作
    path('api/toggle-lock/', views.api_toggle_lock, name='api_toggle_lock'),
]