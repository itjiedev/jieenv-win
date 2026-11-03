from django.urls import path
from . import views

app_name = 'jiecore'

urlpatterns = [
    # ── 页面视图 ──
    path('log/operations/', views.OperationLogListView.as_view(), name='operation_log_list'),
    path('app/list/', views.AppListView.as_view(), name='app_list'),
    path('platform/upgrade/', views.PlatformUpgradeView.as_view(), name='platform_upgrade_page'),
    # 应用管理 POST 操作（重定向回列表页，供 app_list.html 表单使用）
    path('app/sync/', views.app_sync_apps, name='sync_apps'),
    path('app/refresh/', views.app_refresh_cache, name='refresh_cache'),
    path('app/install/', views.app_install, name='app_install'),
    path('app/<str:app_name>/enable/', views.app_enable, name='app_enable'),
    path('app/<str:app_name>/disable/', views.app_disable, name='app_disable'),
    path('app/<str:app_name>/uninstall/', views.app_uninstall, name='app_uninstall'),
    path('app/<str:app_name>/delete/', views.app_delete, name='app_delete'),
    # ── JSON API（前端 AJAX 调用） ──
    path('api/ping/', views.api_ping, name='api_ping'),
    path('api/fs/dirs/', views.api_fs_dirs, name='api_fs_dirs'),
    path('api/fs/open-dir/', views.api_fs_open_dir, name='api_fs_open_dir'),
    path('api/service/state/', views.api_service_state, name='api_service_state'),
    path('api/service/<str:action>/', views.api_service_action, name='api_service_action'),
    path('api/service/startup-state/', views.api_service_startup_state,
         name='api_service_startup_state'),
    path('api/service/startup/', views.api_service_startup, name='api_service_startup'),
    # 通用后台任务轮询（增量日志），路径与 jiecore_tasklog.js 的 API_TASK_STATUS
    # 常量保持一致，前端 JieTaskLog.open({taskId: ...}) 免配置即用
    path('api/task-status/<str:task_id>/', views.api_task_status, name='api_task_status'),
    # 全局任务指示器（JieTaskBar）轮询活跃 + 近期任务列表
    path('api/task-list/', views.api_task_list, name='api_task_list'),
    path('api/restart/', views.api_restart, name='api_restart'),
    path('api/save-restart-url/', views.api_save_restart_url, name='api_save_restart_url'),
    path('api/app/install/', views.api_app_install, name='api_app_install'),
    path('api/app/<str:app_name>/<str:action>/', views.api_app_action, name='api_app_action'),
    path('api/platform/upgrade/', views.api_platform_upgrade, name='api_platform_upgrade'),
    path('api/refresh/', views.api_refresh_cache, name='api_refresh'),
    path('api/opendir/', views.api_open_dir, name='api_opendir'),
    path('api/open-env-variables/', views.api_open_env_variables, name='api_open_env_variables'),
    # ── 操作日志管理 API ──
    path('api/op-log/detail/<int:log_id>/', views.api_op_log_detail, name='api_op_log_detail'),
    path('api/op-log/delete/<int:log_id>/', views.api_op_log_delete, name='api_op_log_delete'),
    path('api/op-log/clear/', views.api_op_log_clear, name='api_op_log_clear'),
]