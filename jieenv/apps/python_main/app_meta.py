from jiecore.app_manager import AppManager

def _has_child_apps():
    """检查是否有已启用应用依赖 python_main（含依赖声明即可）"""
    enabled = AppManager.get_enabled_apps()
    for info in enabled.values():
        deps = info.get('dependencies') or {}
        if 'python_main' in deps:
            return True
    return False

meta = {
    'app_name': 'python_main',
    'display_name': 'python-主应用',
    'version': '1.21.0',
    'description': 'python语言环境主应用，提供按钮式导航框架、开发工具管理，pip包管理通用服务、全局PyPI源配置,常用开发工具安装等基础功能',
    "is_platform_fixed": True,
    "is_enabled": True,
    'is_root': False,
    'url_prefix': 'runtimes',
    'dependencies': {
        'jieenv_platform': '>=1.32.0',
        'jiecore': '>=1.36.0',
        'mainapp': '>=1.3.5',
    },
    'pip_dependencies': {
        "requests":"",
    },
    'menus': [
        {
            'menu_id': 'python_main',
            'parent_id': 'runtimes',
            'name': 'python',
            'icon': '',
            'sort': 100,
            'is_show': True,
            'perm_code': '',
            'path': {
                'viewname': 'python_main:index' if _has_child_apps() else '',
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