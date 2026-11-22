import os
import json
import shutil
import logging
import pathlib
import subprocess
import urllib.request
from datetime import datetime

from jiecore.json_storage import JsonStore
from jiecore.system_ops import run_elevated

from apps.devtools.config import DEV_TOOLS_INSTALL_ROOT
from apps.devtools.services.dbeaver.config import (
    DBEAVER_INSTALLED_SOURCES_FILE, DBEAVER_DOWNLOAD_DIR,
    DBEAVER_LATEST_CACHE_FILE,
)

logger = logging.getLogger(__name__)

DBEAVER_GITHUB_LATEST = 'https://api.github.com/repos/dbeaver/dbeaver/releases/latest'
DBEAVER_DOWNLOAD_PAGE = 'https://dbeaver.io/download/'

DBEAVER_MIRRORS = [
    {
        'key': 'tuna',
        'label': '清华大学镜像',
        'template': 'https://mirror.tuna.tsinghua.edu.cn/github-release/dbeaver/dbeaver/LatestRelease/dbeaver-ce-{version}-windows-x86_64.{suffix}',
    },
    {
        'key': 'nju',
        'label': '南京大学镜像',
        'template': 'https://mirror.nju.edu.cn/github-release/dbeaver/dbeaver/LatestRelease/dbeaver-ce-{version}-windows-x86_64.{suffix}',
    },
    {
        'key': 'ustc',
        'label': '中国科大镜像',
        'template': 'https://mirrors.ustc.edu.cn/github-release/dbeaver/dbeaver/LatestRelease/dbeaver-ce-{version}-windows-x86_64.{suffix}',
    },
    {
        'key': 'dbeaver',
        'label': 'DBeaver 官网',
        'template': 'https://dbeaver.io/files/dbeaver-ce-latest-windows-x86_64.{suffix}',
    },
]

INSTALLER_TYPES = ('exe',)
INSTALLER_LABELS = {
    'exe': 'Installer（安装版）',
}

USER_AGENT = 'curl/8.0.1'
BROWSER_HEADERS = {'User-Agent': USER_AGENT}
GITHUB_HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json'}
FETCH_TIMEOUT = 20

DOWNLOAD_MAX_ATTEMPTS = 3
_INSTALL_TIMEOUT = 900
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

_dbeaver_store = JsonStore(DBEAVER_INSTALLED_SOURCES_FILE, default={})
_latest_cache = JsonStore(DBEAVER_LATEST_CACHE_FILE, default={})


class DBeaverError(Exception):
    pass


def _build_mirrors(version: str) -> list[dict]:
    list_m = []
    for m in DBEAVER_MIRRORS:
        url = m['template'].format(version=version, suffix='exe') if '{version}' in m['template'] else m['template'].format(suffix='exe')
        list_m.append({'key': m['key'], 'label': m['label'], 'url': url})
    return list_m


def _parse_version_from_page(html: str) -> str:
    import re
    matches = re.findall(r'(?:Version[^0-9]*|>)(\d{2}\.\d+\.\d+)', html, re.IGNORECASE)
    if matches:
        return matches[0]
    return ''


