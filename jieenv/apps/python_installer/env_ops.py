"""
python_installer 系统环境操作（Windows）

为已安装列表提供系统级操作能力：

- get_py_default_version: 探测 py 启动器当前默认版本（裸跑 py 解析的版本）
- py_launcher_installed / list_py_registered: py 启动器是否安装、已注册的版本标签
- set_py_default: 检测版本是否已被 py 注册，未注册则自动写入 HKCU 注册表注册，再写用户级 %LOCALAPPDATA%\\py.ini
- get_path_default_dir: 有效 PATH（系统+用户，读注册表）中最先命中 python.exe 的目录
- add_to_path: 先清除用户+系统 PATH 中含真实 python.exe 的条目，再将安装目录
  及 Scripts 前置写入用户级 PATH（系统部分需 UAC 提权）并广播环境变更
- find_uninstall_command: 从注册表 Uninstall 键查找指定版本的官方卸载命令
- run_uninstall_interactive: 启动官方卸载界面并等待结束，按选项清理安装目录后移除记录
- open_terminal: 以指定目录为工作目录打开 cmd 命令行窗口
- annotate_installed: 为已安装列表记录填充 is_py_default / is_path_default 标记

对外接口::

    from apps.python_installer import env_ops
"""
import os
import re
import winreg
import shutil
import logging
import subprocess
import configparser
from pathlib import Path

from jiecore.regutil import RegError, read_path, write_path, write_value
from jiecore.system_ops import (broadcast_env_change, get_path_entries,
                                set_system_path, remove_from_path, split_path)
from . import storage
from .detect import _probe_version

logger = logging.getLogger(__name__)

# 子进程隐藏窗口标志
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
# 为新开的 cmd 窗口分配独立控制台
_CREATE_NEW_CONSOLE = 0x00000010 if os.name == 'nt' else 0

# py 探测超时（秒）
_PROBE_TIMEOUT = 8
# 卸载程序等待超时（秒）
_UNINSTALL_TIMEOUT = 600

_VERSION_RE = re.compile(r'\d+\.\d+\.\d+')

# 注册表卸载信息键（含 32 位视图）
_UNINSTALL_SUBKEYS = [
    r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
    r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
]

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


# ── py 启动器默认版本 ──────────────────────────────

# py -0p 输出行形如 " -V:3.12   D:\...\python.exe"、" -V:3.13-64  D:\..."，
# 默认版本行带 * 标记：" -V:3.12 *        D:\...\python.exe"
_PY_TAG_RE = re.compile(r'^-V:(\d+\.\d+)(?:-\d+)?\s+(?:\*\s*)?(\S.*\S)\s*$')


def py_launcher_installed() -> bool:
    """系统是否安装 py 启动器（py.exe）。"""
    from .detect import find_python_launcher_from_registry
    return find_python_launcher_from_registry()


def list_py_registered() -> dict:
    """解析 py -0p 输出，返回 {版本标签(major.minor): 解释器路径}。

    py 未安装或命令失败时返回空 dict。
    """
    registered, _ = _parse_py0p_output()
    return registered


def _parse_py0p_output() -> tuple[dict, str]:
    """解析 py -0p 输出，返回 (registered_dict, default_tag)。

    default_tag 是带 * 标记的默认版本标签（major.minor），无默认时为空串。
    py 未安装或命令失败时返回 ({}, '')。
    """
    if not py_launcher_installed():
        return {}, ''
    try:
        proc = subprocess.run(['py', '-0p'], capture_output=True,
                              timeout=_PROBE_TIMEOUT,
                              creationflags=_CREATE_NO_WINDOW)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}, ''
    if proc.returncode != 0:
        return {}, ''
    out = (proc.stdout or b'').decode('utf-8', errors='replace')
    registered = {}
    default_tag = ''
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('Installed'):
            continue
        # 检测 * 标记：默认版本行形如 " -V:3.13 *        D:\..."
        has_star = '*' in line
        m = _PY_TAG_RE.match(line)
        if m:
            tag, path = m.group(1), m.group(2)
            registered.setdefault(tag, path)
            if has_star and not default_tag:
                default_tag = tag
    return registered, default_tag


