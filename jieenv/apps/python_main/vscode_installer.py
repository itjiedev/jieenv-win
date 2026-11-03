"""
VSCode（Visual Studio Code）安装 / 探测 / 卸载支持。

与 PyCharm 不同，VSCode 官方不区分历史版本下载，统一从 update API 取最新稳定版：
    https://update.code.visualstudio.com/api/update/win32-x64{suffix}/stable/latest
其中 suffix 为 '-user'（User Installer）或 ''（System Installer），返回 JSON 含
url / productVersion / sha256hash 等字段，url 即实际 CDN 下载地址。

安装器为 Inno Setup，静默参数 /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
/MERGETASKS=... /DIR="..."。提权策略按需：
- System Installer 机器级安装（写 HKLM），一律提权；
- User Installer 目录在用户可写区域时不提权，否则提权。
"""
import os
import json
import time
import shutil
import logging
import pathlib
import subprocess
import urllib.request
from datetime import datetime

from jiecore.json_storage import JsonStore
from jiecore.system_ops import run_elevated
from .config import (VSCODE_INSTALLED_SOURCES_FILE, VSCODE_DOWNLOAD_DIR,
                     VSCODE_LATEST_CACHE_FILE, DEV_TOOLS_INSTALL_ROOT)

logger = logging.getLogger(__name__)

# update API：win32-x64-user（User Installer）/ win32-x64（System Installer）
VSCODE_UPDATE_API = 'https://update.code.visualstudio.com/api/update/win32-x64{suffix}/stable/latest'
INSTALLER_TYPES = ('user', 'system')
INSTALLER_LABELS = {'user': 'User Installer（用户）',
                    'system': 'System Installer（系统）'}

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
BROWSER_HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
FETCH_TIMEOUT = 20

# 下载重试次数
DOWNLOAD_MAX_ATTEMPTS = 3

# 静默安装耗时较短（解压 + 写注册表），给足超时
_VSCODE_INSTALL_TIMEOUT = 600
# 子进程隐藏窗口标志，避免闪现黑色控制台
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# Inno Setup 附加任务：不自动运行、加桌面图标，其他不运行
_VSCODE_MERGE_TASKS = ('!runcode,desktopicon')

_vscode_store = JsonStore(VSCODE_INSTALLED_SOURCES_FILE, default={})
_latest_cache = JsonStore(VSCODE_LATEST_CACHE_FILE, default={})


class PythonVscodeError(Exception):
    pass


# ── 最新版下载信息 ──────────────────────────────────────

def _api_url(installer_type: str) -> str:
    suffix = '-user' if installer_type == 'user' else ''
    return VSCODE_UPDATE_API.format(suffix=suffix)


