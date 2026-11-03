meta = {
    'app_name': 'appmanage',
    'display_name': '应用管理',
    'version': '1.1.0',
    "is_platform_fixed": True,
    'description': '通用应用管理模块，提供应用的安装、升级、启用、禁用、卸载等功能',
    'is_root': False,
    'url_prefix': 'system',
    'dependencies': {
        'jiecore': '>=1.36.0',
    },
    'pip_dependencies': {},
    'menus': [
        {
            'menu_id': 'appmanage',
            'parent_id': 'system',
            'name': '应用管理',
            'icon': '',
            'sort': 90,
            'is_show': True,
            'perm_code': '',
            'path': {
                'viewname': 'appmanage:list',
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