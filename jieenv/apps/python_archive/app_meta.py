meta = {
    # 基础信息
    'app_name': 'python_archive',
    'display_name': 'python-独立版本模式',
    'version': '1.8.0',
    'description': '使用基于压缩包的方式安装python环境，可实现python各个版本的环境独立存在，互相切换。不支持升级。',
    "is_platform_fixed": False,
    "is_enabled": True,
    'is_root': False,
    'url_prefix': '',
    'dependencies': {
        'jieenv_platform': '>=1.32.0',
        'jiecore': '>=1.36.0',
        'python_main': '>=1.15.0',
        'mainapp': '>=1.3.5',
    },
    'pip_dependencies': {
        'requests': '',
    },
    'menus': [
        {
            'menu_id': 'python_archive',
            'parent_id': 'python_main',
            'name': 'python_archive',
            'icon': '',
            'sort': 100,
            'is_show': True,
            'perm_code': '',
            'path': {
                'viewname': '',
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