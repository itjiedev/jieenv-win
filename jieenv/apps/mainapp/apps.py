from django.apps import AppConfig


class MainappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.mainapp'
    label = 'mainapp'
    verbose_name = '主应用'
    