def fetch_vscode_latest(installer_type: str = 'user') -> dict:
    """从官方 update API 抓取指定 installer 类型的最新稳定版下载信息。"""
    if installer_type not in INSTALLER_TYPES:
        raise PythonVscodeError(f'未知的 installer 类型: {installer_type}')
    url = _api_url(installer_type)
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    download_url = (data.get('url') or '').strip()
    if not download_url:
        raise PythonVscodeError('update API 未返回下载地址')
    filename = download_url.rstrip('/').split('/')[-1] or f'VSCodeSetup-{installer_type}.exe'
    return {
        'installer_type': installer_type,
        'installer_label': INSTALLER_LABELS.get(installer_type, installer_type),
        'productVersion': data.get('productVersion', ''),
        'url': download_url,
        'filename': filename,
        'sha256hash': data.get('sha256hash', ''),
        'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def get_vscode_latest(installer_type: str = 'user', refresh: bool = False) -> dict:
    """取最新版下载信息（带本地缓存）。抓取失败时回退到缓存，缓存也没有则抛出。"""
    cache = _latest_cache.read() or {}
    if not refresh and cache.get(installer_type):
        return cache[installer_type]
    try:
        info = fetch_vscode_latest(installer_type)
    except Exception as e:
        if cache.get(installer_type):
            logger.warning('抓取 VSCode 最新版失败，回退缓存: %s', e)
            return cache[installer_type]
        raise
    cache[installer_type] = info
    try:
        _latest_cache.write(cache)
    except OSError as e:
        logger.warning('VSCode 最新版缓存写入失败: %s', e)
    return info


def get_vscode_latest_all(refresh: bool = False) -> dict:
    """抓取 user / system 两种 installer 的最新版，返回 {installer_type: info|None}。

    任一类型抓取失败不阻断另一类型；全部失败时抛出最后一条异常。
    """
    result = {}
    last_error = None
    for it in INSTALLER_TYPES:
        try:
            result[it] = get_vscode_latest(it, refresh=refresh)
        except Exception as e:
            logger.warning('抓取 VSCode %s 最新版失败: %s', it, e)
            result[it] = None
            last_error = e
    if all(v is None for v in result.values()) and last_error:
        raise PythonVscodeError(f'抓取 VSCode 最新版失败: {last_error}')
    return result


# ── 下载 ────────────────────────────────────────────────

def _probe_remote_size(url: str) -> int:
    """探测远端文件大小，失败返回 0。"""
    try:
        head = urllib.request.Request(url, headers=BROWSER_HEADERS, method='HEAD')
        with urllib.request.urlopen(head, timeout=15) as resp:
            return int(resp.headers.get('Content-Length') or 0)
    except Exception as e:
        logger.warning('探测 VSCode 安装包大小失败: %s', e)
        return 0


def _download_once(url: str, exe_path: pathlib.Path, total: int, on_progress=None) -> int:
    """单次下载，返回实际写入的字节数。若中途出错会抛出异常。"""
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    downloaded = 0
    chunk_size = 1024 * 256
    with urllib.request.urlopen(req, timeout=600) as resp, open(exe_path, 'wb') as f:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total and on_progress:
                pct = int(downloaded * 100 / total)
                on_progress(f'下载中 {pct}% ({downloaded / 1048576:.1f}/{total / 1048576:.1f} MB)')
    return downloaded


def download_vscode(url: str, filename: str, on_progress=None) -> pathlib.Path:
    """下载 VSCode 安装包到 tmp 下载目录，返回本地文件路径。

    - 已下载完整（本地大小 >= 远端 Content-Length）则直接复用，避免重复下载；
    - 最多尝试 3 次下载，每次失败会清理未完成的临时文件后重试；
    - 三次都失败则抛出 PythonVscodeError。
    """
    VSCODE_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    exe_path = VSCODE_DOWNLOAD_DIR / filename

    total = _probe_remote_size(url)

    if exe_path.exists() and total and exe_path.stat().st_size >= total:
        if on_progress:
            on_progress(f'检测到已下载的安装包，直接复用: {exe_path}')
        return exe_path

    # 若存在不完整的旧文件，先删除
    if exe_path.exists():
        try:
            exe_path.unlink()
        except OSError:
            pass

    last_error = None
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        if on_progress:
            on_progress(f'开始下载（第 {attempt}/{DOWNLOAD_MAX_ATTEMPTS} 次）: {url}')
        try:
            downloaded = _download_once(url, exe_path, total, on_progress=on_progress)
            # 若已知远端大小，则校验是否完整
            if total and downloaded < total:
                raise PythonVscodeError(
                    f'下载不完整：{downloaded}/{total} 字节')
            if on_progress:
                on_progress(f'下载完成: {exe_path}（{downloaded / 1048576:.1f} MB）')
            return exe_path
        except Exception as e:
            last_error = e
            logger.warning('VSCode 下载第 %d 次失败: %s', attempt, e)
            if on_progress:
                on_progress(f'第 {attempt} 次下载失败: {e}')
            # 清理可能残留的半截文件
            if exe_path.exists():
                try:
                    exe_path.unlink()
                except OSError:
                    pass
            # 最后一次失败后不再等待
            if attempt < DOWNLOAD_MAX_ATTEMPTS:
                if on_progress:
                    on_progress('稍后重试...')
                time.sleep(2)

    raise PythonVscodeError(
        f'下载失败，已尝试 {DOWNLOAD_MAX_ATTEMPTS} 次：{last_error}')


# ── 安装目录与提权判定 ──────────────────────────────────

def default_install_dir(installer_type: str = 'user') -> str:
    """VSCode 默认安装目录（与 PyCharm 一致放在 dev_tools 下，无版本区分）。"""
    return str(DEV_TOOLS_INSTALL_ROOT / 'VSCode')


def _is_dir_writable(path: str) -> bool:
    """探测当前（非提权）进程能否在 path 写入：建目录 + 写探针文件。

    会把目录提前建出来（安装器随后也要用），失败即视为需要提权。
    """
    try:
        os.makedirs(path, exist_ok=True)
        probe = pathlib.Path(path) / '.jie_write_probe'
        probe.write_text('x', encoding='utf-8')
        probe.unlink()
        return True
    except (PermissionError, OSError):
        return False


def needs_elevation(install_dir: str, installer_type: str) -> bool:
    """是否需要 UAC 提权安装。

    System Installer 机器级安装（写 HKLM、默认 Program Files），一律提权；
    User Installer 仅当目标目录当前用户不可写时才提权。
    """
    if installer_type == 'system':
        return True
    return not _is_dir_writable(install_dir)


def build_install_params(install_dir: str) -> str:
    """构造 Inno Setup 静默安装参数（/DIR 必须带引号以兼容含空格路径）。"""
    return (f'/SILENT /NORESTART /SUPPRESSMSGBOXES '
            f'/MERGETASKS="{_VSCODE_MERGE_TASKS}" /DIR="{install_dir}"')


def _run_install_process(exe_path: str, params: str, timeout: int) -> int:
    """非提权直接运行安装程序（User Installer + 用户可写目录），返回退出码。"""
    cmd = f'"{exe_path}" {params}'
    try:
        proc = subprocess.run(cmd, timeout=timeout, creationflags=_CREATE_NO_WINDOW,
                              capture_output=True)
    except subprocess.TimeoutExpired:
        # 统一转成 TimeoutError，与 run_elevated 的超时语义一致，供上层同一分支处理
        raise TimeoutError(f'VSCode 安装超时（{timeout} 秒）')
    return proc.returncode


def run_silent_install(exe_path, install_dir: str, installer_type: str = 'user',
                       on_progress=None) -> int:
    """静默安装 VSCode，返回退出码。按需提权（见 needs_elevation）。"""
    install_dir = str(install_dir).rstrip('\\/')
    params = build_install_params(install_dir)
    elevate = needs_elevation(install_dir, installer_type)
    if on_progress:
        mode = '管理员权限（UAC）' if elevate else '当前用户权限'
        on_progress(f'正在以{mode}静默安装到: {install_dir}')
        if elevate:
            on_progress('若弹出 UAC 对话框，请点击“是”授权。')
        on_progress('安装过程可能需要一段时间，请关注安装进度窗口并耐心等待...')
    if elevate:
        # show=0：静默安装本身无界面，进度由前端 JieTaskLog 日志窗展示
        return run_elevated(str(exe_path), params, _VSCODE_INSTALL_TIMEOUT, show=0)
    return _run_install_process(str(exe_path), params, _VSCODE_INSTALL_TIMEOUT)


# ── 探测已安装 ──────────────────────────────────────────

def find_vscode_exe(install_dir: str):
    """定位 Code.exe（VSCode 主程序直接在安装目录根下，非 bin 子目录）。"""
    base = pathlib.Path(install_dir)
    for candidate in (base / 'Code.exe', base / 'bin' / 'Code.exe'):
        if candidate.exists():
            return candidate
    return None


def vscode_detect_installed():
    """扫描注册表 Uninstall 键探测已安装的 VSCode，结果写入缓存并返回。

    User Installer 注册在 HKCU、System Installer 注册在 HKLM，DisplayName 分别为
    "Microsoft Visual Studio Code (User)" 与 "Microsoft Visual Studio Code"。
    """
    import winreg

    def get_value(key, name, default=""):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except (FileNotFoundError, OSError):
            return default

    reg_key_list = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 'user'),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 'system'),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", 'system'),
    ]

    vscode = None
    for root, base_path, hive_type in reg_key_list:
        try:
            with winreg.OpenKey(root, base_path) as key:
                subkey_count = winreg.QueryInfoKey(key)[0]
                for i in range(subkey_count):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                    except OSError:
                        continue
                    try:
                        full_path = base_path + "\\" + subkey_name
                        with winreg.OpenKey(root, full_path) as subkey:
                            display_name = get_value(subkey, "DisplayName")
                            if "Visual Studio Code" not in display_name:
                                continue
                            install_location = get_value(subkey, "InstallLocation")
                            # installType 优先按 DisplayName 的 (User) 后缀判定，
                            # 其次按注册表 hive（HKCU=user / HKLM=system）
                            if '(User)' in display_name:
                                install_type = 'user'
                            elif '(System)' in display_name:
                                install_type = 'system'
                            else:
                                install_type = hive_type
                            vscode = {
                                'installType': install_type,
                                'installTypeLabel': INSTALLER_LABELS.get(install_type, install_type),
                                'version': get_value(subkey, "DisplayVersion"),
                                'DisplayName': display_name,
                                'DisplayVersion': get_value(subkey, "DisplayVersion"),
                                'InstallLocation': install_location,
                                'UninstallString': get_value(subkey, "UninstallString"),
                                'QuietUninstallString': get_value(subkey, "QuietUninstallString"),
                            }
                            break
                    except OSError as e:
                        logger.warning("打开 VSCode 子键失败: %s", e)
                        continue
        except OSError as e:
            logger.warning("打开注册表路径失败: %s", e)
            continue
        if vscode:
            break

    if vscode:
        logger.info("找到 VSCode 注册表信息: %s", vscode.get("DisplayName"))
        _vscode_store.write(vscode)
    else:
        logger.info("没有找到 VSCode 注册表信息")
        _vscode_store.write({})
    return vscode


