"""
MySQL Windows 服务注册与删除（UAC 提权）。

将 mysqld --install / --remove 的注册逻辑封装在本模块内，
权限不足时自动通过 jiecore.system_ops.run_elevated 申请管理员权限重试。
"""
import os
import re
import logging
import subprocess
from pathlib import Path

from django.conf import settings

from jiecore.system_ops import query_service_state, run_elevated
from jiecore.utils import get_cmd_encoding

logger = logging.getLogger(__name__)

# 服务名格式校验（字母开头，仅含字母/数字/下划线/中划线）
_SERVICE_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_\-]{0,31}$')

# 子进程超时（秒）
_CMD_TIMEOUT = 60

# 子进程隐藏窗口标志
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def register_mysql_service(mysqld_path: str, service_name: str,
                           ini_path: str) -> dict:
    """
    注册 MySQL 为 Windows 服务（mysqld --install），权限不足时自动申请 UAC 提权。

    执行链路：
      1. 以当前权限直接执行 mysqld --install；
      2. 用 sc query 验证服务是否真正注册成功（mysqld 退出码不可靠）；
      3. 未注册成功时，通过 run_elevated 提权重试（cmd.exe 重定向输出到临时文件）；
      4. 再次用 sc query 确认。

    返回 dict: {success: bool, message: str}
    """
    if not service_name or not _SERVICE_NAME_RE.match(service_name):
        return {'success': False,
                'message': '服务名必须以字母开头，仅含字母/数字/下划线/中划线'}

    mysqld = Path(mysqld_path)
    if not mysqld.exists():
        return {'success': False, 'message': f'mysqld.exe 不存在: {mysqld}'}

    # 先检查服务是否已注册
    if query_service_state(service_name)['exists']:
        return {'success': False, 'message': f'服务名已存在: {service_name}'}

    # 第一步：以当前权限尝试注册
    try:
        proc = subprocess.run(
            [str(mysqld), '--install', service_name,
             f'--defaults-file={ini_path}'],
            capture_output=True, timeout=_CMD_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW)
        out = ((proc.stdout or b'') + b'\n' + (proc.stderr or b'')).decode(
            get_cmd_encoding(), errors='replace')
    except subprocess.TimeoutExpired:
        return {'success': False, 'message': '注册服务超时'}
    except Exception as e:
        return {'success': False, 'message': f'执行 mysqld --install 失败: {e}'}

    # 用 sc query 验证服务是否真正注册成功（mysqld 退出码不可靠）
    if query_service_state(service_name)['exists']:
        return {'success': True,
                'message': f'服务 {service_name} 注册成功'}

    # 第二步：权限不足或其他原因失败，通过 UAC 提权重试
    # 将命令写入临时 bat 文件，避免 cmd.exe /c 多层引号嵌套导致路径截断
    logger.info('mysqld --install 以当前权限注册失败，申请 UAC 提权重试')
    out_file = settings.TMP_DIR / f'mysqld_install_{os.getpid()}.log'
    bat_file = settings.TMP_DIR / f'mysqld_install_{os.getpid()}.bat'
    try:
        bat_file.write_text(
            f'@echo off\n"{mysqld}" --install {service_name} '
            f'"--defaults-file={ini_path}" > "{out_file}" 2>&1\n',
            encoding='gbk')
    except OSError as e:
        return {'success': False, 'message': f'无法写入临时批处理: {e}'}
    try:
        run_elevated('cmd.exe', f'/c "{bat_file}"', _CMD_TIMEOUT)
    except PermissionError as e:
        return {'success': False, 'message': str(e)}
    except Exception as e:
        logger.exception('提权注册 MySQL 服务失败')
        return {'success': False, 'message': f'以管理员权限执行失败: {e}'}
    finally:
        try:
            os.remove(bat_file)
        except OSError:
            pass

    # 读取重定向的输出
    out = ''
    try:
        out = out_file.read_text(encoding=get_cmd_encoding(), errors='replace').strip()
    except OSError:
        pass
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass

    # 再次用 sc query 确认
    if query_service_state(service_name)['exists']:
        return {'success': True,
                'message': f'服务 {service_name} 注册成功（管理员权限）'}
    return {'success': False,
            'message': f'注册服务失败: {out[:200] or "无错误输出"}'}


