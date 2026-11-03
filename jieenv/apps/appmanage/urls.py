from django.urls import path

from . import views

app_name = 'appmanage'

urlpatterns = [
    path('list/', views.AppListView.as_view(), name='list'),
    path('install/', views.AppInstallView.as_view(), name='install'),
    path('<str:app_name>/upgrade/', views.AppUpgradeView.as_view(), name='upgrade'),
    path('<str:app_name>/detail/', views.AppDetailView.as_view(), name='detail'),
    path('<str:app_name>/enable/', views.AppConfirmActionView.as_view(), {'action': 'enable'}, name='enable'),
    path('<str:app_name>/disable/', views.AppConfirmActionView.as_view(), {'action': 'disable'}, name='disable'),
    path('<str:app_name>/uninstall/', views.AppConfirmActionView.as_view(), {'action': 'uninstall'}, name='uninstall'),
    path('<str:app_name>/delete/', views.AppConfirmActionView.as_view(), {'action': 'delete'}, name='delete'),
]
