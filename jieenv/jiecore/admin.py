from django.contrib import admin
from .models import AppRegistry, AppMenuItem


@admin.register(AppRegistry)
class AppRegistryAdmin(admin.ModelAdmin):
    list_display = ['app_name', 'display_name', 'version', 'is_enabled', 'is_root', 'updated_at']
    list_filter = ['is_enabled', 'is_root']
    search_fields = ['app_name', 'display_name']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(AppMenuItem)
class AppMenuItemAdmin(admin.ModelAdmin):
    list_display = ['menu_id', 'name', 'parent_id', 'app_name', 'sort', 'is_show']
    list_filter = ['is_show', 'app_name']
    search_fields = ['menu_id', 'name']
