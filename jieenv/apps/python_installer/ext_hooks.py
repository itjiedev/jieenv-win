"""
python_installer 扩展钩子注册（贡献者视角）

向 python 应用的 python.env.tabs 扩展点注册"安装程序"Tab；
向 python.clear_cache 扩展点注册本应用的清理缓存接口，
当用户处于"安装程序"Tab 时，顶部"清理缓存"按钮会命中本应用而非宿主 python 应用。
向 mainapp.home.environments 扩展点注册 Python 环境摘要行，供首页"环境总览"展示。
"""
from django.urls import reverse


def _installer_tab(context):
    """注册"安装程序"Tab"""
    return {
        'tab_id': 'installer',
        'label': '安装程序模式',
        'sort': 20,
        'active': True,
        'content_url': reverse('python_installer:index'),
    }


def _installer_clear_cache(context):
    """注册本应用的清理缓存端点：env_id 需与 _installer_tab 的 tab_id 一致。"""
    return {
        'env_id': 'installer',
        'endpoint': reverse('python_installer:api_clear_cache'),
        'label': '清理安装程序模式下载缓存',
        'confirm_text': (
            '确定要清理缓存吗？</br>'
            '将清空“安装程序”应用的临时文件（如已下载的 Python 安装包），'
            '<span class="text-muted">不影响已安装的 Python 环境。</span>'
        ),
    }


def _installer_pip_env(context):
    """注册本应用的 pip 环境解析器（python_main.pip_envs 扩展点）。

    宿主 python 应用的通用 pip API 以 env_id 定位解释器；本应用以安装记录
    的 id 作为 env_id，命中时返回 {env_id, python_exe, label}，否则返回 None
    （表示该 env_id 不属于本应用，由其他贡献者解析）。
    """
    env_id = (context or {}).get('env_id') or ''
    if not env_id:
        return None
    # ext_hooks.py 由框架以独立模块动态加载（无父包），故用绝对导入而非相对导入
    from apps.python_installer import storage

    record = storage.get_installed_by_id(env_id)
    if not record or not record.get('python_exe'):
        return None
    return {
        'env_id': env_id,
        'python_exe': record.get('python_exe', ''),
        'label': f"Python {record.get('version', '')}",
    }


def _home_environments(context):
    """首页环境总览：返回 Python 环境摘要行，展示字段由本应用自行决定。

    fields 为 {label, value} 字典列表（label 可选）：首字段为版本总数，
    其后为 PATH默认 / py默认 标记字段；无安装记录时返回"未安装"摘要行。
    使用快速版本检测（单次 subprocess）避免首页卡顿。
    """
    from apps.python_installer import storage, env_ops
    env = {
        'menu_id': 'runtimes',
        'name': 'Python - 安装程序模式',
        'icon': 'fa-solid fa-circle',
        'icon_color': '#3776ab',
        'manage_url': reverse('python_installer:index'),
    }
    records = storage.list_installed()
    if not records:
        env['fields'] = [{'value': '未安装'}, {'value': '-'}]
        return env
    # 快速获取默认标签（单次 subprocess），避免完整版本探测的 2 次 subprocess
    py_default_tag = env_ops.get_py_default_tag_fast()
    records = env_ops.annotate_installed(records, py_default_tag=py_default_tag)

    fields = [{'value': f'{len(records)}个版本'}]
    for r in records:
        if r.get('is_path_default'):
            fields.append({'label': 'PATH默认', 'value': r.get('version', '')})
        if env_ops.py_launcher_installed() and r.get('is_py_default'):
            fields.append({'label': 'py默认', 'value': r.get('version', '')})
    env['fields'] = fields
    return env


def register():
    from jiecore.ext_hook_registry import register_hook
    register_hook(
        'python_main.envs',
        _installer_tab,
        app_name='python_installer',
        description='在 Python 环境管理页注册"安装程序"Tab',
        sort=10,
    )
    register_hook(
        'python_main.clear_cache',
        _installer_clear_cache,
        app_name='python_installer',
        description='为"安装程序"Tab 提供自定义清理缓存端点',
        sort=10,
    )
    register_hook(
        'python_main.pip_envs',
        _installer_pip_env,
        app_name='python_installer',
        description='为宿主通用 pip API 解析安装记录 id → 解释器路径',
        sort=10,
    )
    register_hook(
        'mainapp.home.environments',
        _home_environments,
        app_name='python_installer',
        description='在首页"环境总览"中展示 Python 环境摘要行',
        sort=10,
    )
