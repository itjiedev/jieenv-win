meta = {
    'app_name': 'python_installer',
    'display_name': 'python-安装程序模式',
    'version': '1.17.0',
    'description': '使用官网安装程序安装的Python环境并进行管理，同一系列的高版本会对低版本自动升级',
    "is_platform_fixed": False,
    "is_enabled": True,
    'is_root': False,
    'url_prefix': 'runtimes',
    'dependencies': {
        'jieenv_platform': '>=1.32.0',
        'jiecore': '>=1.36.0',
        'mainapp': '>=1.3.5',
        'python_main': '>=1.15.0',
    },
    'pip_dependencies': {
        'requests': ''
    },
    'menus': [],
}