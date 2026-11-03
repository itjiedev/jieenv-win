from django.urls import path

app_name = 'mainapp'

from . import views

urlpatterns = [
    path('', views.HomeView.as_view(), name='index'),
    path('api/home-environments/', views.api_home_environments, name='api_home_environments'),
    path('api/home-projects-stats/', views.api_home_projects_stats, name='api_home_projects_stats'),
]