def get_vscode_options(refresh: bool = False):
    """取本机已安装 VSCode 信息（带缓存），未安装返回 {}。"""
    cache = _vscode_store.read()
    if refresh or not cache:
        vscode_detect_installed()
        cache = _vscode_store.read()
    return cache


# ── 重置（清理用户配置） ────────────────────────────────

def vscode_user_data_dirs() -> list:
    """VSCode 用户配置 / 扩展目录，重置时清理。"""
    dirs = []
    appdata = os.environ.get('APPDATA')
    if appdata:
        dirs.append(os.path.join(appdata, 'Code'))
    userprofile = os.environ.get('USERPROFILE')
    if userprofile:
        dirs.append(os.path.join(userprofile, '.vscode'))
        dirs.append(os.path.join(userprofile, '.vscode-shared'))
    return dirs


def reset_vscode_settings() -> dict:
    """删除 %APPDATA%\\Code 与 %USERPROFILE%\\.vscode，返回 {success, message, removed}。"""
    removed, errors = [], []
    for d in vscode_user_data_dirs():
        if not os.path.exists(d):
            continue
        try:
            shutil.rmtree(d)
            removed.append(d)
        except OSError as e:
            errors.append(f'{d}: {e}')
    if errors:
        return {'success': False, 'removed': removed,
                'message': '部分目录清理失败: ' + '; '.join(errors)}
    return {'success': True, 'removed': removed,
            'message': 'VSCode 配置已重置' if removed else '没有需要清理的 VSCode 配置目录'}