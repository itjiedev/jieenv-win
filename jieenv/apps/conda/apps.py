from django.apps import AppConfig


class CondaConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.conda'
    label = 'conda'
    verbose_name = 'conda'

    def ready(self):
        # ── 注册应用间服务（可选，取消注释按需使用）──────────
        # from jiecore.app_services import register_service
        # from .services import my_service_func
        # register_service('conda', 'my_service', my_service_func, '服务描述')

        # ── 注册私有数据库（可选，取消注释按需使用）──────────
        # from jiecore.private_databases import register_private_databases
        # from . import databases
        # register_private_databases('conda', databases.PRIVATE_DATABASES)
        pass
