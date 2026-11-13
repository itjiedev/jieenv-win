"""
conda 扩展钩子注册（贡献者视角）

向 mainapp.home.environments 扩展点注册 Conda 发行版/环境摘要行，
供首页"环境总览"板块展示。
"""
import logging
import os

from django.urls import reverse

logger = logging.getLogger(__name__)


def _home_environments(context):
    """首页环境总览：返回 Conda 摘要行。

    fields 为 {label, value} 字典列表：首字段为"X个发行版 / Y个环境"，
    其后为发行版名称列表（最多展示前 3 个，超过补"等N个"）。
    优先读缓存（与 views.py api_envs 同款逻辑），避免首页卡顿；
    缓存未命中时跑 conda env list --json 实时探测。
    无安装记录时返回"未安装"摘要行。
    """
    from apps.conda import storage, cache, conda_ops

    env = {
        'menu_id': 'runtimes',
        'name': 'Conda',
        'icon': 'fa-solid fa-circle',
        'icon_color': '#44A833',
        'manage_url': reverse('conda:index'),
    }

    installations = storage.list_installations()
    if not installations:
        env['fields'] = [{'value': '未安装'}, {'value': '-'}]
        return env

    total_envs = 0
    distro_names = []
    for inst in installations:
        distro_id = inst.get('id', '')
        envs = None
        if distro_id:
            envs = cache.get_envs_cache(distro_id)
        if envs is None:
            envs = conda_ops.list_envs(inst.get('conda_bat', ''))
        total_envs += len(envs)
        distro_names.append(
            inst.get('name', '')
            or os.path.basename(inst.get('install_dir', ''))
        )

    fields = [{'value': f'{len(installations)}个发行版 / {total_envs}个环境'}]
    names_str = ', '.join(distro_names[:3])
    if len(distro_names) > 3:
        names_str += f' 等{len(distro_names)}个'
    fields.append({'label': '发行版', 'value': names_str})
    env['fields'] = fields
    return env


def register():
    from jiecore.ext_hook_registry import register_hook
    register_hook(
        'mainapp.home.environments',
        _home_environments,
        app_name='conda',
        description='在首页"环境总览"中展示 Conda 发行版/环境摘要',
        sort=25,
    )