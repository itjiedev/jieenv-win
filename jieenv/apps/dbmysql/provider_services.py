"""
dbmysql provider 包装服务层
把 mysql_client + storage 的有状态操作包装成 jiecore app_services 可调用的无状态服务。

所有 service 函数签名: (instance_id, user, password, ...) → dict{success, data, error}
"""
import logging

from . import storage
from .mysql_client import (
    connect, MysqlError,
    create_database as _create_db,
    drop_database as _drop_db,
    list_databases as _list_dbs,
    test_login as _test_login,
)

logger = logging.getLogger(__name__)


def _get_conn(instance_id: str, user: str, password: str):
    inst = storage.get_instance(instance_id)
    if not inst:
        raise ValueError(f'实例 {instance_id!r} 不存在')
    return connect(inst['host'], inst['port'], user, password)


def create_database(instance_id: str, db_name: str, user: str, password: str,
                    charset: str = 'utf8mb4') -> dict:
    """在指定实例上创建数据库。"""
    try:
        conn = _get_conn(instance_id, user, password)
        try:
            _create_db(conn, db_name, charset=charset)
            return {'success': True, 'data': db_name}
        finally:
            conn.close()
    except MysqlError as e:
        return {'success': False, 'error': str(e)}
    except Exception as e:
        return {'success': False, 'error': f'创建数据库失败: {e}'}


def verify_database(instance_id: str, user: str, password: str = "") -> dict:
    """健康检查：测试登录 + 返回版本。

    贡献自愈协议：verify 返回 dict 时可带 verify_code / verify_hint / remedy
    让上层（project_mgmt）能区分异常类型并渲染对应的修复按钮。
    """
    inst = storage.get_instance(instance_id)
    if not inst:
        return {
            'success': False,
            'error': f'实例 {instance_id!r} 不存在',
            'verify_code': 'INSTANCE_MISSING',
            'verify_hint': '项目配置的 MySQL 实例不存在，请在 dbmysql 应用中重新配置',
            'remedy': {'type': 'edit_config', 'label': '编辑配置'},
        }

    port_check = check_port(instance_id)
    if not port_check.get('reachable'):
        return {
            'success': False,
            'error': port_check.get('error') or 'MySQL 端口不可达',
            'verify_code': 'SERVICE_DOWN',
            'verify_hint': 'MySQL 服务未启动或端口不可达',
            'remedy': {'type': 'invoke_action', 'action': 'start_service', 'label': '启动 MySQL'},
        }

    try:
        result = _test_login(inst['host'], inst['port'], user, password)
        return {'success': True, 'data': result}
    except MysqlError as e:
        errno = getattr(e, 'errno', 0) or (e.args[0] if isinstance(e.args[0], int) and e.args else 0)
        if errno == 1045:
            verify_code = 'AUTH_FAILED'
            verify_hint = '用户名或密码不正确'
            remedy = {'type': 'edit_config', 'label': '编辑配置'}
        elif errno in (2002, 2003):
            verify_code = 'SERVICE_DOWN'
            verify_hint = 'MySQL 服务未启动'
            remedy = {'type': 'invoke_action', 'action': 'start_service', 'label': '启动 MySQL'}
        elif errno == 2006:
            verify_code = 'SERVICE_GONE'
            verify_hint = 'MySQL 连接断开，可能需要重启服务'
            remedy = {'type': 'invoke_action', 'action': 'start_service', 'label': '重启 MySQL'}
        else:
            verify_code = 'MYSQL_ERROR'
            verify_hint = str(e)
            remedy = {'type': 'edit_config', 'label': '编辑配置'}
        return {
            'success': False,
            'error': str(e),
            'verify_code': verify_code,
            'verify_hint': verify_hint,
            'remedy': remedy,
        }


# ═══════════════════════════════════════════════════════════════
# provider 级别健康检测（无参数，_ProviderHealthCache 定时调）
# 遍历所有已注册 MySQL 实例，轻量探测 host:port 可达性
# 不做真实登录——避免密码缺失导致误报，只测端口连通
# ═══════════════════════════════════════════════════════════════

def provider_health_check() -> dict:
    import socket
    from . import storage
    try:
        instances = storage.list_instances()
    except Exception as e:
        return {'success': False, 'error': f'storage 访问失败: {e}'}
    if not instances:
        return {'success': True, 'data': {'total': 0, 'healthy': 0}}
    healthy = 0
    for inst in instances:
        host = inst.get('host', '127.0.0.1')
        port = int(inst.get('port', 3306))
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            healthy += 1
        except Exception:
            pass
    return {'success': True, 'data': {'total': len(instances), 'healthy': healthy}}


def delete_database(instance_id: str, db_name: str, user: str, password: str = "") -> dict:
    """在指定实例上删除数据库。"""
    try:
        conn = _get_conn(instance_id, user, password)
        try:
            _drop_db(conn, db_name)
            return {'success': True, 'data': db_name}
        finally:
            conn.close()
    except MysqlError as e:
        msg = str(e)
        low = msg.lower()
        if "doesn't exist" in low or 'unknown database' in low:
            return {'success': True, 'data': {'skipped': True}}
        return {'success': False, 'error': msg}


def list_databases(instance_id: str, user: str, password: str) -> dict:
    """列出指定实例上的所有数据库。"""
    try:
        conn = _get_conn(instance_id, user, password)
        try:
            dbs = _list_dbs(conn)
            return {'success': True, 'data': dbs}
        finally:
            conn.close()
    except MysqlError as e:
        return {'success': False, 'error': str(e)}


