meta = {
    # 基础信息
    'app_name': 'conda',
    'display_name': 'Conda管理',
    'version': '1.2.7',
    'description': 'conda系列的安装、conda环境的管理',
    'is_root': False,
    "is_enabled": True,
    'url_prefix': '',
    'dependencies': {
        'jieenv_platform': '>=1.50.0',  # 最低平台版本（强烈建议）
        'mainapp': '>=1.3.0',
        'jiecore': '>=1.36.0',
    },
    'pip_dependencies': {
        'pyyaml': '>=6.0',
    },
    'menus': [
        {
            'menu_id': 'conda',
            'parent_id': 'runtimes',
            'name': 'conda',
            'icon': '',
            'sort': 150,
            'is_show': True,
            'perm_code': '',
            'path': {
                'viewname': 'conda:index',
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