def _build_result(version: str) -> dict:
    mirrors = _build_mirrors(version)
    return {
        'exe': {
            'installer_type': 'exe',
            'installer_label': INSTALLER_LABELS['exe'],
            'productVersion': version,
            'mirrors': mirrors,
            'url': mirrors[0]['url'],
            'filename': f'dbeaver-ce-{version}-windows-x86_64.exe',
            'supports_update': True,
            'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    }


def _fetch_from_github() -> dict:
    req = urllib.request.Request(DBEAVER_GITHUB_LATEST, headers=GITHUB_HEADERS)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    version = data.get('tag_name', '').strip()
    if not version:
        raise DBeaverError('GitHub API 返回缺少 tag_name')
    return _build_result(version)


def _fetch_from_website() -> dict:
    req = urllib.request.Request(DBEAVER_DOWNLOAD_PAGE, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        html_text = resp.read().decode('utf-8', errors='ignore')
    version = _parse_version_from_page(html_text)
    if not version:
        raise DBeaverError('官网页面未匹配到版本号')
    return _build_result(version)


def fetch_dbeaver_latest() -> dict:
    try:
        return _fetch_from_github()
    except Exception as e:
        logger.warning('GitHub API 抓取失败，降级官网: %s', e)
        return _fetch_from_website()


def get_dbeaver_latest_all(refresh: bool = False, allow_fetch: bool = True) -> dict:
    cache = _latest_cache.read() or {}
    if not refresh and cache:
        return cache
    if not refresh and not cache and not allow_fetch:
        return {}
    try:
        info = fetch_dbeaver_latest()
    except Exception as e:
        if cache:
            logger.warning('抓取 DBeaver 最新版失败，回退缓存: %s', e)
            return cache
        raise DBeaverError(f'抓取 DBeaver 最新版失败: {e}')
    try:
        _latest_cache.write(info)
    except OSError as e:
        logger.warning('DBeaver 最新版缓存写入失败: %s', e)
    return info


def _probe_remote_size(url: str) -> int:
    try:
        head = urllib.request.Request(url, headers=BROWSER_HEADERS, method='HEAD')
        with urllib.request.urlopen(head, timeout=15) as resp:
            return int(resp.headers.get('Content-Length') or 0)
    except Exception as e:
        logger.warning('探测 DBeaver 安装包大小失败: %s', e)
        return 0


def _download_once(url: str, target_path: pathlib.Path, total: int, on_progress=None) -> int:
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    downloaded = 0
    chunk_size = 1024 * 256
    with urllib.request.urlopen(req, timeout=600) as resp, open(target_path, 'wb') as f:
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


def download_dbeaver(url: str | list[str], filename: str, on_progress=None) -> pathlib.Path:
    DBEAVER_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = DBEAVER_DOWNLOAD_DIR / filename

    urls = [url] if isinstance(url, str) else url
    if not urls:
        raise DBeaverError('没有可用的下载地址')

    total = _probe_remote_size(urls[0])

    if target.exists() and total and target.stat().st_size >= total:
        if on_progress:
            on_progress(f'检测到已下载的安装包，直接复用: {target}')
        return target

    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass

    last_error = None
    for try_url in urls:
        for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
            if on_progress:
                on_progress(f'下载（源 {urls.index(try_url) + 1}/{len(urls)}，尝试 {attempt}/{DOWNLOAD_MAX_ATTEMPTS}）: {try_url}')
            try:
                probe_total = total or _probe_remote_size(try_url)
                downloaded = _download_once(try_url, target, probe_total, on_progress=on_progress)
                if probe_total and downloaded < probe_total:
                    raise DBeaverError(f'下载不完整：{downloaded}/{probe_total} 字节')
                if on_progress:
                    on_progress(f'下载完成: {target}（{downloaded / 1048576:.1f} MB）')
                return target
            except Exception as e:
                last_error = e
                logger.warning('DBeaver 下载失败 (%s, 第 %d 次): %s', try_url, attempt, e)
                if on_progress:
                    on_progress(f'下载失败: {e}')
                if target.exists():
                    try:
                        target.unlink()
                    except OSError:
                        pass

    raise DBeaverError(f'下载失败，所有源均不可用：{last_error}')


def default_install_dir(installer_type: str = 'exe') -> str:
    return str(DEV_TOOLS_INSTALL_ROOT / 'DBeaver')


def build_install_params_exe(install_dir: str, for_all_users: bool = True) -> str:
    user_flag = '/allusers' if for_all_users else '/currentuser'
    install_dir = str(install_dir).rstrip('\\/')
    return f'/S {user_flag} /D={install_dir}'


def run_silent_install_exe(exe_path: pathlib.Path, install_dir: str, on_progress=None) -> int:
    install_dir = str(install_dir).rstrip('\\/')
    pathlib.Path(install_dir).mkdir(parents=True, exist_ok=True)
    params = build_install_params_exe(install_dir, for_all_users=True)
    if on_progress:
        on_progress(f'正在以管理员权限静默安装到: {install_dir}')
        on_progress('若弹出 UAC 对话框，请点击"是"授权。')

    import threading
    import time
    result_holder = {'code': None, 'error': None}

    def _do_install():
        try:
            result_holder['code'] = run_elevated(str(exe_path), params, _INSTALL_TIMEOUT, show=0)
        except Exception as exc:
            result_holder['error'] = exc

    t = threading.Thread(target=_do_install, daemon=True)
    t.start()

    if on_progress:
        on_progress('安装进行中，请稍候...')
        dots = 0
        last_seen = set()
        while t.is_alive():
            t.join(2)
            if not t.is_alive():
                break
            dots = (dots + 1) % 4
            dot_str = '.' * dots + ' ' * (3 - dots)

            dir_files = set()
            try:
                root = pathlib.Path(install_dir)
                if root.exists():
                    dir_files = {str(p) for p in root.rglob('*') if p.is_file()}
            except OSError:
                pass

            new_count = len(dir_files - last_seen)
            if new_count > 0:
                on_progress(f'安装中{dot_str} 已写入 {len(dir_files)} 个文件（+{new_count}）')
            else:
                on_progress(f'安装中{dot_str} 已写入 {len(dir_files)} 个文件')
            last_seen = dir_files

    if result_holder['error']:
        raise result_holder['error']
    code = result_holder['code']
    if code is not None and on_progress:
        on_progress(f'安装进程已结束，退出码: {code}')
    return code if code is not None else -1


def _find_dbeaver_exe(install_dir: str):
    base = pathlib.Path(install_dir)
    candidates = [
        base / 'dbeaver.exe',
        base / 'dbeaver' / 'dbeaver.exe',
        base / 'plugins' / 'org.jkiss.dbeaver.core.application' / 'dbeaver.exe',
    ]
    for c in candidates:
        if c.exists():
            return c
    for c in base.rglob('dbeaver.exe'):
        return c
    return None


def dbeaver_detect_installed():
    import winreg

    def get_value(key, name, default=""):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except (FileNotFoundError, OSError):
            return default

    reg_key_list = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    dbeaver = None

    for root, base_path in reg_key_list:
        try:
            with winreg.OpenKey(root, base_path) as key:
                subkey_count = winreg.QueryInfoKey(key)[0]
                for i in range(subkey_count):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                    except OSError:
                        continue
                    if "DBeaver" not in subkey_name:
                        continue
                    try:
                        full_path = base_path + "\\" + subkey_name
                        with winreg.OpenKey(root, full_path) as subkey:
                            display_name = get_value(subkey, "DisplayName")
                            if "DBeaver" not in display_name:
                                continue
                            install_location = get_value(subkey, "InstallLocation")
                            dbeaver = {
                                'installType': 'exe',
                                'installTypeLabel': INSTALLER_LABELS['exe'],
                                'supportsUpdate': True,
                                'version': get_value(subkey, "DisplayVersion"),
                                'DisplayName': display_name,
                                'DisplayVersion': get_value(subkey, "DisplayVersion"),
                                'InstallLocation': install_location,
                                'UninstallString': get_value(subkey, "UninstallString"),
                                'QuietUninstallString': get_value(subkey, "QuietUninstallString"),
                            }
                            break
                    except OSError as e:
                        logger.warning("打开 DBeaver 子键失败: %s", e)
                        continue
        except OSError as e:
            logger.warning("打开注册表路径失败: %s", e)
            continue

        if dbeaver:
            break

    if dbeaver:
        logger.info("找到 DBeaver 安装信息: %s", dbeaver.get("DisplayName", dbeaver.get("InstallLocation")))
        _dbeaver_store.write(dbeaver)
    else:
        logger.info("没有找到 DBeaver 安装信息")
        _dbeaver_store.write({})

    return dbeaver


def get_dbeaver_options(refresh: bool = False):
    cache = _dbeaver_store.read()
    if refresh or not cache:
        dbeaver_detect_installed()
        cache = _dbeaver_store.read()
    return cache


def _unregister_install():
    _dbeaver_store.write({})