def get_py_default_tag_fast() -> str:
    """快速获取 py 启动器默认版本标签（major.minor），单次 subprocess 调用。

    用于首页等只需知道"哪个是默认"而不需要完整版本号的场景。
    py 缺失或无注册版本时返回空串。
    """
    _, default_tag = _parse_py0p_output()
    return default_tag


def get_py_default_version() -> str:
    """获取 py 启动器当前默认版本号（x.y.z），py 缺失或无注册版本返回空串。

    优先裸跑 py 解析版本；裸跑失败（如 py.ini 被手工指向未注册版本）时
    回退取 py 已注册的最高版本标签并探测其解释器精确版本。
    """
    registered = list_py_registered()
    if not registered:
        return ''
    try:
        proc = subprocess.run(
            ['py', '-c', 'import sys;print("%d.%d.%d"%sys.version_info[:3])'],
            capture_output=True, timeout=_PROBE_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW)
        if proc.returncode == 0:
            out = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
            first = out.splitlines()[0].strip() if out else ''
            if _VERSION_RE.fullmatch(first):
                return first
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    tag = max(registered, key=lambda t: tuple(int(x) for x in t.split('.')))
    return _probe_version(registered[tag])


def _register_py_version(tag: str, install_dir: str) -> dict:
    """将 Python 版本注册到 py launcher（写 HKCU 注册表 PythonCore）。

    py 启动器从 Software\\Python\\PythonCore\\<tag>\\InstallPath 发现可用版本，
    写入 HKCU 无需管理员权限。使用 KEY_WOW64_64KEY 确保 32 位进程也能写入
    64 位注册表视图，使 64 位 py launcher 能发现注册的版本。
    """
    if os.name != 'nt':
        return {'success': False, 'message': '仅支持 Windows 系统'}
    try:
        import winreg
        reg_path = rf'SOFTWARE\Python\PythonCore\{tag}\InstallPath'
        # KEY_WOW64_64KEY: 强制写入 64 位注册表视图，避免 32 位进程被重定向到 WOW6432Node
        access = winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_path, 0, access)
        try:
            winreg.SetValueEx(key, '', 0, winreg.REG_SZ, install_dir)
            exe_path = os.path.join(install_dir, 'python.exe')
            if os.path.isfile(exe_path):
                winreg.SetValueEx(key, 'ExecutablePath', 0, winreg.REG_SZ, exe_path)
        finally:
            winreg.CloseKey(key)
        logger.info('已将 Python %s 注册到 py launcher: %s -> %s',
                    tag, 'HKCU\\' + reg_path, install_dir)
        return {'success': True, 'message': f'已注册 Python {tag} 到 py launcher'}
    except Exception as e:
        logger.warning('注册 Python %s 到 py launcher 失败: %s', tag, e)
        return {'success': False, 'message': f'注册到 py launcher 失败: {e}'}