def remove_mysql_service(mysqld_path: str, service_name: str) -> dict:
    """
    删除 MySQL Windows 服务（mysqld --remove），权限不足时自动申请 UAC 提权。

    执行链路：
      1. 检查服务是否存在，不存在则直接返回成功；
      2. 以当前权限执行 mysqld --remove；
      3. 用 sc query 验证服务是否已删除；
      4. 未删除时尝试 sc delete 兜底；
      5. 仍未删除则通过 run_elevated 提权重试。

    返回 dict: {success: bool, message: str}
    """
    if not service_name:
        return {'success': False, 'message': '未指定服务名'}

    # 服务不存在，视为已删除
    if not query_service_state(service_name)['exists']:
        return {'success': True, 'message': f'服务 {service_name} 不存在，无需删除'}

    mysqld = Path(mysqld_path)
    if not mysqld.exists():
        # mysqld 不存在但服务存在，用 sc delete 兜底
        return _sc_delete_service(service_name)

    # 第一步：以当前权限尝试删除
    try:
        proc = subprocess.run(
            [str(mysqld), '--remove', service_name],
            capture_output=True, timeout=_CMD_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {'success': False, 'message': '删除服务超时'}
    except Exception as e:
        return {'success': False, 'message': f'执行 mysqld --remove 失败: {e}'}

    # 验证服务是否已删除
    if not query_service_state(service_name)['exists']:
        return {'success': True, 'message': f'服务 {service_name} 已删除'}

    # mysqld --remove 失败，尝试 sc delete 兜底
    logger.info('mysqld --remove 未成功删除服务，尝试 sc delete')
    sc_result = _sc_delete_service(service_name)
    if sc_result['success']:
        return sc_result

    # 第二步：提权删除
    logger.info('以当前权限删除服务失败，申请 UAC 提权重试')
    bat_file = settings.TMP_DIR / f'mysqld_remove_{os.getpid()}.bat'
    out_file = settings.TMP_DIR / f'mysqld_remove_{os.getpid()}.log'
    try:
        bat_file.write_text(
            f'@echo off\nsc delete {service_name} > "{out_file}" 2>&1\n',
            encoding='gbk')
    except OSError as e:
        return {'success': False, 'message': f'无法写入临时批处理: {e}'}
    try:
        run_elevated('cmd.exe', f'/c "{bat_file}"', _CMD_TIMEOUT)
    except PermissionError as e:
        return {'success': False, 'message': str(e)}
    except Exception as e:
        logger.exception('提权删除 MySQL 服务失败')
        return {'success': False, 'message': f'以管理员权限执行失败: {e}'}
    finally:
        try:
            os.remove(bat_file)
        except OSError:
            pass

    # 读取输出
    out = ''
    try:
        out = out_file.read_text(encoding=get_cmd_encoding(), errors='replace').strip()
    except OSError:
        pass
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass

    if not query_service_state(service_name)['exists']:
        return {'success': True,
                'message': f'服务 {service_name} 已删除（管理员权限）'}
    return {'success': False,
            'message': f'删除服务失败: {out[:200] or "无错误输出"}'}


def _sc_delete_service(service_name: str) -> dict:
    """使用 sc delete 删除服务（当前权限），返回 dict: {success, message}"""
    try:
        proc = subprocess.run(
            ['sc', 'delete', service_name],
            capture_output=True, timeout=_CMD_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW)
        if not query_service_state(service_name)['exists']:
            return {'success': True,
                    'message': f'服务 {service_name} 已删除（sc delete）'}
    except Exception:
        pass
    return {'success': False, 'message': f'sc delete 未能删除服务 {service_name}'}
