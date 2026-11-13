"""
Conda 发行版探测模块。

Miniconda / Miniforge JustMe 模式不写注册表的 InstallPath，但会在
HKCU / HKLM 的 Uninstall 下注册 DisplayName 和 UninstallString。
本模块通过扫描注册表 Uninstall 路径来发现系统中已安装的 Conda 发行版。

注册表无命中时返回空列表，由上层展示手动选择界面。

对外接口::

    scan_registry() -> list[dict]
    detect_custom_dir(folder_path) -> dict
"""
import os
import re
import logging
import subprocess
import winreg
from pathlib import Path

from apps.conda.config import CREATE_NO_WINDOW

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 10

_VERSION_RE = re.compile(r'conda\s+(\d+\.\d+\.\d+)', re.IGNORECASE)

_UNINSTALL_KEYS = [
    (winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKCU'),
    (winreg.HKEY_LOCAL_MACHINE, r'Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM'),
    (winreg.HKEY_LOCAL_MACHINE, r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM'),
]

_CONDA_KEYWORDS = re.compile(r'conda|miniconda|miniforge|anaconda', re.IGNORECASE)

_DISTRO_MAP = [
    ('miniforge', 'miniforge'),
    ('miniconda', 'miniconda'),
    ('anaconda', 'anaconda'),
    ('conda', 'conda'),
]


def _reg_str(key, name, default=''):
    try:
        val, _ = winreg.QueryValueEx(key, name)
        if isinstance(val, str):
            return val
        return default
    except OSError:
        return default


def _reg_int(key, name, default=0):
    try:
        val, _ = winreg.QueryValueEx(key, name)
        if isinstance(val, (int, float)):
            return int(val)
        return default
    except OSError:
        return default


def _classify_distro(display_name: str) -> str:
    low = display_name.lower()
    for keyword, distro in _DISTRO_MAP:
        if keyword in low:
            return distro
    return 'conda'


def _extract_install_dir(uninstall_string: str) -> str:
    if not uninstall_string:
        return ''
    m = re.match(r'^\s*"([^"]+)"', uninstall_string)
    if m:
        exe_path = m.group(1)
    else:
        exe_path = uninstall_string.strip().split()[0]

    name = os.path.basename(exe_path)
    if name.startswith('Uninstall-') and name.endswith('.exe'):
        parent = os.path.dirname(exe_path)
        return parent

    parent = os.path.dirname(exe_path)
    parent_name = os.path.basename(parent).lower()
    if any(k in parent_name for k in ('conda', 'miniconda', 'miniforge', 'anaconda')):
        return parent

    return os.path.dirname(exe_path)


def _probe_conda_version(conda_exe: str) -> str:
    try:
        if not Path(conda_exe).is_file():
            return ''
    except OSError:
        return ''

    p = Path(conda_exe)
    conda_bat = p.parent.parent / 'condabin' / 'conda.bat'
    cmd = [str(conda_bat)] if conda_bat.is_file() else [str(p)]

    safe_env = os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
        safe_env.pop(key, None)

    try:
        proc = subprocess.run(
            cmd + ['--version'],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
            env=safe_env,
        )
        if proc.returncode != 0:
            return ''
        out = (proc.stdout or proc.stderr or b'').decode('utf-8', errors='replace').strip()
        m = _VERSION_RE.search(out)
        if m:
            return m.group(1)
    except subprocess.TimeoutExpired:
        logger.debug('探测 conda 版本超时: %s', conda_exe)
    except Exception as e:
        logger.debug('探测 conda 版本失败 %s: %s', conda_exe, e)
    return ''


def _empty_record():
    return {
        'install_dir': '',
        'conda_exe': '',
        'version': '',
        'distro': 'conda',
        'display_name': '',
        'publisher': '',
        'display_version': '',
        'install_location': '',
        'uninstall_string': '',
        'quiet_uninstall_string': '',
        'display_icon': '',
        'install_date': '',
        'estimated_size_kb': 0,
        'language': 0,
        'comments': '',
        'help_link': '',
        'url_info_about': '',
        'url_update_info': '',
        'contact': '',
        'registry_hive': '',
        'registry_subkey': '',
        'registry_path': '',
    }


def _normalize_path(p: str) -> str:
    if not p:
        return ''
    return os.path.normpath(p).lower().rstrip('\\/')


def _lookup_registry_by_install_dir(install_dir: str) -> dict | None:
    """在注册表 Uninstall 下查找 InstallLocation 与目标目录匹配的条目。

    返回 scan_registry 单条结构（含所有注册表字段 + registry_* 来源），
    无匹配时返回 None。
    """
    target = _normalize_path(install_dir)
    if not target:
        return None

    for hive, subkey_path, hive_label in _UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(hive, subkey_path) as base:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(base, i)
                        i += 1
                    except OSError:
                        break

                    try:
                        with winreg.OpenKey(base, subkey_name) as sk:
                            install_location = _reg_str(sk, 'InstallLocation')
                            uninstall_string = _reg_str(sk, 'UninstallString') or _reg_str(sk, 'QuietUninstallString')

                            reg_dir = ''
                            if install_location:
                                reg_dir = _normalize_path(install_location)
                            elif uninstall_string:
                                reg_dir = _normalize_path(_extract_install_dir(uninstall_string))

                            if not reg_dir or reg_dir != target:
                                continue

                            display_name = _reg_str(sk, 'DisplayName')
                            if not display_name:
                                continue

                            rec = {
                                'install_location': install_location,
                                'uninstall_string': uninstall_string,
                                'quiet_uninstall_string': _reg_str(sk, 'QuietUninstallString'),
                                'display_name': display_name,
                                'publisher': _reg_str(sk, 'Publisher'),
                                'display_version': _reg_str(sk, 'DisplayVersion'),
                                'display_icon': _reg_str(sk, 'DisplayIcon'),
                                'install_date': _reg_str(sk, 'InstallDate'),
                                'estimated_size_kb': _reg_int(sk, 'EstimatedSize'),
                                'language': _reg_int(sk, 'Language'),
                                'comments': _reg_str(sk, 'Comments'),
                                'help_link': _reg_str(sk, 'HelpLink'),
                                'url_info_about': _reg_str(sk, 'URLInfoAbout'),
                                'url_update_info': _reg_str(sk, 'URLUpdateInfo'),
                                'contact': _reg_str(sk, 'Contact'),
                                'registry_hive': hive_label,
                                'registry_subkey': subkey_name,
                                'registry_path': f'{subkey_path}\\{subkey_name}',
                            }
                            return rec
                    except OSError:
                        continue
        except OSError:
            continue

    return None


def scan_registry() -> list:
    """扫描注册表 Uninstall 路径，返回所有命中的 conda 发行版。

    返回格式::

        [
            {
                # === 核心字段 ===
                'install_dir': 'D:\\Programs\\miniconda3',
                'conda_exe': 'D:\\Programs\\miniconda3\\Scripts\\conda.exe',
                'version': '26.7.0',                # conda --version 实际探测
                'distro': 'miniconda',
                'display_name': 'Miniconda3 py313_26.5.3-2 (Python 3.13.14 64-bit)',

                # === 注册表字段 ===
                'publisher': 'Anaconda, Inc.',
                'display_version': '26.5.3-2',       # 安装包版本（与 conda version 不同）
                'install_location': 'D:\\Programs\\miniconda3',
                'uninstall_string': '...',
                'quiet_uninstall_string': '',
                'display_icon': '...',
                'install_date': '20250115',           # yyyymmdd
                'estimated_size_kb': 1234567,
                'language': 0,
                'comments': '',
                'help_link': '',
                'url_info_about': '',
                'url_update_info': '',
                'contact': '',

                # === 注册表来源 ===
                'registry_hive': 'HKCU',
                'registry_subkey': 'Miniconda3 py313_26.5.3-2 (Python 3.13.14 64-bit)',
                'registry_path': 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Miniconda3 ...',
            },
            ...
        ]

    无命中时返回空列表。
    """
    results = []
    seen_dirs = set()

    for hive, subkey_path, hive_label in _UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(hive, subkey_path) as base:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(base, i)
                        i += 1
                    except OSError:
                        break

                    try:
                        with winreg.OpenKey(base, subkey_name) as sk:
                            display_name = _reg_str(sk, 'DisplayName')
                            if not display_name:
                                continue

                            if not _CONDA_KEYWORDS.search(display_name):
                                continue

                            uninstall_string = _reg_str(sk, 'UninstallString') or _reg_str(sk, 'QuietUninstallString')
                            if not uninstall_string:
                                continue

                            install_location = _reg_str(sk, 'InstallLocation')
                            install_dir = install_location.strip().rstrip('\\/') if install_location else ''
                            if not install_dir:
                                install_dir = _extract_install_dir(uninstall_string)

                            if not install_dir or install_dir in seen_dirs:
                                continue

                            conda_exe = str(Path(install_dir) / 'Scripts' / 'conda.exe')
                            if not Path(conda_exe).is_file():
                                logger.debug('注册表命中但 conda.exe 不存在: %s', install_dir)
                                continue

                            version = _probe_conda_version(conda_exe)
                            distro = _classify_distro(display_name)

                            rec = _empty_record()
                            rec.update({
                                'install_dir': install_dir,
                                'conda_exe': conda_exe,
                                'conda_bat': str(Path(install_dir) / 'condabin' / 'conda.bat'),
                                'version': version,
                                'distro': distro,
                                'display_name': display_name,
                                'publisher': _reg_str(sk, 'Publisher'),
                                'display_version': _reg_str(sk, 'DisplayVersion'),
                                'install_location': install_location,
                                'uninstall_string': uninstall_string,
                                'quiet_uninstall_string': _reg_str(sk, 'QuietUninstallString'),
                                'display_icon': _reg_str(sk, 'DisplayIcon'),
                                'install_date': _reg_str(sk, 'InstallDate'),
                                'estimated_size_kb': _reg_int(sk, 'EstimatedSize'),
                                'language': _reg_int(sk, 'Language'),
                                'comments': _reg_str(sk, 'Comments'),
                                'help_link': _reg_str(sk, 'HelpLink'),
                                'url_info_about': _reg_str(sk, 'URLInfoAbout'),
                                'url_update_info': _reg_str(sk, 'URLUpdateInfo'),
                                'contact': _reg_str(sk, 'Contact'),
                                'registry_hive': hive_label,
                                'registry_subkey': subkey_name,
                                'registry_path': f'{subkey_path}\\{subkey_name}',
                            })

                            seen_dirs.add(install_dir)
                            results.append(rec)
                    except OSError:
                        continue
        except OSError:
            continue

    return results


def detect_custom_dir(folder_path: str) -> dict:
    """探测用户手动选择的目录是否为合法 Conda 安装目录。

    返回成功 dict（结构同 scan_registry 单条）或失败 dict::

        {'success': False, 'message': '未找到 conda.exe'}
    """
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        return {'success': False, 'message': f'目录不存在: {folder_path}'}

    conda_exe = folder / 'Scripts' / 'conda.exe'
    if not conda_exe.is_file():
        return {'success': False, 'message': f'未找到 conda.exe，请确认目录结构（应为 <安装目录>/Scripts/conda.exe）'}

    version = _probe_conda_version(str(conda_exe))
    if not version:
        return {'success': False, 'message': '无法获取 conda 版本号，请确认该目录为有效 Conda 安装'}

    conda_bat = folder / 'condabin' / 'conda.bat'
    resolved_dir = str(folder.resolve())
    rec = _empty_record()
    rec.update({
        'install_dir': resolved_dir,
        'conda_exe': str(conda_exe),
        'conda_bat': str(conda_bat),
        'version': version,
        'distro': 'conda',
        'display_name': f'Conda ({version}) @ {folder.name}',
    })

    reg = _lookup_registry_by_install_dir(resolved_dir)
    if reg:
        rec.update(reg)
        if reg.get('display_name'):
            rec['distro'] = _classify_distro(reg['display_name'])

    return {'success': True, **rec}