def set_py_default(version: str) -> dict:
    """设置 py 启动器默认版本：写用户级 %LOCALAPPDATA%\\py.ini 的 [defaults] python。

    写入前检查版本是否已被 py 注册，未注册则自动将该 Python 注册到注册表
    （HKCU PythonCore），使 py 启动器能发现该版本后再写入 py.ini；
    保留 py.ini 中已有的其他配置项。
    """
    if not py_launcher_installed():
        return {'success': False, 'message': '未检测到 py 启动器'}
    local = os.environ.get('LOCALAPPDATA', '')
    if not local:
        return {'success': False, 'message': '未定位到 %LOCALAPPDATA%，无法写入 py.ini'}
    ini = Path(local) / 'py.ini'
    # ---------- 分支一：version 为空 -> 删除 python 项 ----------
    if not version or not version.strip():
        if not ini.exists():
            return {'success': True, 'message': 'py.ini 不存在，无需删除'}
        cp = configparser.ConfigParser()
        try:
            with open(ini, 'r', encoding='utf-8', errors='replace') as f:
                cp.read_file(f)
        except configparser.Error as e:
            return {'success': False, 'message': f'现有 py.ini 解析失败，未覆盖: {e}'}

        if not (cp.has_section('defaults') and cp.has_option('defaults', 'python')):
            return {'success': True, 'message': 'py.ini 中本无 python 设置，无需删除'}

        cp.remove_option('defaults', 'python')
        if not cp.options('defaults') and not cp.items('defaults'):
            cp.remove_section('defaults')
        try:
            with open(ini, 'w', encoding='utf-8') as f:
                cp.write(f)
        except OSError as e:
            return {'success': False, 'message': f'写入 py.ini 失败: {e}'}
        logger.info('已删除 py 默认版本设置: %s', ini)
        return {'success': True, 'message': '已删除 py 默认版本设置'}

    # ---------- 分支二：version 非空 -> 校验/自动注册并写入 ----------
    tag = '.'.join(version.split('.')[:2])
    registered = list_py_registered()
    # py launcher 对 64 位版本可能使用带架构后缀的标签（如 "3.13-64"），
    # 需检查后缀变体以正确匹配已注册版本
    ini_tag = tag  # 写入 py.ini 的标签，优先使用 py launcher 已识别的标签
    if tag not in registered:
        # 检查带架构后缀的标签是否已注册（如 "3.13-64"）
        arch_tag = None
        for suffix in ('-64', '-32'):
            candidate = tag + suffix
            if candidate in registered:
                arch_tag = candidate
                break
        if arch_tag:
            # py launcher 已用后缀标签注册，直接使用该标签
            ini_tag = arch_tag
        else:
            # 确实未注册：自动注册到 py launcher
            install_dir = ''
            record = storage.get_installed(version)
            if record:
                install_dir = record.get('install_dir', '') or os.path.dirname(record.get('python_exe', ''))
            if not install_dir:
                # 回退：从注册表 PythonCore 查找匹配版本的安装目录
                try:
                    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                        for sub in (r'SOFTWARE\Python\PythonCore',
                                    r'SOFTWARE\WOW6432Node\Python\PythonCore'):
                            try:
                                core = winreg.OpenKey(hive, sub)
                            except OSError:
                                continue
                            try:
                                idx = 0
                                while True:
                                    try:
                                        name = winreg.EnumKey(core, idx)
                                    except OSError:
                                        break
                                    idx += 1
                                    if name.split('-')[0] == tag:
                                        try:
                                            with winreg.OpenKey(core, name + r'\InstallPath') as ip:
                                                install_dir, _ = winreg.QueryValueEx(ip, '')
                                        except OSError:
                                            pass
                                        if install_dir:
                                            break
                            finally:
                                winreg.CloseKey(core)
                            if install_dir:
                                break
                        if install_dir:
                            break
                except Exception as e:
                    logger.debug('从注册表查找 Python %s 安装目录失败: %s', version, e)
            if not install_dir:
                return {'success': False,
                        'message': f'未找到 Python {version} 的安装目录，无法注册到 py launcher'}
            reg_result = _register_py_version(tag, install_dir)
            if not reg_result.get('success'):
                return reg_result
            logger.info('Python %s 未注册到 py launcher，已自动注册: %s', version, install_dir)

    cp = configparser.ConfigParser()
    if ini.exists():
        try:
            with open(ini, 'r', encoding='utf-8', errors='replace') as f:
                cp.read_file(f)
        except configparser.Error as e:
            return {'success': False, 'message': f'现有 py.ini 解析失败，未覆盖: {e}'}
    if not cp.has_section('defaults'):
        cp.add_section('defaults')
    cp.set('defaults', 'python', ini_tag)
    try:
        with open(ini, 'w', encoding='utf-8') as f:
            cp.write(f)
    except OSError as e:
        return {'success': False, 'message': f'写入 py.ini 失败: {e}'}
    logger.info('已设置 py 默认版本: %s -> %s', ini, ini_tag)
    return {'success': True, 'message': '已设置为py默认'}


# ── PATH 环境变量 ──────────────────────────────────

def get_path_default_dir() -> str:
    """有效 PATH 中最先命中 python.exe 的目录（normcase 绝对路径），无则返回空串。"""
    for entry in get_path_entries():
        try:
            if os.path.isfile(os.path.join(entry, 'python.exe')):
                return os.path.normcase(os.path.abspath(entry))
        except OSError:
            continue
    return ''


def _is_real_python_dir(entry: str) -> bool:
    """目录下是否存在真实 python.exe（排除 WindowsApps 等 0 字节重解析点别名）。"""
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
    return False


