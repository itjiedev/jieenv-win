"""
探测当前系统中通过 Python 官方 Installer 安装的 Python（Windows）。

python_installer 使用官网 amd64.exe 安装器静默安装 Python，安装器会把解释器
注册到 Windows 注册表 Software\\Python\\PythonCore 并登记到 py 启动器，因此本模块
从以下来源收集候选解释器，并逐一探测其精确版本号（x.y.z）：

1. py 启动器（py -0p）—— 覆盖通过官方安装器安装并注册的版本；
2. 注册表 Software\\Python\\PythonCore（HKLM/HKCU，含 64/32 位视图）；
3. 本应用的安装记录（storage.list_installed）。

对每个候选解释器执行 `<exe> -c "..."` 获取精确版本号，仅保留能成功返回版本的解释器，
从而过滤失效路径与占位别名。PATH 中的 python.exe 不作为来源，避免把 embeddable
zip、Microsoft Store 等非安装器版本误判为 installer 安装。

对外接口::

    from apps.python_installer.detect import detect_installed, detect_installed_versions

    detect_installed()          -> {'3.13.15': {'path': 'C:\\\\...\\\\python.exe', 'source': 'registry'}, ...}
    detect_installed_versions() -> ['3.13.15', '3.12.10', ...]   # 版本号倒序
"""
import os
import re
import logging
import subprocess
from pathlib import Path

from . import storage

logger = logging.getLogger(__name__)

# 子进程隐藏窗口标志
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 单个解释器探测版本的超时时间（秒）
_PROBE_TIMEOUT = 8

# py 启动器输出中形如 "C:\...\python.exe" 的路径（允许路径含空格，取到行尾）
_PY_LAUNCHER_PATH_RE = re.compile(r'([A-Za-z]:\\.*python\.exe)\s*$', re.IGNORECASE)

_VERSION_RE = re.compile(r'\d+\.\d+\.\d+')


