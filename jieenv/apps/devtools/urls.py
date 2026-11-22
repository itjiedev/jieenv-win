from django.urls import path, include

from apps.devtools.views import IndexView, api_cleanup_downloads
from apps.devtools.views.pycharm_views import (
    PycharmVersionsView, PycharmDetailView, PycharmVersionsRefreshView,
    PycharmInstallView, PycharmUninstallView,
    api_fetch_pycharm_version, api_pycharm_info_refresh, api_pycharm_install,
    api_pycharm_uninstall, api_start_pycharm, api_pycharm_reset,
)
from apps.devtools.views.vscode_views import (
    VscodeVersionsView, VscodeInstallView, VscodeDetailView, VscodeUninstallView,
    api_vscode_install, api_vscode_uninstall, api_start_vscode,
    api_vscode_info_refresh, api_fetch_vscode_latest, api_vscode_reset,
)
from apps.devtools.views.dbeaver_views import (
    DbeaverVersionsView, DbeaverInstallView, DbeaverDetailView, DbeaverUninstallView,
    api_dbeaver_install, api_dbeaver_uninstall, api_start_dbeaver,
    api_dbeaver_info_refresh, api_fetch_dbeaver_latest, api_dbeaver_check_dir,
    api_dbeaver_reset,
)

app_name = 'devtools'

urlpatterns = [
    path('', IndexView.as_view(), name='index'),

    # PyCharm
    path('pycharm/versions/', PycharmVersionsView.as_view(), name='pycharm_versions'),
    path('pycharm/versions/<str:main_version>/', PycharmVersionsView.as_view(), name='pycharm_versions_param'),
    path('pycharm/refresh/', PycharmVersionsRefreshView.as_view(), name='pycharm_versions_refresh'),
    path('pycharm/detail/', PycharmDetailView.as_view(), name='pycharm_detail'),
    path('pycharm/uninstall/', PycharmUninstallView.as_view(), name='pycharm_uninstall'),
    path('pycharm/install/<str:version>/', PycharmInstallView.as_view(), name='pycharm_install'),

    path('api/pycharm/version/refresh/', api_fetch_pycharm_version, name='api_pycharm_version_refresh'),
    path('api/pycharm/detail/refresh/', api_pycharm_info_refresh, name='api_pycharm_detail_refresh'),
    path('api/pycharm/start/', api_start_pycharm, name='api_start_pycharm'),
    path('api/pycharm/uninstall/', api_pycharm_uninstall, name='api_pycharm_uninstall'),
    path('api/pycharm/install/', api_pycharm_install, name='api_pycharm_install'),
    path('api/pycharm/reset/', api_pycharm_reset, name='api_pycharm_reset'),

    # VSCode
    path('vscode/versions/', VscodeVersionsView.as_view(), name='vscode_versions'),
    path('vscode/install/', VscodeInstallView.as_view(), name='vscode_install'),
    path('vscode/detail/', VscodeDetailView.as_view(), name='vscode_detail'),
    path('vscode/uninstall/', VscodeUninstallView.as_view(), name='vscode_uninstall'),

    path('api/vscode/detail/refresh/', api_vscode_info_refresh, name='api_vscode_detail_refresh'),
    path('api/vscode/latest/refresh/', api_fetch_vscode_latest, name='api_vscode_latest_refresh'),
    path('api/vscode/start/', api_start_vscode, name='api_start_vscode'),
    path('api/vscode/install/', api_vscode_install, name='api_vscode_install'),
    path('api/vscode/uninstall/', api_vscode_uninstall, name='api_vscode_uninstall'),
    path('api/vscode/reset/', api_vscode_reset, name='api_vscode_reset'),

    # DBeaver
    path('dbeaver/versions/', DbeaverVersionsView.as_view(), name='dbeaver_versions'),
    path('dbeaver/install/', DbeaverInstallView.as_view(), name='dbeaver_install'),
    path('dbeaver/detail/', DbeaverDetailView.as_view(), name='dbeaver_detail'),
    path('dbeaver/uninstall/', DbeaverUninstallView.as_view(), name='dbeaver_uninstall'),

    path('api/dbeaver/detail/refresh/', api_dbeaver_info_refresh, name='api_dbeaver_detail_refresh'),
    path('api/dbeaver/latest/refresh/', api_fetch_dbeaver_latest, name='api_dbeaver_latest_refresh'),
    path('api/dbeaver/start/', api_start_dbeaver, name='api_start_dbeaver'),
    path('api/dbeaver/install/', api_dbeaver_install, name='api_dbeaver_install'),
    path('api/dbeaver/uninstall/', api_dbeaver_uninstall, name='api_dbeaver_uninstall'),
    path('api/dbeaver/check-dir/', api_dbeaver_check_dir, name='api_dbeaver_check_dir'),
    path('api/dbeaver/reset/', api_dbeaver_reset, name='api_dbeaver_reset'),

    path('api/cleanup_downloads/', api_cleanup_downloads, name='api_cleanup_downloads'),
]