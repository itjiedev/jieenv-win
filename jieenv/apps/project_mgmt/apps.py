from django.apps import AppConfig


class ProjectMgmtConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.project_mgmt'
    verbose_name = '项目管理'

    def ready(self):
        from jiecore import provider_catalog, event_bus
        from . import storage

        provider_catalog.start_health_auto_refresh(30)

        cursor = storage.read_last_cursor()
        missed = provider_catalog.get_events_since(cursor)
        if missed:
            storage.apply_events(missed)
            new_cursor = provider_catalog.get_event_cursor()
            storage.write_last_cursor(new_cursor)

        def _on_provider_registered(payload):
            storage.invalidate_health_cache()

        def _on_provider_unregistered(payload):
            storage.invalidate_health_cache()

        event_bus.subscribe(event_bus.EVENT_PROVIDER_REGISTERED, _on_provider_registered)
        event_bus.subscribe(event_bus.EVENT_PROVIDER_UNREGISTERED, _on_provider_unregistered)