"""
根据用户提供的目录解析 MySQL 实例信息（Windows）。

用户手动给出"安装目录"或"数据目录"，本模块据此识别实例的版本、端口、
Windows 服务等信息，兼容两种安装方式：

1. ZIP 免安装版：
   安装目录形如 ...\\mysql-8.4.11-winx64，内含 bin\\mysqld.exe 与 my.ini，
   数据目录通常是安装目录下的 data 子目录；
2. MSI Installer 安装版：
   安装目录形如 C:\\Program Files\\MySQL\\MySQL Server 8.4，
   my.ini 位于 C:\\ProgramData\\MySQL\\MySQL Server 8.4\\my.ini，
   数据目录默认在 C:\\ProgramData\\MySQL\\MySQL Server 8.4\\Data。

识别规则：
- 目录下存在 bin\\mysqld.exe（或 bin 目录）→ 视为安装目录；
- 目录下存在 ibdata1 / ib_logfile0 / mysql 子目录 → 视为数据目录；
- 解析 my.ini 得到 port / datadir；
- 扫描 Windows 服务（sc query/sc qc），按二进制路径匹配归属服务，
  兼容 installer 版"mysqld --defaults-file=... 服务名"的服务命令行。
"""
import os
import re
import socket
import logging
import subprocess
from pathlib import Path

from jiecore.utils import get_cmd_encoding

try:
    import winreg
except ImportError:
    winreg = None

logger = logging.getLogger(__name__)

# 子进程统一超时（秒）
_CMD_TIMEOUT = 15
# 子进程隐藏窗口标志：防止执行 sc/mysqld 等命令时闪现黑色控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 数据目录特征文件/目录
# _DATA_DIR_MARKERS = ('ibdata1', 'ib_logfile0', 'ibtmp1', 'mysql', 'undo_001')

# installer 版默认配置根目录
_PROGRAM_DATA_MYSQL = Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'MySQL'

# 注册表中查找 MSI 安装器的卸载信息路径
_UNINSTALL_REG_PATHS = [
    r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
    r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
]


class ResolveError(Exception):
    """目录无法识别为 MySQL 安装/数据目录"""


def _run(cmd: list) -> str:
    """执行命令返回 stdout，失败返回空串"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_CMD_TIMEOUT,
                              creationflags=_CREATE_NO_WINDOW)
        return proc.stdout.decode(get_cmd_encoding(), errors='replace')
    except Exception as e:
        logger.debug('命令执行失败 %s: %s', cmd, e)
        return ''


def check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    """TCP 端口连通性探测"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def find_my_ini(install_dir='', data_dir: str = '') -> 'Path | None':
    """按多种途径查找 my.ini 配置文件，优先 install_dir/data_dir，其次服务/ProgramData 等，找不到返回 None"""
    return _find_ini(Path(install_dir) if install_dir else None,
                     Path(data_dir) if data_dir else None)


def query_version(mysqld_exe: str) -> str:
    """通过 mysqld --version 获取版本号，如 8.0.36"""
    out = _run([mysqld_exe, '--version'])
    m = re.search(r'Ver\s+(\d+\.\d+\.\d+)', out)
    return m.group(1) if m else ''


def _entry_exists(p: Path) -> bool:
    """判断路径是否存在；数据目录常受服务账号 ACL 保护，
    stat 拒绝访问时视为存在"""
    try:
        p.lstat()
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _norm(path) -> str:
    """目录归一化，用于比较"""
    return str(path).replace('/', '\\').rstrip('\\').lower()


# ── my.ini 解析 ──────────────────────────────────────────

def _parse_ini(ini_path: Path) -> dict:
    """解析 my.ini 中的 port / datadir（取 [mysqld] 段，简单按行匹配）"""
    try:
        text = ini_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return {}
    result = {}
    m = re.search(r'^\s*port\s*=\s*(\d+)', text, re.MULTILINE)
    if m:
        result['port'] = int(m.group(1))
    m = re.search(r'^\s*datadir\s*=\s*["\']?([^"\'\r\n]+)', text, re.MULTILINE)
    if m:
        result['datadir'] = m.group(1).strip().rstrip('/')
    return result


