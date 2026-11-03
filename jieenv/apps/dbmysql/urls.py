from django.urls import path

from . import views

app_name = 'dbmysql'

urlpatterns = [
    # 页面
    path('', views.IndexView.as_view(), name='index'),
    path('instance/add/', views.InstanceAddView.as_view(), name='instance_add'),
    path('instance/<str:instance_id>/detail/',
         views.InstanceDetailView.as_view(), name='instance_detail'),
    path('instance/<str:instance_id>/config/',
         views.InstanceConfigView.as_view(), name='instance_config'),
    path('install/', views.InstallView.as_view(), name='install'),
    path('install/options/', views.InstallOptionsView.as_view(), name='install_options'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('conn/<str:conn_id>/databases/', views.DatabasesView.as_view(), name='databases'),
    path('conn/<str:conn_id>/db/<str:db>/tables/', views.TablesView.as_view(), name='tables'),
    path('conn/<str:conn_id>/db/<str:db>/table/<str:table>/columns/',
         views.ColumnsView.as_view(), name='columns'),
    path('conn/<str:conn_id>/db/<str:db>/table/<str:table>/indexes/',
         views.IndexesView.as_view(), name='indexes'),
    path('conn/<str:conn_id>/user/<str:user>/host/<str:host>/grants/',
         views.UserGrantsView.as_view(), name='user_grants'),
    # 新建/表单页面（从列表页链接跳转）
    path('conn/<str:conn_id>/db/create/', views.DatabaseCreateView.as_view(), name='db_create'),
    path('conn/<str:conn_id>/db/<str:db>/table/create/',
         views.TableCreateView.as_view(), name='table_create'),
    path('conn/<str:conn_id>/db/<str:db>/table/<str:table>/column/add/',
         views.ColumnAddView.as_view(), name='column_add'),
    path('conn/<str:conn_id>/db/<str:db>/table/<str:table>/column/<str:name>/edit/',
         views.ColumnEditView.as_view(), name='column_edit'),

    # 实例管理 API
    path('api/instance-status/', views.api_instance_status, name='api_instance_status'),
    path('api/instance/add/', views.api_instance_add, name='api_instance_add'),
    path('api/instance/resolve/', views.api_instance_resolve, name='api_instance_resolve'),
    path('api/instance/remove/', views.api_instance_remove, name='api_instance_remove'),
    path('api/instance/start/', views.api_instance_start, name='api_instance_start'),
    path('api/instance/stop/', views.api_instance_stop, name='api_instance_stop'),
    # 注意：register/register-task/remove-service/uninstall 必须在 <str:action> 之前
    path('api/instance/service/register/',
         views.api_instance_register_service, name='api_instance_register_service'),
    path('api/instance/service/register-task/',
         views.api_instance_register_service_task,
         name='api_instance_register_service_task'),
    path('api/instance/service/remove-service/',
         views.api_instance_remove_service,
         name='api_instance_remove_service'),
    path('api/instance/uninstall/',
         views.api_instance_uninstall, name='api_instance_uninstall'),
    path('api/instance/process/<str:action>/',
         views.api_instance_process_task, name='api_instance_process_task'),
    path('api/instance/service/<str:action>/',
         views.api_instance_service_task, name='api_instance_service_task'),
    path('api/instance/startup-type/',
         views.api_instance_startup_type, name='api_instance_startup_type'),
    path('api/instance/set-startup-type/',
         views.api_instance_set_startup_type, name='api_instance_set_startup_type'),
    path('api/instance/conn/clear/', views.api_instance_conn_clear,
         name='api_instance_conn_clear'),

    path('api/task/status/<str:task_id>/', views.api_task_status, name='api_task_status'),
    path('api/instance/open-dir/', views.api_instance_open_dir, name='api_instance_open_dir'),
    path('api/instance/config/read/', views.api_instance_config_read, name='api_instance_config_read'),
    path('api/instance/config/save/', views.api_instance_config_save, name='api_instance_config_save'),
    path('api/instance/change-port/', views.api_instance_change_port, name='api_instance_change_port'),
    path('api/instance/rename/', views.api_instance_rename, name='api_instance_rename'),
    path('api/port-check/', views.api_port_check, name='api_port_check'),
    path('api/clear-downloads/', views.api_clear_downloads, name='api_clear_downloads'),

    # 设置
    path('settings/', views.SettingsView.as_view(), name='settings'),
    path('api/settings/', views.api_settings, name='api_settings'),
    path('api/settings/save/', views.api_settings_save, name='api_settings_save'),

    # 版本抓取 / 安装 API
    path('api/versions/', views.api_versions, name='api_versions'),
    path('api/install/start/', views.api_install_start, name='api_install_start'),
    path('api/install/status/<str:task_id>/', views.api_install_status, name='api_install_status'),

    # 登录 / 登出 API
    path('api/login/', views.api_login, name='api_login'),
    path('api/logout/', views.api_logout, name='api_logout'),
    path('conn/<str:conn_id>/api/change-password/',
         views.api_change_password, name='api_change_password'),

    # 库 / 表 / 字段管理 API（需已登录）
    path('conn/<str:conn_id>/api/db/list/', views.api_db_list, name='api_db_list'),
    path('conn/<str:conn_id>/api/db/<str:db>/tables/', views.api_table_list, name='api_table_list'),
    path('conn/<str:conn_id>/api/charsets/', views.api_charsets, name='api_charsets'),
    path('conn/<str:conn_id>/api/db/', views.api_db_action, name='api_db'),
    path('conn/<str:conn_id>/api/table/', views.api_table_action, name='api_table'),
    path('conn/<str:conn_id>/api/column/', views.api_column_action, name='api_column'),

    # 用户管理 API（需已登录）
    path('conn/<str:conn_id>/api/users/', views.api_users_list, name='api_users_list'),
    path('conn/<str:conn_id>/api/user/', views.api_user_action, name='api_user_action'),
    path('conn/<str:conn_id>/api/user/grants/', views.api_user_grants, name='api_user_grants'),

    # 表数据浏览 API（需已登录）
    path('conn/<str:conn_id>/api/db/<str:db>/table/<str:table>/data/',
         views.api_table_data, name='api_table_data'),
    path('conn/<str:conn_id>/api/db/<str:db>/table/<str:table>/show-create/',
         views.api_table_show_create, name='api_table_show_create'),

    # 索引管理 API（需已登录）
    path('conn/<str:conn_id>/api/db/<str:db>/table/<str:table>/indexes/',
         views.api_index_list, name='api_index_list'),
    path('conn/<str:conn_id>/api/index/', views.api_index_action, name='api_index_action'),

    # 导入导出 API（需已登录）
    path('conn/<str:conn_id>/api/db/export/', views.api_db_export, name='api_db_export'),
]