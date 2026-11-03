from django.urls import path
from . import views

app_name = 'project_mgmt'

urlpatterns = [
    path('', views.IndexView.as_view(), name='index'),
    path('import/local/', views.ImportLocalView.as_view(), name='import_local'),
    path('create/', views.CreateWizardView.as_view(), name='create_wizard'),
    path('create/api/binding-form/', views.BindingFormApiView.as_view(), name='api_binding_form'),
    path('create/result/<str:task_id>/', views.CreateResultView.as_view(), name='create_result'),
    path('api/create/', views.api_create_project, name='api_create_project'),
    path('api/open-dir/', views.api_open_dir, name='api_open_dir'),
    path('api/open-terminal/', views.api_open_terminal, name='api_open_terminal'),
    path('api/provider/<str:provider_key>/<str:capability>/',
         views.api_provider_capability, name='api_provider_capability'),
    path('api/check-project-dir/', views.api_check_project_dir, name='api_check_project_dir'),
    path('api/scan-dir/', views.api_scan_dir, name='api_scan_dir'),
    path('api/import-local/', views.api_import_local, name='api_import_local'),

    path('delete/<str:project_id>/', views.DeleteConfirmView.as_view(), name='delete_confirm'),
    path('api/delete/execute/', views.api_delete_execute, name='api_delete_execute'),
    path('api/delete/status/', views.api_delete_status, name='api_delete_status'),

    path('trash/', views.TrashIndexView.as_view(), name='trash_index'),
    path('trash/delete/<str:project_id>/', views.PermanentDeleteConfirmView.as_view(), name='permanent_delete_confirm'),
    path('api/trash/restore/', views.api_trash_restore, name='api_trash_restore'),
    path('api/trash/delete-permanent/', views.api_trash_delete_permanent, name='api_trash_delete_permanent'),
    path('api/trash/remove-record/', views.api_trash_remove_record, name='api_trash_remove_record'),
    path('api/health-batch/', views.api_health_batch, name='api_health_batch'),

    path('project/<str:project_id>/', views.ProjectDetailView.as_view(), name='detail'),
    path('api/snapshot/<str:project_id>/', views.api_snapshot_json, name='api_snapshot_json'),
    path('api/health/check/', views.api_health_check, name='api_health_check'),
    path('api/binding/edit/', views.api_edit_binding, name='api_edit_binding'),
]