def _find_ini(install_dir: 'Path | None' = None, data_dir: 'Path | None' = None) -> 'Path | None':
    """
    按优先级查找 my.ini，覆盖 ZIP 版与 MSI Installer 版所有常见位置：
    1. 安装目录自身（my.ini / my-default.ini）
    2. 数据目录自身及其上级目录
    3. Windows 服务 --defaults-file 指定路径
    4. ProgramData/MySQL/<ver>/my.ini（按版本名匹配 install_dir 或 data_dir）
    5. C:/Windows/my.ini
    6. 安装目录递归搜索（兜底）
    """
    candidates: list[Path] = []

    # 1. 安装目录自身
    if install_dir:
        candidates += [install_dir / 'my.ini', install_dir / 'my-default.ini']

    # 2. 数据目录自身及其上级（MSI 版 my.ini 常在数据目录上级）
    if data_dir:
        candidates += [data_dir / 'my.ini', data_dir.parent / 'my.ini']

    for c in candidates:
        if c.exists():
            return c

    # 3. Windows 服务 --defaults-file 路径（最准确的来源）
    for svc in _list_mysql_services():
        cfg = _parse_service_binpath(svc['name'])
        df = cfg.get('defaults_file', '')
        if df:
            ini = Path(df)
            if ini.exists():
                # 若服务二进制路径包含 install_dir 则直接确认归属
                if install_dir and _norm(install_dir) in cfg.get('bin_path', '').lower():
                    return ini
                # 否则先缓存，待后续兜底使用
                candidates.append(ini)

    # 4. ProgramData\MySQL\<sub>\my.ini，按版本名匹配
    if _PROGRAM_DATA_MYSQL.exists():
        try:
            for sub in _PROGRAM_DATA_MYSQL.iterdir():
                if not sub.is_dir():
                    continue
                ini = sub / 'my.ini'
                if not ini.exists():
                    continue
                # 优先：ini 的 datadir 与已知的 install_dir/data_dir 有交集
                ini_datadir = _norm(_parse_ini(ini).get('datadir', ''))
                if ini_datadir:
                    if install_dir and _norm(install_dir) in ini_datadir:
                        return ini
                    if data_dir and (_norm(data_dir) in ini_datadir
                                     or ini_datadir in _norm(data_dir)):
                        return ini
                # 次优先：ProgramData 子目录名与 install_dir 目录名相同
                #   例：install_dir = C:\Program Files\MySQL\MySQL Server 8.4
                #        ProgramData\MySQL\MySQL Server 8.4\my.ini
                if install_dir and sub.name.lower() == install_dir.name.lower():
                    return ini
        except (PermissionError, OSError):
            pass

    # 5. C:\Windows\my.ini（MySQL 5.x 及更早版本常见位置）
    win_dir = os.environ.get('WINDIR', r'C:\Windows')
    win_ini = Path(win_dir) / 'my.ini'
    if win_ini.exists():
        candidates.append(win_ini)

    # 6. 安装目录递归搜索（兜底，最多两层深度）
    if install_dir:
        try:
            for child in install_dir.rglob('my.ini'):
                # 限制深度，避免进入数据目录深层
                if len(child.relative_to(install_dir).parts) <= 3:
                    candidates.append(child)
        except (PermissionError, OSError):
            pass

    # 从候选列表中返回第一个存在的文件
    for c in candidates:
        if c.exists():
            return c
    return None


# ── Windows 服务匹配 ────────────────────────────────────

