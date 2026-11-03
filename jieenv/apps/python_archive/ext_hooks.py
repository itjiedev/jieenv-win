"""
python_archive 扩展钩子注册（贡献者视角）

向 python_main.envs 扩展点注册"独立版本"Tab；
向 python_main.clear_cache 扩展点注册本应用的清理缓存接口；
向 python_main.pip_envs 扩展点注册 pip 环境解析器，供宿主通用 pip API 定位解释器；
向 mainapp.home.environments 扩展点注册 Python 环境摘要行，供首页"环境总览"展示。
"""
from django.urls import reverse


def _archive_tab(context):
    """注册"独立版本模式"Tab"""
    return {
        'tab_id': 'archive',
        'label': '独立版本模式',
        'icon': 'fa-download',
        'sort': 10,
        'active': True,
        'content_url': reverse('python_archive:index'),
    }


def _archive_clear_cache(context):
    """注册本应用的清理缓存端点：env_id 需与 _archive_tab 的 tab_id 一致。"""
    return {
        'env_id': 'archive',
        'endpoint': reverse('python_archive:api_clear_cache'),
        'label': '清理独立版本模式下载缓存',
        'confirm_text': (
            '确定要清理缓存吗？</br>'
            '将清空"独立版本模式"应用的临时文件（如已下载的 Python 压缩包），'
            '<span class="text-muted">不影响已安装的 Python 环境。</span>'
        ),
    }


def _archive_pip_env(context):
    """注册本应用的 pip 环境解析器（python_main.pip_envs 扩展点）。

    宿主 python 应用的通用 pip API 以 env_id 定位解释器；本应用以安装记录
    的 id 作为 env_id，命中时返回 {env_id, python_exe, label}，否则返回 None
    （表示该 env_id 不属于本应用，由其他贡献者解析）。
    """
    env_id = (context or {}).get('env_id') or ''
    if not env_id:
        return None
    from apps.python_archive import storage

    record = storage.get_by_id(env_id)
    if not record or not record.get('python_exe'):
        return None
    return {
        'env_id': env_id,
        'python_exe': record.get('python_exe', ''),
        'label': f"Python {record.get('version', '')}",
    }


def _home_environments(context):
    """首页环境总览：返回 Python 环境摘要行，展示字段由本应用自行决定。

    fields 为 {label, value} 字典列表：首字段为版本总数，
    其后为 PATH默认 标记字段；无安装记录时返回"未安装"摘要行。
    """
    from apps.python_archive import storage, env_ops
    env = {
        'menu_id': 'runtimes',
        'name': 'Python - 独立版本模式',
        'icon': 'fa-solid fa-circle',
        'icon_color': '#28a745',
        'manage_url': reverse('python_archive:index'),
    }
    records = storage.list_installed()
    if not records:
        env['fields'] = [{'value': '未安装'}, {'value': '-'}]
        return env
    records = env_ops.annotate_installed(records)

    fields = [{'value': f'{len(records)}个版本'}]
    for r in records:
        if r.get('is_path_default'):
            fields.append({'label': 'PATH默认', 'value': r.get('version', '')})
    env['fields'] = fields
    return env


def register():
    from jiecore.ext_hook_registry import register_hook
    register_hook(
        'python_main.envs',
        _archive_tab,
        app_name='python_archive',
        description='在 Python 环境管理页注册"独立版本"Tab',
        sort=20,
    )
    register_hook(
        'python_main.clear_cache',
        _archive_clear_cache,
        app_name='python_archive',
        description='为"独立版本"Tab 提供自定义清理缓存端点',
        sort=20,
    )
    register_hook(
        'python_main.pip_envs',
        _archive_pip_env,
        app_name='python_archive',
        description='为宿主通用 pip API 解析安装记录 id → 解释器路径',
        sort=20,
    )
    register_hook(
        'mainapp.home.environments',
        _home_environments,
        app_name='python_archive',
        description='在首页"环境总览"中展示 Python 独立版本环境摘要行',
        sort=20,
    )
