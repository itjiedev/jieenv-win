"""
dbmysql 扩展钩子注册（贡献者视角）

向 mainapp.home.environments 扩展点注册 MySQL 实例摘要行，
供首页"环境总览"板块展示。
"""
from django.urls import reverse
from concurrent.futures import ThreadPoolExecutor


def _home_environments(context):
    """首页环境总览：返回 MySQL 实例摘要行，展示字段由本应用自行决定。

    fields 为 {label, value} 字典列表：首字段为实例总数，其后为运行中
    汇总（数量 + 运行中实例名:端口）；无登记实例时返回"未安装"摘要行。
    端口检测使用线程池并发执行，避免多实例时串行等待。
    """
    from apps.dbmysql import storage
    from apps.dbmysql.mysql_resolve import check_port

    env = {
        'menu_id': 'db',
        'name': 'MySQL',
        'icon': 'fa-solid fa-circle',
        'icon_color': '#00758f',
        'manage_url': reverse('dbmysql:index'),
    }
    instances = storage.list_instances()
    if not instances:
        env['fields'] = [{'value': '未安装'}, {'value': '-'}]
        return env

    # 并发检测端口状态（TCP 连通性）
    def _check(inst):
        port = int(inst.get('port') or 3306)
        host = inst.get('host', '127.0.0.1')
        return check_port(host, port)

    with ThreadPoolExecutor(max_workers=min(len(instances), 8)) as pool:
        reachable_list = list(pool.map(_check, instances))

    running = [inst for inst, ok in zip(instances, reachable_list) if ok]
    fields = [{'value': f'{len(instances)}个实例'}]
    if running:
        names = ', '.join(
            f"{inst.get('name', '')}:{inst.get('port') or 3306}" for inst in running
        )
        fields.append({'label': '运行中', 'value': f'{len(running)} [ {names} ]'})
    else:
        fields.append({'label': '运行中', 'value': '0'})
    env['fields'] = fields
    return env


def register():
    from jiecore.ext_hook_registry import register_hook
    register_hook(
        'mainapp.home.environments',
        _home_environments,
        app_name='dbmysql',
        description='在首页"环境总览"中展示 MySQL 实例摘要行',
        sort=20,
    )
