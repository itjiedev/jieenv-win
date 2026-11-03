from django.apps import AppConfig


def _list_mysql_instances(context=None):
    from . import storage
    records = storage.list_instances()
    options = []
    for r in records:
        host = r.get('host') or '127.0.0.1'
        port = r.get('port') or 3306
        ver = r.get('version') or ''
        ver_tag = f' {ver}' if ver else ''
        name = r.get('name') or f'{host}:{port}'
        options.append({
            'value': r.get('id', ''),
            'label': f'{name}{ver_tag} ({host}:{port})',
            'default': False,
            'version': ver,
        })
    if options:
        options[0]['default'] = True
    return options


class DbmysqlConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.dbmysql'
    verbose_name = 'dbmysql'

    def ready(self):
        from jiecore.app_services import register_service
        from . import provider_services

        register_service(
            'dbmysql', 'create_database',
            provider_services.create_database,
            '在指定 MySQL 实例上创建数据库'
        )
        register_service(
            'dbmysql', 'verify_database',
            provider_services.verify_database,
            '检测 MySQL 实例连接与版本（健康检查）'
        )
        register_service(
            'dbmysql', 'provider_health_check',
            provider_services.provider_health_check,
            '无参数: provider 级别健康检查（探测所有已注册实例端口可达性）'
        )
        register_service(
            'dbmysql', 'delete_database',
            provider_services.delete_database,
            '删除指定 MySQL 实例上的数据库'
        )
        register_service(
            'dbmysql', 'list_databases',
            provider_services.list_databases,
            '列出指定 MySQL 实例上的所有数据库'
        )
        register_service(
            'dbmysql', 'list_mysql_instances',
            _list_mysql_instances,
            '列出 dbmysql 中已注册的实例，返回 [{value, label, default}]'
        )
        register_service(
            'dbmysql', 'preflight_check',
            provider_services.preflight_check,
            'create 前校验：db_name 是否已存在'
        )
        register_service(
            'dbmysql', 'frontend_context',
            provider_services.frontend_context,
            'create 页面 provider 专属前端上下文'
        )
        register_service(
            'dbmysql', 'check_port',
            provider_services.check_port,
            'MySQL 端口可达性探测'
        )
        register_service(
            'dbmysql', 'start_service',
            provider_services.start_service,
            '启动 MySQL 实例服务'
        )
        register_service(
            'dbmysql', 'check_db_exists',
            provider_services.check_db_exists,
            '检查 db_name 是否已存在'
        )