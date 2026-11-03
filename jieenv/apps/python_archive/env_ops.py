"""
python_archive 系统环境操作（Windows）

为已安装列表提供系统级操作能力（简化版，无 py_launcher 和注册表卸载）：

- get_path_default_dir: 有效 PATH 中最先命中 python.exe 的目录
- add_to_path: 清除旧 Python PATH 条目后将安装目录加入用户 PATH
- remove_from_path: 从 PATH 中移除指定版本的安装目录
- open_terminal: 以指定目录为工作目录打开 cmd 命令行窗口
- annotate_installed: 为已安装列表记录填充 is_path_default 标记

对外接口::

    from apps.python_archive import env_ops
"""
import os
import winreg
import logging
import subprocess
from pathlib import Path

from jiecore.regutil import RegError, read_path, write_path
from jiecore.system_ops import (broadcast_env_change, get_path_entries,
                                set_system_path, remove_from_path as _remove_from_path, split_path)
from . import storage

logger = logging.getLogger(__name__)

# 子进程隐藏窗口标志
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
# 为新开的 cmd 窗口分配独立控制台
_CREATE_NEW_CONSOLE = 0x00000010 if os.name == 'nt' else 0

# 注册表 PATH 键
_SYS_ENV_KEY = (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')
_USR_ENV_KEY = (winreg.HKEY_CURRENT_USER, 'Environment')


def _ver_key(version: str) -> tuple:
    """版本号排序键，非数字段按 0 处理"""
    parts = (version or '').split('.')
    nums = []
    for p in parts[:3]:
        nums.append(int(p) if p.isdigit() else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


# ── PATH 环境变量 ──────────────────────────────────

def _has_python_exe(entry: str) -> bool:
    """检查目录下是否存在 python.exe 或 python*t.exe（free-threaded 版本）。"""
    if not entry:
        return False
    try:
        # 检查标准 python.exe
        if os.path.isfile(os.path.join(entry, 'python.exe')):
            return True
        # 检查 free-threaded 版本：python*t.exe
        entry_path = Path(entry)
        if entry_path.is_dir():
            for exe in entry_path.glob('python*t.exe'):
                if exe.is_file():
                    return True
    except OSError:
        pass
    return False


def get_path_default_dir() -> str:
    """有效 PATH 中最先命中 python.exe 或 python*t.exe 的目录（normcase 绝对路径），无则返回空串。"""
    for entry in get_path_entries():
        if _has_python_exe(entry):
            try:
                return os.path.normcase(os.path.abspath(entry))
            except OSError:
                continue
    return ''


def _is_real_python_dir(entry: str) -> bool:
    """目录下是否存在真实 python.exe 或 python*t.exe（排除 WindowsApps 等 0 字节重解析点别名）。"""
    if not entry:
        return False

    candidates = [os.path.join(entry, 'python.exe')]
    if os.path.basename(entry.rstrip('\\/')).lower() == 'scripts':
        candidates.append(os.path.join(os.path.dirname(entry), 'python.exe'))

    for p in candidates:
        try:
            if (os.path.isfile(p) and not os.path.islink(p) and os.path.getsize(p) > 0):
                return True
        except OSError:
            continue

    # 检查 free-threaded 版本：python*t.exe
    try:
        entry_path = Path(entry)
        if entry_path.is_dir():
            for exe in entry_path.glob('python*t.exe'):
                if exe.is_file() and not exe.is_symlink() and exe.stat().st_size > 0:
                    return True
    except OSError:
        pass

    return False


def _clean_python_dirs(entries: list) -> tuple:
    """按"路径下有真实 python.exe"准则拆为 (保留条目, 被清除的目录)。"""
    keep, removed = [], []
    for part in entries:
        expanded = os.path.expandvars(part).strip()
        if expanded and _is_real_python_dir(expanded):
            removed.append(expanded)
        else:
            keep.append(part)
    return keep, removed


def add_to_path(install_dir: str) -> dict:
    """将安装目录及其 Scripts 前置写入用户级 PATH，使其成为 PATH 默认。

    写入前先清除用户与系统 PATH 中所有含真实 python.exe 的条目（系统部分
    经 UAC 提权写 HKLM，用户拒绝提权或提权失败则中止、不做任何改动）。

    返回字段：
        success:    整体是否成功
        message:    人类可读的结果说明
        changed:    本次调用是否真的修改了某个 PATH
        is_default: 修改后 install_dir 是否是 PATH 中第一个命中的 python.exe
    """
    if os.name != 'nt':
        return {'success': False, 'message': '仅支持 Windows 系统'}

    install_dir = (install_dir or '').strip('"').rstrip("\\").rstrip("/")
    if not install_dir or not Path(install_dir).is_dir():
        return {'success': False, 'message': f'安装目录不存在: {install_dir}'}

    # 1) 读取系统 / 用户 PATH
    try:
        sys_raw, _ = read_path(*_SYS_ENV_KEY)
    except RegError as e:
        return {'success': False, 'message': f'读取系统 PATH 失败: {e}'}

    try:
        usr_raw, vtype = read_path(*_USR_ENV_KEY)
    except RegError as e:
        return {'success': False, 'message': f'读取用户 PATH 失败: {e}'}

    # 2) 清洗：剔除所有"真实 Python 目录"
    sys_keep, sys_removed = _clean_python_dirs(split_path(sys_raw))
    usr_keep, usr_removed = _clean_python_dirs(split_path(usr_raw))

    # 3) 构造新的用户 PATH
    candidates = [install_dir, str(Path(install_dir) / 'Scripts')]
    taken = set()
    for c in candidates:
        try:
            taken.add(os.path.normcase(os.path.abspath(c)))
        except OSError:
            continue

    kept = []
    for part in usr_keep:
        try:
            norm = os.path.normcase(os.path.abspath(os.path.expandvars(part)))
        except OSError:
            kept.append(part)
            continue
        if norm not in taken:
            taken.add(norm)
            kept.append(part)
    new_user = ';'.join(candidates + kept)

    # 4) 系统 PATH 有清除 → 先 UAC 提权写 HKLM
    sys_changed = False
    if sys_removed:
        result = set_system_path(';'.join(sys_keep))
        if not result.get('success'):
            return result
        sys_changed = True
        logger.info('系统 PATH 已清除 Python 路径: %s', ', '.join(sys_removed))

    # 5) 写用户 PATH
    try:
        usr_changed = write_path(*_USR_ENV_KEY, new_user, vtype=vtype)
    except RegError as e:
        if sys_changed:
            rollback = set_system_path(sys_raw)
            if rollback.get('success'):
                logger.warning('用户 PATH 写入失败，已回滚系统 PATH')
        return {'success': False, 'message': f'写入用户 PATH 失败: {e}'}

    if usr_removed:
        logger.info('用户 PATH 已清除 Python 路径: %s', ', '.join(usr_removed))

    changed = sys_changed or usr_changed

    # 6) 通知其他进程
    if changed:
        broadcast_env_change()

    # 7) 同步当前进程 PATH
    eff = ([os.path.expandvars(p).strip() for p in sys_keep]
           + [os.path.expandvars(p).strip() for p in split_path(new_user)])
    os.environ['PATH'] = os.pathsep.join(p for p in eff if p)

    # 8) 组装返回
    is_default = (get_path_default_dir()
                  == os.path.normcase(os.path.abspath(install_dir)))

    removed_count = len(sys_removed) + len(usr_removed)
    if not changed:
        message = 'PATH 无需修改'
    else:
        message = '已加入环境变量'
        if removed_count:
            message += f'，已清除 {removed_count} 个旧 Python 路径'
    if not is_default:
        message += '；尚非 PATH 默认'

    return {
        'success': True,
        'changed': changed,
        'is_default': is_default,
        'message': message,
    }


def remove_from_path(install_dir: str) -> dict:
    """从系统/用户 PATH 中移除指定安装目录及其 Scripts 子目录。

    用于卸载时清理环境变量。
    """
    if os.name != 'nt':
        return {'success': False, 'message': '仅支持 Windows 系统'}

    install_dir = (install_dir or '').strip()
    if not install_dir:
        return {'success': False, 'message': '未提供安装目录'}

    # 读取 PATH
    try:
        sys_raw, _ = read_path(*_SYS_ENV_KEY)
    except RegError as e:
        return {'success': False, 'message': f'读取系统 PATH 失败: {e}'}

    try:
        usr_raw, vtype = read_path(*_USR_ENV_KEY)
    except RegError as e:
        return {'success': False, 'message': f'读取用户 PATH 失败: {e}'}

    # 要移除的路径
    remove_dirs = [install_dir, os.path.join(install_dir, 'Scripts')]

    # 从 PATH 中移除
    new_sys, sys_removed = _remove_from_path(sys_raw, remove_dirs)
    new_usr, usr_removed = _remove_from_path(usr_raw, remove_dirs)

    # 系统 PATH 有变化 → UAC 提权写 HKLM
    sys_changed = False
    if sys_removed:
        result = set_system_path(new_sys)
        if not result.get('success'):
            logger.warning('系统 PATH 清理失败: %s', result.get('message'))
        else:
            sys_changed = True
            logger.info('系统 PATH 已删除 Python 路径: %s', ', '.join(sys_removed))

    # 用户 PATH 有变化 → 写 HKCU
    usr_changed = False
    if usr_removed:
        try:
            write_path(*_USR_ENV_KEY, new_usr, vtype=vtype)
            usr_changed = True
            logger.info('用户 PATH 已删除 Python 路径: %s', ', '.join(usr_removed))
        except RegError as e:
            logger.warning('用户 PATH 清理失败: %s', e)

    # 广播 + 同步本进程 PATH
    if sys_changed or usr_changed:
        broadcast_env_change()
        eff = ([os.path.expandvars(p).strip() for p in new_sys.split(';') if p.strip()]
               + [os.path.expandvars(p).strip() for p in new_usr.split(';') if p.strip()])
        os.environ['PATH'] = os.pathsep.join(p for p in eff if p)

    removed_count = len(sys_removed) + len(usr_removed)
    if removed_count:
        return {'success': True, 'message': f'已从 PATH 移除 {removed_count} 个路径'}
    return {'success': True, 'message': 'PATH 中无需清理的路径'}


def refresh_process_path() -> None:
    """从注册表重新读取系统+用户 PATH，同步更新当前进程的 os.environ['PATH']。"""
    if os.name != 'nt':
        return
    try:
        entries = get_path_entries()
        if entries:
            os.environ['PATH'] = os.pathsep.join(entries)
            logger.debug('已从注册表刷新进程 PATH（%d 条目）', len(entries))
    except Exception as e:
        logger.warning('刷新进程 PATH 失败: %s', e)


# ── 终端 ─────────────────────────────────────────

from jiecore.system_ops import open_terminal


# ── 列表标记 ─────────────────────────────────────

def annotate_installed(records: list) -> list:
    """为已安装列表记录填充标记并按版本倒序返回。

    - is_path_default: 是否为有效 PATH 中最先命中的 python
    - exe_dir: 解释器所在目录
    """
    records = sorted(records,
                     key=lambda r: _ver_key(r.get('version', '')), reverse=True)
    path_dir = get_path_default_dir()
    for r in records:
        exe = r.get('python_exe', '') or ''
        exe_dir = os.path.normcase(os.path.abspath(os.path.dirname(exe))) if exe else ''
        r['exe_dir'] = os.path.dirname(exe) if exe else ''
        r['is_path_default'] = bool(path_dir) and bool(exe_dir) and exe_dir == path_dir
    return records