def _probe_version(exe: str) -> str:
    """执行解释器获取精确版本号（x.y.z），失败返回空串。"""
    if not exe:
        return ''
    try:
        if not Path(exe).is_file():
            return ''
    except OSError:
        return ''
    try:
        proc = subprocess.run(
            [exe, '-c', 'import sys;print("%d.%d.%d"%sys.version_info[:3])'],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            return ''
        out = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
        first = out.splitlines()[0].strip() if out else ''
        if _VERSION_RE.fullmatch(first):
            return first
    except subprocess.TimeoutExpired:
        logger.debug('探测解释器版本超时: %s', exe)
    except Exception as e:
        logger.debug('探测解释器版本失败 %s: %s', exe, e)
    return ''


# ── 各来源候选解释器路径 ──────────────────────────────

def _from_py_launcher() -> list:
    """通过 py 启动器列出已安装的 Python 解释器路径。"""
    paths = []
    if os.name != 'nt':
        return paths
    for args in (['py', '-0p'], ['py', '--list-paths']):
        try:
            proc = subprocess.run(args, capture_output=True, timeout=_PROBE_TIMEOUT,
                                  creationflags=_CREATE_NO_WINDOW)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            break
        out = (proc.stdout or b'').decode('utf-8', errors='replace')
        for line in out.splitlines():
            m = _PY_LAUNCHER_PATH_RE.search(line.strip())
            if m:
                paths.append(m.group(1))
        if paths:
            break
    return paths


def _from_registry() -> list:
    """从注册表 PythonCore 读取安装器登记的解释器路径。"""
    paths = []
    if os.name != 'nt':
        return paths
    try:
        import winreg
    except ImportError:
        return paths

    hives = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
    subkeys = [
        r'SOFTWARE\Python\PythonCore',
        r'SOFTWARE\WOW6432Node\Python\PythonCore',
    ]
    for hive in hives:
        for sub in subkeys:
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
                    # name 形如 "3.13" 或 "3.13-64"
                    try:
                        with winreg.OpenKey(core, name + r'\InstallPath') as ip:
                            install_dir, _ = winreg.QueryValueEx(ip, '')
                        if install_dir:
                            paths.append(str(Path(install_dir) / 'python.exe'))
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(core)
    return paths


def _extract_exe_from_cmd(cmd: str) -> str:
    """从命令行字符串中提取可执行文件路径。

    注册表 UninstallString / ModifyPath 常形如：
      "C:\\ProgramData\\Package Cache\\{guid}\\python.exe" /uninstall
    提取引号内或第一个空格前的路径。
    """
    cmd = cmd.strip()
    if not cmd:
        return ''
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        return cmd[1:end] if end > 1 else ''
    return cmd.split()[0]


def _read_registry_detail(install_dir: str) -> dict:
    """从注册表读取指定安装的详细信息（ExecutablePath、Uninstall 信息等）。"""
    info = {}
    if os.name != 'nt' or not install_dir:
        return info
    try:
        import winreg
    except ImportError:
        return info

    # 从 PythonCore 读取 ExecutablePath
    hives = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
    subkeys = [
        r'SOFTWARE\Python\PythonCore',
        r'SOFTWARE\WOW6432Node\Python\PythonCore',
    ]
    for hive in hives:
        for sub in subkeys:
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
                    try:
                        with winreg.OpenKey(core, name + r'\InstallPath') as ip:
                            reg_dir, _ = winreg.QueryValueEx(ip, '')
                            if reg_dir and os.path.normcase(reg_dir).rstrip('\\') == os.path.normcase(install_dir).rstrip('\\'):
                                # 读取 InstallPath 下所有可用值
                                vi = 0
                                while True:
                                    try:
                                        vname, vdata, _ = winreg.EnumValue(ip, vi)
                                        vi += 1
                                        key_map = {'': 'install_path_default', 'ExecutablePath': 'executable_path',
                                                   'WindowedExecutablePath': 'windowed_executable_path'}
                                        if vname in key_map:
                                            info[key_map[vname]] = vdata
                                    except OSError:
                                        break
                                info['registry_version_tag'] = name
                                info['registry_hive'] = 'HKLM' if hive == winreg.HKEY_LOCAL_MACHINE else 'HKCU'
                                # 读取 PythonCore\<version> 下的顶层值
                                try:
                                    with winreg.OpenKey(core, name) as vk:
                                        for vn in ('DisplayName', 'SupportUrl', 'Version'):
                                            try:
                                                vd, _ = winreg.QueryValueEx(vk, vn)
                                                info[f'pycore_{vn.lower()}'] = vd
                                            except FileNotFoundError:
                                                pass
                                except OSError:
                                    pass
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(core)
        if info:
            break

    # 从 Uninstall 注册表读取更多信息
    # 匹配策略：优先按 InstallLocation 精确比对路径，
    #           后备按 registry_version_tag 版本号 + DisplayName 包含 "Python"
    version_tag = info.get('registry_version_tag', '')  # 如 "3.12" 或 "3.12-64"
    uninstall_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hkey_root, subpath in uninstall_paths:
        try:
            with winreg.OpenKey(hkey_root, subpath, 0, winreg.KEY_READ) as parent_key:
                subkey_count, _, _ = winreg.QueryInfoKey(parent_key)
                for i in range(subkey_count):
                    subkey_name = winreg.EnumKey(parent_key, i)
                    try:
                        with winreg.OpenKey(parent_key, subkey_name) as app_key:
                            display_name = winreg.QueryValueEx(app_key, "DisplayName")[0] if True else ''
                            # 排除 py launcher，必须包含 "Python"
                            if 'Python' not in display_name or 'Launcher' in display_name:
                                continue
                            # 匹配方式 1: InstallLocation 与 install_dir 一致
                            matched = False
                            try:
                                install_loc = winreg.QueryValueEx(app_key, "InstallLocation")[0]
                                if install_loc and os.path.normcase(install_loc).rstrip('\\') == os.path.normcase(install_dir).rstrip('\\'):
                                    matched = True
                            except FileNotFoundError:
                                pass
                            # 匹配方式 2: 版本号在 DisplayName 中出现
                            if not matched and version_tag:
                                major_minor = version_tag.split('-')[0]  # "3.12-64" → "3.12"
                                if major_minor in display_name:
                                    matched = True
                            if not matched:
                                continue
                            # 尽可能多地读取 Uninstall 注册表字段
                            for val_name in (
                                'DisplayVersion', 'DisplayIcon', 'DisplayName',
                                'InstallLocation', 'InstallSource', 'UninstallString',
                                'Publisher', 'EstimatedSize', 'HelpLink',
                                'ModifyPath', 'URLInfoAbout', 'Comments',
                                'ReleaseType', 'SystemComponent',
                            ):
                                try:
                                    val, _ = winreg.QueryValueEx(app_key, val_name)
                                    info[val_name.lower()] = val
                                except FileNotFoundError:
                                    pass
                            # 映射关键字段到存储标准名
                            uninstall_str = info.get('uninstallstring', '')
                            if uninstall_str:
                                info['uninstall_exe'] = _extract_exe_from_cmd(uninstall_str)
                            # 缓存安装包路径（Package Cache 中的 exe）
                            # ModifyPath 指向 MsiExec.exe 时为 MSI 安装，跳过
                            # InstallSource 可能是目录，搜索其中的 exe
                            modify_path = info.get('modifypath', '')
                            install_source = info.get('installsource', '')
                            mod_exe = _extract_exe_from_cmd(modify_path)
                            if mod_exe and not mod_exe.lower().endswith('msiexec.exe'):
                                info['cached_installer'] = mod_exe
                            elif install_source:
                                src = install_source.strip()
                                if os.path.isdir(src):
                                    for name in os.listdir(src):
                                        if name.lower().endswith('.exe'):
                                            info['cached_installer'] = os.path.join(src, name)
                                            break
                                elif os.path.isfile(src) and src.lower().endswith('.exe'):
                                    info['cached_installer'] = src
                            info['uninstall_reg_path'] = fr"{hkey_root}\{subpath}\{subkey_name}"
                            return info
                    except (FileNotFoundError, OSError):
                        continue
        except (FileNotFoundError, OSError):
            continue
    return info


def _from_installer_records() -> list:
    """从本应用的安装记录读取解释器路径。"""
    paths = []
    try:
        for record in storage.list_installed():
            exe = record.get('python_exe') or ''
            if exe:
                paths.append(exe)
    except Exception as e:
        logger.debug('读取安装记录失败: %s', e)
    return paths


# ── 汇总探测 ────────────────────────────────────────

def detect_installed() -> dict:
    """
    探测系统中通过 installer 安装的 Python。

    返回 {version: {'path': exe_path, 'source': 来源标记}}，version 为精确三段版本号。
    同一版本以最先探测到的来源为准；同一解释器路径不重复探测。
    """
    sources = [
        ('installer_record', _from_installer_records),
        ('registry', _from_registry),
        ('py_launcher', _from_py_launcher),
    ]
    result = {}
    probed = set()
    for source, collector in sources:
        try:
            candidates = collector()
        except Exception as e:
            logger.debug('探测来源 %s 失败: %s', source, e)
            continue
        for exe in candidates:
            if not exe:
                continue
            key = os.path.normcase(os.path.abspath(exe))
            if key in probed:
                continue
            probed.add(key)
            version = _probe_version(exe)
            if version and version not in result:
                result[version] = {'path': exe, 'source': source}
    logger.info('installer Python 探测完成，共 %d 个版本: %s',
                len(result), ', '.join(sorted(result.keys(), reverse=True)))
    return result


def detect_installed_versions() -> list:
    """返回系统中通过 installer 安装的 Python 版本号列表（倒序）。"""
    versions = list(detect_installed().keys())
    versions.sort(key=lambda v: tuple(int(x) for x in v.split('.')), reverse=True)
    return versions


def import_detected() -> dict:
    """
    探测系统中通过 installer 安装的 Python，清除旧记录后重新写入。

    每次探测都会清空现有已安装列表，然后重新从系统（注册表、py 启动器）
    探测并写入最新数据，确保列表与实际系统状态一致。
    返回 {'added': [version, ...], 'total': 探测总数}，added 按版本号倒序。
    """
    # 清除旧记录
    storage.clear_all_installed()

    detected = detect_installed()
    added = []
    for version, info in detected.items():
        exe = info.get('path', '') or ''
        install_dir = str(Path(exe).parent) if exe else ''
        # 从注册表读取详细信息
        reg_info = _read_registry_detail(install_dir)
        record = {
            'version': version,
            'name': f'Python {version}',
            'install_dir': install_dir,
            'python_exe': exe,
            'uninstall_exe': reg_info.get('uninstall_exe', ''),
            'status': 'available',
            'source': info.get('source', 'detect'),
            'imported': True,
        }
        # 存储注册表中读取的额外信息（含 cached_installer 等）
        record.update(reg_info)
        storage.add_installed(record)
        added.append(version)
        logger.info('探测写入 Python %s -> %s', version, exe)

    added.sort(key=lambda v: tuple(int(x) for x in v.split('.')), reverse=True)
    return {'added': added, 'total': len(detected)}


#---------- 探测 py launcher 注册表信息 -----------------------
import winreg
def find_python_launcher_from_registry():
    # 两个注册表路径，64位和32位视图
    uninstall_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    ]
    result_list = {}

    for hkey_root, subpath in uninstall_paths:
        try:
            # 打开Uninstall父项
            with winreg.OpenKey(hkey_root, subpath, 0, winreg.KEY_READ) as parent_key:
                # QueryInfoKey 返回 (子键数量, 值数量, 最后写入时间)
                subkey_count, _, _ = winreg.QueryInfoKey(parent_key)
                for i in range(subkey_count):
                    subkey_name = winreg.EnumKey(parent_key, i)
                    try:
                        # 打开每个子项（GUID）
                        with winreg.OpenKey(parent_key, subkey_name) as app_key:
                            display_name = winreg.QueryValueEx(app_key, "DisplayName")[0]
                            # 匹配 Python Launcher
                            if "Python Launcher" in display_name:
                                product_code = subkey_name
                                install_location = ""
                                try:
                                    install_location = winreg.QueryValueEx(app_key, "InstallLocation")[0]
                                except FileNotFoundError:
                                    pass
                                result_list = {
                                    "DisplayName": display_name,
                                    "DisplayVersion": winreg.QueryValueEx(app_key, "DisplayVersion")[0],
                                    "InstallLocation": install_location,
                                    "InstallSource": winreg.QueryValueEx(app_key, "InstallSource")[0],
                                    "RegPath": fr"HKEY_LOCAL_MACHINE\{subpath}\{subkey_name}",
                                    "UninstallString": winreg.QueryValueEx(app_key, "UninstallString")[0]
                                }
                                break
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            continue
    return result_list