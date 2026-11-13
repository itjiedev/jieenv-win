meta = {
    # 基础信息
    'app_name': 'dbmysql',
    'display_name': 'mysql数据库管理',
    'version': '1.14.1',
    'description': '安装管理数据库Mysql',
    'is_root': False,
    'url_prefix': 'db',
    'dependencies': {
        'jiecore': '>=1.36.0',
    },
    'pip_dependencies': {
        'mysqlclient': '>=1.4.3',
    },
    'menus': [
        {
            'menu_id': 'dbmysql',
            'parent_id': 'db',
            'name': 'MySQL',
            'icon': '',
            'sort': 10,
            'is_show': True,
            'perm_code': '',
            'path': {
                'viewname': 'dbmysql:index',
                'urlconf': '',
                'args': [],
                'kwargs': {},
                'current_app': '',
                'query': {},
                'fragment': '',
            },
        },
    ],
}