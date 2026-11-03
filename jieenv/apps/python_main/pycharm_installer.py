import os
import logging
import pathlib
import urllib.request
from typing import Optional

from jiecore.json_storage import JsonStore
from jiecore.system_ops import run_elevated
from jiecore.utils import format_size

from .config import (PYCHARM_INSTALLED_SOURCES_FILE, PYCHARM_DOWNLOAD_DIR,
                     PYCHARM_SILENT_CONFIG_FILE, DEV_TOOLS_INSTALL_ROOT)
from .pycharm_versions import BROWSER_HEADERS, get_pycharm_versions

logger = logging.getLogger(__name__)


_pycharm_store = JsonStore(PYCHARM_INSTALLED_SOURCES_FILE, default={})

# 静默安装耗时较长（解压 + 写注册表 + 生成共享归档），给足超时
_PYCHARM_INSTALL_TIMEOUT = 900

# 探测远端大小的独立重试次数（HEAD 请求偶发抖动很常见）
_PROBE_MAX_ATTEMPTS = 2
# 下载失败最大尝试次数（含首次）
_DOWNLOAD_MAX_ATTEMPTS = 3


class PythonPycharmError(Exception):
    pass


def default_install_dir(version: str) -> str:
    """根据版本号生成 PyCharm 默认安装目录"""
    return str(DEV_TOOLS_INSTALL_ROOT / f'PyCharm {version}')


def find_version_entry(version: str) -> Optional[dict]:
    """从版本缓存中按版本号定位下载条目，找不到返回 None。

    缓存结构为 versions[mainVersion][majorVersion][version] = entry。
    """
    if not version:
        return None
    data = get_pycharm_versions() or {}
    versions = data.get('versions') or {}
    for major_map in versions.values():
        for sub_map in (major_map or {}).values():
            entry = (sub_map or {}).get(version)
            if entry:
                return entry
    return None


def _probe_remote_size(url: str) -> int:
    """通过 HEAD 探测远端文件大小，失败自动重试；最终失败返回 0。"""
    last_err = None
    for attempt in range(1, _PROBE_MAX_ATTEMPTS + 1):
        try:
            head = urllib.request.Request(url, headers=BROWSER_HEADERS, method='HEAD')
            with urllib.request.urlopen(head, timeout=15) as resp:
                size = int(resp.headers.get('Content-Length') or 0)
            if size:
                return size
            last_err = '服务器未返回 Content-Length'
        except Exception as e:
            last_err = e
            logger.warning('探测 PyCharm 安装包大小失败（第 %d/%d 次）: %s',
                           attempt, _PROBE_MAX_ATTEMPTS, e)
    logger.warning('探测 PyCharm 安装包大小最终失败: %s', last_err)
    return 0


def _download_pycharm_once(url: str, exe_path: pathlib.Path,
                           on_progress=None) -> pathlib.Path:
    """单次下载尝试：探测大小 -> 复用或下载 -> 完整性校验。"""
    total = _probe_remote_size(url)

    if on_progress:
        if total:
            on_progress(f'安装包大小共: {format_size(total)}')
        else:
            on_progress('未能探测到安装包大小，将直接下载（暂无进度百分比）。')

    # 已下载完整则直接复用
    if exe_path.exists() and total and exe_path.stat().st_size >= total:
        if on_progress:
            on_progress(f'检测到已下载的安装包，直接复用: {exe_path}')
        return exe_path

    # 关键点：只有当"明确知道本地文件不完整"或"本地无文件"时，才直接写到 exe_path；
    # 若本地已有文件但拿不到远端大小，避免误删完整包，先下到 .part 再原子替换。
    use_temp = exe_path.exists() and not total
    target = exe_path.with_suffix(exe_path.suffix + '.part') if use_temp else exe_path

    if not use_temp and exe_path.exists():
        try:
            exe_path.unlink()
        except OSError:
            pass

    if on_progress:
        on_progress(f'开始下载: {url}')

    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    downloaded = 0
    chunk_size = 1024 * 256
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            # HEAD 没拿到大小时，从 GET 响应头补一次
            if not total:
                total = int(resp.headers.get('Content-Length') or 0)
                if total and on_progress:
                    on_progress(f'安装包大小共: {format_size(total)}')

            with open(target, 'wb') as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total and on_progress:
                        pct = int(downloaded * 100 / total)
                        on_progress(f'下载中 {pct}% ({format_size(downloaded)}/{format_size(total)})')

        if total and downloaded < total:
            raise PythonPycharmError(f'下载不完整: {format_size(downloaded)}/{format_size(total)}')

        if use_temp:
            os.replace(target, exe_path)  # 原子替换，原文件在此之前始终可用

        if on_progress:
            on_progress(f'下载完成: {exe_path}（{format_size(downloaded)}）')
        return exe_path
    except Exception:
        # 只清理本次产生的临时文件，绝不动 exe_path
        if use_temp and target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        raise


