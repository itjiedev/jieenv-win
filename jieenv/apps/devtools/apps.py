import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class DevtoolsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.devtools'
    verbose_name = '开发工具'

    def ready(self):
        from apps.devtools._discover import discover_and_register
        discover_and_register()