def _list_mysql_services() -> list:
    """sc query state= all 中名称/显示名含 mysql 的服务 [{name, display, running}]"""
    out = _run(['sc', 'query', 'state=', 'all'])
    services = []
    name = display = None
    running = False
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('SERVICE_NAME:'):
            if name and 'mysql' in (name + display).lower():
                services.append({'name': name, 'display': display, 'running': running})
            name = line.split(':', 1)[1].strip()
            display, running = '', False
        elif line.startswith('DISPLAY_NAME:'):
            display = line.split(':', 1)[1].strip()
        elif line.startswith('STATE'):
            running = 'RUNNING' in line
    if name and 'mysql' in (name + display).lower():
        services.append({'name': name, 'display': display, 'running': running})
    return services


def _parse_service_binpath(service_name: str) -> dict:
    """sc qc 解析服务二进制路径，提取 exe 路径、端口、defaults-file"""
    out = _run(['sc', 'qc', service_name])
    m = re.search(r'BINARY_PATH_NAME\s*:\s*(.+)', out)
    if not m:
        return {}
    bin_path = m.group(1).strip()

    result = {'bin_path': bin_path, 'exe_path': '', 'port': 0, 'defaults_file': ''}
    qm = re.match(r'"([^"]+)"', bin_path)
    exe = qm.group(1) if qm else bin_path.split(' ')[0]
    exe = exe.strip()
    if exe.lower().endswith('.exe'):
        result['exe_path'] = exe

    pm = re.search(r'--port[=\s]+(\d+)', bin_path)
    if pm:
        result['port'] = int(pm.group(1))

    dm = re.search(r'--defaults-file[=\s]+"([^"]+)"', bin_path)
    if not dm:
        dm = re.search(r'--defaults-file[=\s]+(\S+)', bin_path)
    if dm:
        result['defaults_file'] = dm.group(1)
    return result


def _find_service(install_dir: Path) -> dict:
    """
    在 MySQL 服务中找到与给定目录归属同一实例的服务。
    匹配依据：服务命令行包含安装目录路径，或其 defaults-file 的
    返回 {service_name, running, port, defaults_file} 或 {}
    """
    target_install = _norm(install_dir) if install_dir else ''
    # target_data = _norm(data_dir) if data_dir else ''
    for svc in _list_mysql_services():
        cfg = _parse_service_binpath(svc['name'])
        if not cfg:
            continue
        bin_path = cfg['bin_path'].lower()
        hit = bool(target_install) and target_install in bin_path
        if not hit and cfg.get('defaults_file'):
            ini = Path(cfg['defaults_file'])
            if ini.exists():
                ini_datadir = _norm(_parse_ini(ini).get('datadir', ''))
                hit = bool(ini_datadir) and bool(target_install) and (
                    target_install in ini_datadir
                    or ini_datadir in target_install
                )
        # if not hit and target_data and target_data in bin_path:
        #     hit = True
        if hit:
            return {'service_name': svc['name'], 'running': svc['running'],
                    'port': cfg.get('port', 0),
                    'defaults_file': cfg.get('defaults_file', '')}
    return {}


# ── MSI 安装方式检测 ──────────────────────────────────────

def find_msi_product_code(install_dir: str) -> str:
    """从 Windows 注册表卸载信息中查找与 install_dir 匹配的 MSI 产品代码。
    未找到返回空串。"""
    if not winreg or not install_dir:
        return ''
    norm_dir = _norm(install_dir)
    for reg_path in _UNINSTALL_REG_PATHS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    # MSI 产品代码形如 {XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}
                    if not (subkey_name.startswith('{') and subkey_name.endswith('}')):
                        continue
                    try:
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            display_name = ''
                            install_location = ''
                            try:
                                display_name, _ = winreg.QueryValueEx(subkey, 'DisplayName')
                            except OSError:
                                pass
                            try:
                                install_location, _ = winreg.QueryValueEx(subkey, 'InstallLocation')
                            except OSError:
                                pass
                            # 按显示名含 MySQL 且安装目录匹配
                            if ('mysql' in (display_name or '').lower()
                                    and install_location
                                    and _norm(install_location) == norm_dir):
                                return subkey_name
                    except OSError:
                        continue
        except OSError:
            continue
    return ''


