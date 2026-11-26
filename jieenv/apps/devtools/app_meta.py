meta = {
    'app_name': 'devtools',
    'display_name': '开发工具管理',
    'version': '1.2.1',
    'description': '开发工具统一管理平台，支持 IDE、数据库管理、API 工具等的安装、卸载、配置',
    'is_root': False,
    'is_enabled': True,
    'url_prefix': '',
    'dependencies': {
        'jiecore': '>=1.36.0',
        'mainapp': '>=1.3.6',
    },
    'pip_dependencies': {

    },
    'menus': [
        {
            'menu_id': 'devtools',
            'parent_id': '',
            'name': '开发工具',
            'icon': 'fa-solid fa-wrench',
            'sort': 810,
            'is_show': True,
            'perm_code': '',
            'path': {
                'viewname': 'devtools:index',
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