def _clean_python_dirs(entries: list) -> tuple:
    """按“路径下有真实 python.exe”准则拆为 (保留条目, 被清除的目录)。"""
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
    经 UAC 提权写 HKLM，用户拒绝提权或提权失败则中止、不做任何改动）；
    WindowsApps 等 Store 别名目录（0 字节 python.exe）不在清除之列。

    返回字段：
        success:    整体是否成功
        message:    人类可读的结果说明
        changed:    本次调用是否真的修改了某个 PATH（注册表层面）
        is_default: 修改后 install_dir 是否是 PATH 中第一个命中的 python.exe
    """
    if os.name != 'nt':
        return {'success': False, 'message': '仅支持 Windows 系统'}

    install_dir = (install_dir or '').strip('"').rstrip("\\").rstrip("/")
    if not install_dir or not Path(install_dir).is_dir():
        return {'success': False, 'message': f'安装目录不存在: {install_dir}'}

    # ---- 1) 读取系统 / 用户 PATH（读失败会抛 RegError） ----
    try:
        sys_raw, _ = read_path(*_SYS_ENV_KEY)
    except RegError as e:
        return {'success': False, 'message': f'读取系统 PATH 失败: {e}'}

    try:
        usr_raw, vtype = read_path(*_USR_ENV_KEY)
    except RegError as e:
        return {'success': False, 'message': f'读取用户 PATH 失败: {e}'}

    # ---- 2) 清洗：剔除所有“真实 Python 目录” ----
    sys_keep, sys_removed = _clean_python_dirs(split_path(sys_raw))
    usr_keep, usr_removed = _clean_python_dirs(split_path(usr_raw))

    # ---- 3) 构造新的用户 PATH ----
    # 安装目录及其 Scripts 前置；其余条目按规范化路径去重后跟随
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

    # ---- 4) 系统 PATH 有清除 → 先 UAC 提权写 HKLM ----
    #    失败即中止，用户 PATH 一个字节都不动，避免“清一半”的状态
    sys_changed = False
    if sys_removed:
        result = set_system_path(';'.join(sys_keep))
        if not result.get('success'):
            return result
        sys_changed = True
        logger.info('系统 PATH 已清除 Python 路径: %s', ', '.join(sys_removed))

    # ---- 5) 写用户 PATH；失败则尝试回滚系统 PATH ----
    try:
        usr_changed = write_path(*_USR_ENV_KEY, new_user, vtype=vtype)
    except RegError as e:
        if sys_changed:
            rollback = set_system_path(sys_raw)
            if rollback.get('success'):
                logger.warning('用户 PATH 写入失败，已回滚系统 PATH')
            else:
                logger.error(
                    '用户 PATH 写入失败且系统 PATH 回滚失败，需人工修复: %s',
                    rollback.get('message'))
        return {'success': False, 'message': f'写入用户 PATH 失败: {e}'}

    if usr_removed:
        logger.info('用户 PATH 已清除 Python 路径: %s', ', '.join(usr_removed))

    changed = sys_changed or usr_changed

    # ---- 6) 通知其他进程；仅在真的发生变化时广播 ----
    if changed:
        broadcast_env_change()

    # ---- 7) 同步当前进程 PATH，使本进程后续启动的子进程立即生效 ----
    #    广播管不到本进程（窗口消息 + 环境块副本语义），必须自己改 os.environ
    eff = ([os.path.expandvars(p).strip() for p in sys_keep]
           + [os.path.expandvars(p).strip() for p in split_path(new_user)])
    os.environ['PATH'] = os.pathsep.join(p for p in eff if p)

    # ---- 8) 组装返回 ----
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
        message += '；尚非 PATH 默认（存在更先命中的 python.exe）'

    return {
        'success': True,
        'changed': changed,
        'is_default': is_default,
        'message': message,
    }


# ── 卸载程序 ──────────────────────────────────────

def _query_str(key, name: str) -> str:
    """读注册表键下字符串值，缺失返回空串"""
    import winreg
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return value or ''
    except OSError:
        return ''


def find_uninstall_command(version: str) -> str:
    """从注册表 Uninstall 键查找指定版本的官方卸载命令（带界面），未找到返回空串。

    官方安装器会登记一个捆绑卸载入口（DisplayName 恰为 "Python <version> (64-bit)"，
    UninstallString 为 bundle exe + /uninstall）与若干 MSI 组件入口
    （DisplayName 为 "Python <version> Core Interpreter (64-bit)" 等，
    UninstallString 为 MsiExec /I{GUID}）。组件入口只能维护单个组件，
    故优先命中捆绑入口；仅当无捆绑入口时才回退到非 MsiExec 的其他卸载命令。
    """
    if os.name != 'nt':
        return ''
    import winreg
    bundle_re = re.compile(r'^Python %s \(\d+-bit\)$' % re.escape(version))
    fallback = ''
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in _UNINSTALL_SUBKEYS:
            try:
                root = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            with root:
                idx = 0
                while True:
                    try:
                        name = winreg.EnumKey(root, idx)
                    except OSError:
                        break
                    idx += 1
                    try:
                        with winreg.OpenKey(root, name) as k:
                            display_name = _query_str(k, 'DisplayName').strip()
                            cmd = _query_str(k, 'UninstallString').strip()
                    except OSError:
                        continue
                    if not cmd:
                        continue
                    if bundle_re.match(display_name):
                        logger.debug('命中捆绑卸载命令 %s: %s', version, cmd)
                        return cmd
                    is_msi = cmd.lower().startswith('msiexec')
                    if not fallback and not is_msi and \
                            (display_name.startswith(f'Python {version}')
                             or f'Python {version}' in display_name):
                        fallback = cmd
    if fallback:
        logger.debug('回退卸载命令 %s: %s', version, fallback)
    return fallback


def run_uninstall_interactive(version: str, remove_leftovers: bool = False,
                              on_progress=None) -> dict:
    """启动官方卸载程序（带界面）并阻塞等待结束。

    卸载程序退出码为 0 才视为卸载成功：按选项删除安装目录遗留文件，
    并移除本应用记录与 python 应用环境记录；退出码非 0（如用户取消）
    或超时则不做任何清理，保留记录。
    """
    record = storage.get_installed(version)
    if not record:
        return {'success': False, 'message': f'未找到 Python {version} 的安装记录'}

    cmd = find_uninstall_command(version) or record.get('uninstall_exe', '')
    if not cmd:
        return {'success': False,
                'message': f'未找到卸载程序：注册表与安装记录中均无 Python {version} 的卸载命令'}

    if on_progress:
        on_progress(f'启动卸载程序: {cmd}')
    try:
        proc = subprocess.Popen(cmd)
    except OSError as e:
        return {'success': False, 'message': f'启动卸载程序失败: {e}'}
    try:
        rc = proc.wait(timeout=_UNINSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {'success': False,
                'message': f'卸载超时（超过 {_UNINSTALL_TIMEOUT // 60} 分钟），记录与安装目录未改动'}
    if rc != 0:
        return {'success': False,
                'message': f'卸载程序返回退出码 {rc}（可能已取消卸载），记录与安装目录未改动'}

    if on_progress:
        on_progress('探测并清理py默认设置')
    get_default_py =  get_py_default_version()
    if get_default_py == version:
        set_py_default('')

    install_dir = record.get('install_dir', '')
    if remove_leftovers and install_dir and Path(install_dir).exists():
        if on_progress:
            on_progress(f'删除安装目录遗留文件: {install_dir}')
        shutil.rmtree(install_dir, ignore_errors=True)
        if on_progress:
            on_progress(f'开始清理环境变量。。。')
        try:
            sys_raw, _ = read_path(*_SYS_ENV_KEY)
        except RegError as e:
            if on_progress:
                on_progress(f'需要手动清理环境变量。读取系统 PATH 失败: {e}')
        try:
            usr_raw, vtype = read_path(*_USR_ENV_KEY)
        except RegError as e:
            if on_progress:
                on_progress(f'需要手动清理环境变量。读取用户 PATH 失败: {e}')
        remove_path = [install_dir, os.path.join(install_dir, 'Scripts')]
        new_sys, sys_removed = remove_from_path(sys_raw, remove_path)
        new_usr, usr_removed = remove_from_path(usr_raw, remove_path)
        # 系统 PATH 有变化 → 先 UAC 提权写 HKLM；失败即中止
        sys_changed = False
        if sys_removed:
            result = set_system_path(new_sys)
            if not result.get('success'):
                if on_progress:
                    on_progress(f'Python {version} 已卸载， 但系统 PATH 清理失败，需手动清理: {result.get("message", "未知错误")}')
            sys_changed = True
            if on_progress:
                on_progress(f'系统 PATH 已删除 Python 路径: {", ".join(sys_removed)}')

        # 用户 PATH 有变化 → 写 HKCU
        usr_changed = False
        if usr_removed:
            try:
                write_path(*_USR_ENV_KEY, new_usr, vtype=vtype)
            except RegError as e:
                msg = f'Python {version} 已卸载'
                if sys_changed:
                    msg += '，系统 PATH 已清理'
                msg += f'，但用户 PATH 清理失败，需手动清理: {e}'
                if on_progress:
                    on_progress(msg)
            usr_changed = True
            logger.info('用户 PATH 已删除 Python 路径: %s', ', '.join(usr_removed))
            if on_progress:
                on_progress(f'用户 PATH 已删除 Python 路径: {", ".join(usr_removed)}')

        # 广播 + 同步本进程 PATH
        if sys_changed or usr_changed:
            broadcast_env_change()
            eff = ([os.path.expandvars(p).strip() for p in new_sys.split(';') if p.strip()]
                   + [os.path.expandvars(p).strip() for p in new_usr.split(';') if p.strip()])
            os.environ['PATH'] = os.pathsep.join(p for p in eff if p)

    storage.remove_installed(version)

    suffix = '，安装目录遗留文件已删除' if remove_leftovers else '，安装目录遗留文件已保留'
    logger.info('Python %s 官方卸载完成%s', version, suffix)
    return {'success': True, 'message': f'Python {version} 已卸载{suffix}'}


# ── 终端 ─────────────────────────────────────────

from jiecore.system_ops import open_terminal


# ── 列表标记 ─────────────────────────────────────

def refresh_process_path() -> None:
    """从注册表重新读取系统+用户 PATH，同步更新当前进程的 os.environ['PATH']。

    安装程序（如 Python installer）在安装 py launcher 或勾选 PrependPath 时会向
    注册表 PATH 写入新条目，但当前 Django 服务进程的 os.environ['PATH'] 仍是旧值，
    导致 shutil.which('py') 等探测无法发现新安装的可执行文件。调用此函数后，
    后续 py_launcher_installed() 等探测将使用最新 PATH。
    """
    if os.name != 'nt':
        return
    try:
        entries = get_path_entries()
        if entries:
            os.environ['PATH'] = os.pathsep.join(entries)
            logger.debug('已从注册表刷新进程 PATH（%d 条目）', len(entries))
    except Exception as e:
        logger.warning('刷新进程 PATH 失败: %s', e)


def annotate_installed(records: list, py_default_tag: str = None) -> list:
    """为已安装列表记录填充标记并按版本倒序返回。

    - is_py_default: 是否为 py 启动器默认版本；
    - is_path_default: 是否为有效 PATH 中最先命中的 python（目录匹配，
      目录匹配不上时按解释器版本兜底匹配一次）；
    - exe_dir: 解释器所在目录，供前端“打开目录/打开命令行”按钮使用。

    py_default_tag: 可选，默认版本标签（major.minor），提供时跳过完整版本探测。
    """
    records = sorted(records,
                     key=lambda r: _ver_key(r.get('version', '')), reverse=True)
    # 若未提供标签，走完整探测（2 次 subprocess）
    if py_default_tag is None:
        py_default = get_py_default_version()
    else:
        py_default = None  # 用标签前缀匹配
    path_dir = get_path_default_dir()
    for r in records:
        exe = r.get('python_exe', '') or ''
        exe_dir = os.path.normcase(os.path.abspath(os.path.dirname(exe))) if exe else ''
        r['exe_dir'] = os.path.dirname(exe) if exe else ''
        if py_default_tag is not None:
            # 标签前缀匹配：版本 "3.13.15" 匹配标签 "3.13"
            ver = r.get('version', '')
            r['is_py_default'] = bool(py_default_tag) and (
                ver == py_default_tag or ver.startswith(py_default_tag + '.'))
        else:
            r['is_py_default'] = bool(py_default) and r.get('version') == py_default
        r['is_path_default'] = bool(path_dir) and bool(exe_dir) and exe_dir == path_dir
    if path_dir and not any(r['is_path_default'] for r in records):
        path_version = _probe_version(os.path.join(path_dir, 'python.exe'))
        if path_version:
            for r in records:
                if r.get('version') == path_version:
                    r['is_path_default'] = True
    return records