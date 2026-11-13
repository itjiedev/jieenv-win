import logging

from .config import USER_TEMPLATES_DIR

logger = logging.getLogger(__name__)

BUILTIN_TEMPLATES = [
    {
        'template_id': 'empty',
        'name': '空白项目',
        'description': '从零开始创建，无预设组件',
        'icon': 'fa-solid fa-file',
        'category': 'basic',
        'bindings': [],
    },
    {
        'template_id': 'python_simple',
        'name': 'Python 基础',
        'description': '仅有 Python 运行时 + venv',
        'icon': 'fa-brands fa-python',
        'category': 'python',
        'bindings': [
            {'provider_key': 'runtime.python', 'label': 'Python 运行时',
             'preferences': {'default_version': '3.13.16',
                             'python_exe': {'match': 'default'}}},
        ],
    },
    {
        'template_id': 'python_django',
        'name': 'Python + Django',
        'description': 'Python 运行时 + Django 包 + startproject 脚手架',
        'icon': 'fa-brands fa-python',
        'category': 'python',
        'bindings': [
            {'provider_key': 'runtime.python', 'label': 'Python 运行时',
             'preferences': {'default_version': '3.13.16',
                             'python_exe': {'match': 'default'}}},

            {'type': 'lang_packages', 'target_provider_key': 'runtime.python',
             'invoke_action': 'install_packages',
             'params': {'packages': ['django~=5.2']},
             'label': '安装 Django 包'},

            {'type': 'python_module', 'target_provider_key': 'runtime.python',
             'invoke_action': 'run_python_module',
             'params': {'module': 'django', 'args': 'startproject {PROJECT_SUB_DIR} .'},
             'label': 'Django 项目脚手架',
             'project_dir_fix': 'python_module_name'},
        ],
    },
    {
        'template_id': 'python_django_mysql',
        'name': 'Python + Django + MySQL',
        'description': 'Python 3.13 + Django 5.2 + MySQL 8.4 数据库',
        'icon': 'fa-solid fa-database',
        'category': 'python',
        'bindings': [
            {'provider_key': 'runtime.python', 'label': 'Python 运行时',
             'preferences': {'default_version': '3.13.16',
                             'python_exe': {'match': 'default'}}},

            {'provider_key': 'db.mysql', 'label': 'MySQL 数据库',
             'depends_on': [],
             'preferences': {'default_version': '8.4',
                             'instance_id': {'match': 'default'}}},

            {'type': 'lang_packages', 'target_provider_key': 'runtime.python',
             'invoke_action': 'install_packages',
             'params': {'packages': ['django~=5.2']},
             'label': '安装 Django 包'},

            {'type': 'python_module', 'target_provider_key': 'runtime.python',
             'invoke_action': 'run_python_module',
             'params': {'module': 'django', 'args': 'startproject {PROJECT_SUB_DIR} .'},
             'label': 'Django 项目脚手架',
             'project_dir_fix': 'python_module_name'},
        ],
    },
]


TEMPLATE_CATEGORIES = [
    ('basic',     '基础',   'fa-solid fa-circle'),
    ('python',    'Python', 'fa-brands fa-python'),
]


def get_template_categories() -> list:
    templates = get_all_templates()
    counts = {}
    for t in templates:
        cat = t.get('category', 'basic')
        counts[cat] = counts.get(cat, 0) + 1
    result = [('all', '全部', 'fa-solid fa-grid-2', len(templates))]
    for cat_id, label, icon in TEMPLATE_CATEGORIES:
        cnt = counts.get(cat_id, 0)
        if cnt > 0:
            result.append((cat_id, label, icon, cnt))
    return result


def get_builtin_templates() -> list:
    return [dict(t) for t in BUILTIN_TEMPLATES]


def get_user_templates() -> list:
    """扫描用户自定义模板目录。Phase 1 返回空列表（Phase 4 扩展生态启用）。"""
    return []


def get_all_templates() -> list:
    builtin = get_builtin_templates()
    user = get_user_templates()
    return builtin + user


def get_template(template_id: str) -> dict | None:
    for t in get_all_templates():
        if t.get('template_id') == template_id:
            return dict(t)
    return None