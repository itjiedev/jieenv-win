"""
dbmysql 生命周期钩子
on_enable → 注册 provider_catalog provider（db.mysql）
on_uninstall → 注销归属本应用的所有 provider
"""
from jiecore.provider_catalog import (
    register_provider, unregister_providers_by_app,
)
import logging

logger = logging.getLogger(__name__)


def on_enable(context):
    pk = register_provider({
        "category": "db",
        "provider_id": "mysql",
        "app_name": "dbmysql",
        "label": "MySQL",
        "icon": "fa-solid fa-database",
        "lifecycle": {
            "create": {
                "service": ("dbmysql", "create_database"),
                "config_schema": [
                    {"key": "instance_id", "label": "MySQL 实例", "type": "select",
                     "options_from": {"capability": "list_instances"},
                     "default": {"match": "default"},
                     "required": True, "hint": "dbmysql 中已注册的实例"},
                    {"key": "db_name", "label": "数据库名", "type": "text",
                     "required": True, "default_from": "dir_slug",
                     "hint": "默认使用项目目录名"},
                    {"key": "user", "label": "用户名", "type": "text",
                     "required": True, "default": "root"},
                    {"key": "password", "label": "密码", "type": "password",
                     "required": True, "sensitive": True,
                     "hint": "用于登录 MySQL 自动创建数据库，留空则跳过自动建库"},
                ],
            },
            "verify": {
                "service": ("dbmysql", "verify_database"),
                "config_schema": [
                    {"key": "instance_id", "label": "MySQL实例", "type": "text", "required": True},
                    {"key": "user", "label": "用户名", "type": "text", "required": True},
                    {"key": "password", "label": "密码", "type": "password", "required": False, "sensitive": True},
                ],
            },
            "delete": {
                "service": ("dbmysql", "delete_database"),
                "config_schema": [
                    {"key": "instance_id", "label": "MySQL实例", "type": "text", "required": True},
                    {"key": "db_name", "label": "数据库名", "type": "text", "required": True},
                    {"key": "user", "label": "用户名", "type": "text", "required": True},
                    {"key": "password", "label": "密码", "type": "password", "required": False, "sensitive": True},
                ],
            },
            "health": {
                "service": ("dbmysql", "provider_health_check"),
            },
            "preflight": {
                "service": ("dbmysql", "preflight_check"),
                "config_schema": [
                    {"key": "instance_id", "label": "MySQL实例", "type": "text", "required": True},
                    {"key": "db_name", "label": "数据库名", "type": "text", "required": True},
                    {"key": "user", "label": "用户名", "type": "text", "required": True},
                    {"key": "password", "label": "密码", "type": "password", "required": False, "sensitive": True},
                ],
            },
            "_frontend_context": {
                "service": ("dbmysql", "frontend_context"),
            },
        },
        "frontend_extras": {
            "create_embed": "dbmysql/embeds/create_binding_extra.html",
            "context_service": True,
        },
        "capabilities": {
            "list_instances": {"service": ("dbmysql", "list_mysql_instances")},
            "list_dbs": {
                "service": ("dbmysql", "list_databases"),
                "config_schema": [
                    {"key": "instance_id", "label": "MySQL实例", "type": "text", "required": True},
                    {"key": "user", "label": "用户名", "type": "text", "required": True},
                    {"key": "password", "label": "密码", "type": "password", "required": True, "sensitive": True},
                ],
            },
            "check_port": {
                "service": ("dbmysql", "check_port"),
                "config_schema": [
                    {"key": "instance_id", "label": "MySQL实例", "type": "text", "required": True},
                ],
            },
            "start_service": {
                "service": ("dbmysql", "start_service"),
                "config_schema": [
                    {"key": "instance_id", "label": "MySQL实例", "type": "text", "required": True},
                ],
            },
            "check_db_exists": {
                "service": ("dbmysql", "check_db_exists"),
                "config_schema": [
                    {"key": "instance_id", "label": "MySQL实例", "type": "text", "required": True},
                    {"key": "db_name", "label": "数据库名", "type": "text", "required": True},
                    {"key": "user", "label": "用户名", "type": "text", "required": True},
                    {"key": "password", "label": "密码", "type": "password", "required": True, "sensitive": True},
                ],
            },
        },
    })
    logger.info('provider 注册: %s (db.mysql)', pk)


def on_uninstall(context):
    removed = unregister_providers_by_app("dbmysql")
    logger.info('provider 注销: %s', removed)