def preflight_check(instance_id: str = '', db_name: str = '',
                    user: str = '', password: str = '') -> dict:
    """create 前校验：db_name 是否已在目标实例上存在。

    返回: {'success': True} 或 {'success': False, 'error': '...'}
    """
    if not db_name:
        return {'success': True}

    try:
        conn = _get_conn(instance_id, user, password)
        try:
            dbs = _list_dbs(conn)
            existing = set()
            for d in dbs:
                if isinstance(d, dict):
                    nm = d.get('name', '')
                else:
                    nm = str(d)
                if nm:
                    existing.add(nm.lower())
            if db_name.lower() in existing:
                return {'success': False,
                        'error': f'database 已存在: {db_name}'}
            return {'success': True}
        finally:
            conn.close()
    except MysqlError as e:
        return {'success': False,
                'error': f'无法检查数据库: {e}'}
    except Exception as e:
        return {'success': False,
                'error': f'preflight 检查异常: {e}'}


def frontend_context(**kwargs) -> dict:
    """create 页面前端上下文：MySQL 实例列表及运行状态。

    返回: {'mysql_no_instances': bool, 'mysql_instances_status': list,
           'mysql_instances_status_json': str}
    """
    from . import storage
    import json as _json

    try:
        from jiecore.provider_catalog import invoke_provider
        rr = invoke_provider('db.mysql', 'list_instances')
        instances_list = rr.data if rr.success and isinstance(rr.data, list) else []
    except Exception:
        instances_list = []

    if not instances_list:
        return {'mysql_no_instances': True}

    status_list = []
    for inst in instances_list:
        inst_val = inst.get('value') if isinstance(inst, dict) else inst
        inst_name = inst.get('label', inst_val) if isinstance(inst, dict) else inst_val
        host = '127.0.0.1'
        port = 3306
        run_mode = ''
        service_name = ''
        full = storage.get_instance(inst_val) if inst_val else None
        if full:
            host = full.get('host', host)
            port = full.get('port', port)
            inst_name = full.get('name', inst_name)
            run_mode = full.get('run_mode', '')
            service_name = full.get('service_name', '')
        status_list.append({
            'value': inst_val,
            'name': inst_name,
            'reachable': None,
            'run_mode': run_mode,
            'service_name': service_name,
            'host': host,
            'port': port,
        })

    return {
        'mysql_no_instances': False,
        'mysql_instances_status': status_list,
        'mysql_instances_status_json': _json.dumps(status_list, ensure_ascii=False),
    }


def check_port(instance_id: str = '') -> dict:
    """端口可达性探测。"""
    import socket as _socket
    inst = storage.get_instance(instance_id)
    if not inst:
        return {'success': False, 'error': f'实例 {instance_id!r} 不存在'}
    host = inst.get('host', '127.0.0.1')
    port = int(inst.get('port', 3306))
    try:
        s = _socket.create_connection((host, port), timeout=1.5)
        s.close()
        return {'success': True, 'reachable': True}
    except Exception as e:
        return {'success': True, 'reachable': False, 'error': str(e)}


def start_service(instance_id: str = '') -> dict:
    """启动 MySQL 实例服务（service 模式或进程模式）。"""
    inst = storage.get_instance(instance_id)
    if not inst:
        return {'success': False, 'error': f'实例 {instance_id!r} 不存在'}

    service_name = inst.get('service_name', '')
    run_mode = inst.get('run_mode', '')

    if run_mode == 'service' and service_name:
        try:
            from jiecore.system_ops import start_service as _start
            result = _start(service_name)
            return {'success': result.get('success', False),
                    'message': result.get('message', '')}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    if run_mode == 'process':
        try:
            from pathlib import Path
            from .mysql_process import launch_mysqld, wait_port_ready, START_READY_TIMEOUT
            from .mysql_resolve import find_my_ini
        except Exception as e:
            return {'success': False, 'error': f'启动模块加载失败: {e}'}
        install_dir = inst.get('install_dir', '')
        data_dir = inst.get('data_dir', '')
        host = inst.get('host', '127.0.0.1')
        port = int(inst.get('port') or 3306)
        if not install_dir:
            return {'success': False, 'error': '该实例无安装目录信息'}
        ini = find_my_ini(install_dir, data_dir)
        mysqld = Path(install_dir) / 'bin' / 'mysqld.exe'
        if not ini or not Path(ini).exists():
            return {'success': False, 'error': '安装目录中未找到 my.ini'}
        if not mysqld.exists():
            return {'success': False, 'error': f'mysqld.exe 不存在: {mysqld}'}
        try:
            pid = launch_mysqld(str(mysqld), str(ini))
            ready = wait_port_ready(host, port, START_READY_TIMEOUT)
            if ready:
                return {'success': True, 'message': f'MySQL 进程已启动 (PID {pid})'}
            return {'success': True, 'message': f'MySQL 进程已拉起 (PID {pid})，等待端口就绪超时，请手动刷新确认'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    return {'success': False, 'error': f'未知 run_mode: {run_mode}'}


def check_db_exists(instance_id: str = '', db_name: str = '',
                    user: str = '', password: str = '') -> dict:
    """检查 db_name 是否已存在于目标实例上（前端即时校验用）。"""
    if not instance_id or not db_name:
        return {'success': False, 'error': '参数不完整'}
    try:
        conn = _get_conn(instance_id, user, password)
        try:
            dbs = _list_dbs(conn)
            existing = set()
            for d in dbs:
                if isinstance(d, dict):
                    nm = d.get('name', '')
                else:
                    nm = str(d)
                if nm:
                    existing.add(nm.lower())
            return {'success': True, 'exists': db_name.lower() in existing,
                    'db_name': db_name}
        finally:
            conn.close()
    except MysqlError as e:
        return {'success': False, 'error': str(e)}
    except Exception as e:
        return {'success': False, 'error': str(e)}