def detect_install_method(install_dir) -> str:
    """检测 MySQL 安装方式：'zip' / 'msi' / ''（远程或未知）"""
    if not install_dir:
        return ''
    install_dir_str = str(install_dir)
    # MSI 安装器通常安装到 Program Files 且注册表有卸载信息
    if find_msi_product_code(install_dir_str):
        return 'msi'
    # 有 bin/mysqld.exe 但无 MSI 注册 → ZIP 版
    mysqld = Path(install_dir_str) / 'bin' / 'mysqld.exe'
    if mysqld.exists():
        return 'zip'
    return ''


def msi_uninstall_product(product_code: str, timeout: int = 300) -> tuple:
    """通过 msiexec /x 卸载 MSI 产品，返回 (success: bool, message: str)。
    三级后备：/qn（静默）→ /qb（基本 UI，允许 UAC）→ run_elevated（提权执行）。
    兼容 pywebview 桌面壳低完整性级别进程。
    """
    if not product_code:
        return False, '未提供 MSI 产品代码'

    def _check_rc(rc):
        """检查 msiexec 退出码，返回 (success, message) 或 None 表示失败"""
        if rc in (0, 1605):
            return True, f'MSI 卸载完成（退出码 {rc}）'
        if rc == 1641:
            return True, 'MSI 卸载完成，系统正在重启'
        if rc == 3010:
            return True, 'MSI 卸载完成，需要重启以完成清理'
        return None

    # ── 第一级：/qn 静默卸载（已具管理员权限时有效） ──
    try:
        proc = subprocess.run(
            ['msiexec', '/x', product_code, '/qn', '/norestart'],
            capture_output=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW)
        result = _check_rc(proc.returncode)
        if result:
            return result
    except subprocess.TimeoutExpired:
        return False, f'MSI 卸载超时（{timeout}秒）'
    except Exception:
        pass

    # ── 第二级：/qb 基本 UI 卸载（允许 UAC 弹窗，纯 Web 模式有效） ──
    try:
        proc = subprocess.run(
            ['msiexec', '/x', product_code, '/qb', '/norestart'],
            capture_output=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW)
        result = _check_rc(proc.returncode)
        if result:
            return result
    except subprocess.TimeoutExpired:
        return False, f'MSI 卸载超时（{timeout}秒）'
    except Exception:
        pass

    # ── 第三级：run_elevated 提权执行（兼容 pywebview 桌面壳） ──
    try:
        from jiecore.system_ops import run_elevated
        import tempfile
        tag = product_code[:8].strip('{}')
        log_file = Path(tempfile.gettempdir()) / f'jie_msi_uninstall_{tag}.log'
        bat_file = Path(tempfile.gettempdir()) / f'jie_msi_uninstall_{tag}.bat'
        bat_content = (
            f'@echo off\n'
            f'msiexec /x {product_code} /qn /norestart '
            f'> "{log_file}" 2>&1\n'
            f'echo %ERRORLEVEL% > "{log_file}.rc"\n'
        )
        bat_file.write_text(bat_content, encoding='gbk')
        try:
            rc = run_elevated('cmd.exe', f'/c "{bat_file}"', timeout)
        finally:
            try:
                bat_file.unlink()
            except OSError:
                pass
        # 从日志文件读取实际退出码
        rc_file = Path(str(log_file) + '.rc')
        actual_rc = -1
        try:
            if rc_file.exists():
                actual_rc = int(rc_file.read_text(encoding='gbk').strip())
                rc_file.unlink()
        except (ValueError, OSError):
            pass
        result = _check_rc(actual_rc)
        if result:
            return result
        return False, f'MSI 提权卸载失败（退出码 {actual_rc}）'
    except Exception as e:
        return False, f'MSI 提权卸载执行异常: {e}'


# ── 主入口 ───────────────────────────────────────────────