def download_pycharm(url: str, filename: str, on_progress=None) -> pathlib.Path:
    """下载 PyCharm 安装包到 tmp 下载目录，返回本地文件路径。

    失败自动重试至多 _DOWNLOAD_MAX_ATTEMPTS 次（共 3 次尝试）；
    全部失败则抛出 PythonPycharmError。
    """
    PYCHARM_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    exe_path = PYCHARM_DOWNLOAD_DIR / filename

    last_error: Optional[Exception] = None
    for attempt in range(1, _DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            return _download_pycharm_once(url, exe_path, on_progress=on_progress)
        except Exception as e:
            last_error = e
            logger.warning('PyCharm 安装包下载失败（第 %d/%d 次尝试）: %s',
                           attempt, _DOWNLOAD_MAX_ATTEMPTS, e)
            if on_progress:
                on_progress(f'下载失败（第 {attempt}/{_DOWNLOAD_MAX_ATTEMPTS} 次）: {e}')
                if attempt < _DOWNLOAD_MAX_ATTEMPTS:
                    on_progress(f'准备重试下载（{attempt + 1}/{_DOWNLOAD_MAX_ATTEMPTS}）...')

    logger.error('PyCharm 安装包下载连续失败 %d 次，放弃。', _DOWNLOAD_MAX_ATTEMPTS)
    raise PythonPycharmError(
        f'PyCharm 安装包下载失败，已重试 {_DOWNLOAD_MAX_ATTEMPTS} 次：{last_error}'
    ) from last_error


def run_silent_install(exe_path, install_dir: str, on_progress=None) -> int:
    """以管理员权限静默安装 PyCharm，返回提权进程退出码。

    PyCharm installer（NSIS）静默参数：/S 静默、/CONFIG 指定配置文件、
    /D 指定安装目录。/D 必须是最后一个参数且路径不能带引号，否则会被
    installer 当成安装目录名的一部分。
    """
    install_dir = str(install_dir).rstrip('\\/')
    pathlib.Path(install_dir).mkdir(parents=True, exist_ok=True)
    config_file = str(PYCHARM_SILENT_CONFIG_FILE)
    if not os.path.exists(config_file):
        raise PythonPycharmError(f'静默安装配置文件不存在: {config_file}')

    params = f'/S /CONFIG="{config_file}" /D={install_dir}'
    if on_progress:
        on_progress(f'正在以管理员权限静默安装到: {install_dir}')
        on_progress('若弹出 UAC 对话框，请点击“是”授权。')
        on_progress('安装过程可能需要几分钟，请耐心等待...')
    # show=0：静默安装本身无界面，进度由前端 JieTaskLog 日志窗展示
    return run_elevated(str(exe_path), params, _PYCHARM_INSTALL_TIMEOUT, show=0)


def pycharm_detect_installed():
    import winreg

    def get_value(key, name, default=""):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return default
        except OSError:
            return default

    reg_key_list = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    pycharm = None

    for root, base_path in reg_key_list:
        try:
            with winreg.OpenKey(root, base_path) as key:
                subkey_count = winreg.QueryInfoKey(key)[0]
                for i in range(subkey_count):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                    except OSError:
                        continue

                    if "PyCharm" not in subkey_name:
                        continue

                    try:
                        full_path = base_path + "\\" + subkey_name
                        with winreg.OpenKey(root, full_path) as subkey:
                            display_name = get_value(subkey, "DisplayName")
                            if "PyCharm" not in display_name:
                                continue
                            install_location = get_value(subkey, "InstallLocation")
                            pycharm_info_file = os.path.join(install_location, "product-info.json")
                            if os.path.exists(pycharm_info_file):
                                pycharm_info = JsonStore(pycharm_info_file, default={}).read()
                                logger.info("找到 PyCharm 安装信息文件: %s", pycharm_info_file)
                            else:
                                pycharm_info = {}
                                logger.warning("没有找到 PyCharm 安装信息: %s", pycharm_info_file)
                            pycharm = {
                                'installType': 'installer',
                                'version': pycharm_info.get('version', ''),
                                'dataDirectoryName': pycharm_info.get('dataDirectoryName', ''),
                                'DisplayName': display_name,
                                'DisplayVersion': get_value(subkey, "DisplayVersion"),
                                'InstallLocation': install_location,
                                'UninstallString': get_value(subkey, "UninstallString"),
                                'QuietUninstallString': get_value(subkey, "QuietUninstallString"),
                            }
                            break
                    except OSError as e:
                        logger.warning("打开 PyCharm 子键失败: %s", e)
                        continue
        except OSError as e:
            logger.warning("打开注册表路径失败: %s", e)
            continue

        if pycharm:
            break

    if pycharm:
        logger.info("找到 PyCharm 注册表信息: %s", pycharm.get("DisplayName"))
        _pycharm_store.write(pycharm)
    else:
        logger.info("没有找到 PyCharm 注册表信息")
        _pycharm_store.write({})

    return pycharm


def get_pycharm_options(refresh=False):
    cache = _pycharm_store.read()
    if refresh or not cache:
        pycharm_detect_installed()
        cache = _pycharm_store.read()
    return cache