def _guess_version_from_name(text: str) -> str:
    """从目录名中提取版本号（兜底），如 mysql-8.4.11-winx64 / MySQL Server 8.4"""
    m = re.search(r'(\d+\.\d+(?:\.\d+)?)', text)
    return m.group(1) if m else ''


def resolve_instance(path: str) -> dict:
    """
    根据安装目录或数据目录解析实例信息。

    返回 dict:
        {name, host, port, version, install_dir, data_dir,
         service_name, run_mode, reachable, remark}
    无法识别时抛 ResolveError。
    """
    path = (path or '').strip().strip('"')
    if not path:
        raise ResolveError('请填写安装目录')
    root = Path(path)
    if not root.exists() or not root.is_dir():
        raise ResolveError(f'目录不存在: {path}')

    # 判定目录类型
    mysqld = root / 'bin' / 'mysqld.exe'
    if not mysqld.exists() and (root / 'mysqld.exe').exists():
        mysqld = root / 'mysqld.exe'
    # has_data_marker = any(_entry_exists(root / m) for m in _DATA_DIR_MARKERS)

    if mysqld.exists() or (root / 'bin').is_dir():
        install_dir, data_dir = root, None
    # elif has_data_marker:
    #     install_dir, data_dir = None, root
    else:
        raise ResolveError('无法识别该目录：不含 bin\\mysqld.exe（安装目录），')

    # ZIP 版数据目录默认在安装目录下的 data
    if install_dir and not data_dir:
        default_data = install_dir / 'data'
        if default_data.exists():
            data_dir = default_data

    # 关联 Windows 服务（installer 版通常以服务方式运行）
    svc = _find_service(install_dir)
    port = svc.get('port', 0)

    # my.ini → port / datadir；服务 --defaults-file 也是 my.ini 的重要来源
    ini = _find_ini(install_dir, data_dir)
    if not ini and svc.get('defaults_file'):
        candidate = Path(svc['defaults_file'])
        if candidate.exists():
            ini = candidate
    ini_conf = _parse_ini(ini) if ini else {}
    if not port:
        port = ini_conf.get('port', 0)
    if not data_dir and ini_conf.get('datadir'):
        candidate = Path(ini_conf['datadir'])
        if candidate.exists():
            data_dir = candidate
    port = port or 3306

    # 版本号：优先 mysqld --version，其次服务 exe，最后目录名猜
    version = ''
    exe = str(mysqld) if mysqld.exists() else ''
    if version == '' and exe:
        version = query_version(exe)
    if not version and install_dir:
        version = _guess_version_from_name(install_dir.name)
    if not version and data_dir and data_dir.parent.name.lower() != 'data':
        version = _guess_version_from_name(data_dir.parent.name)

    # 显示名称
    name = ''
    if svc.get('service_name'):
        name = svc['service_name']
    elif install_dir:
        name = install_dir.name
    elif data_dir:
        name = data_dir.parent.name if data_dir.name.lower() == 'data' else data_dir.name

    remark_parts = []
    if install_dir:
        remark_parts.append('安装目录识别')
    if data_dir:
        remark_parts.append('数据目录识别')
    if svc.get('service_name'):
        remark_parts.append('已关联 Windows 服务')
    if ini:
        remark_parts.append(f'配置文件 {ini.name}')

    # 检测安装方式：ZIP 还是 MSI Installer
    install_method = detect_install_method(install_dir)
    if install_method == 'msi':
        remark_parts.append('MSI Installer 安装')

    return {
        'name': name or f'MySQL@127.0.0.1:{port}',
        'host': '127.0.0.1',
        'port': port,
        'version': version,
        'install_dir': str(install_dir) if install_dir else '',
        'data_dir': str(data_dir) if data_dir else '',
        'service_name': svc.get('service_name', ''),
        'run_mode': 'service' if svc.get('service_name') else 'process',
        'install_method': install_method,
        'reachable': check_port('127.0.0.1', port),
        'remark': '，'.join(remark_parts),
    }