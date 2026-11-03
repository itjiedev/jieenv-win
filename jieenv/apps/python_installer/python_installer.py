"""
Python Installer 自动下载与安装（Windows）。

安装流程（前端阻塞式同步执行）：
1. 从官网下载 Windows Installer (exe)；
2. 执行静默安装到指定目录；
3. 将环境信息登记到 python 应用的 storage。

安装选项（options）:
    version        版本号，如 3.13.5
    msi_url        exe 安装包下载地址
    install_dir    安装目录（默认 PYTHON_INSTALL_DIR/Python<version>）
    add_to_path    是否添加到 PATH（默认 False）
    install_pip    是否安装 pip（默认 True）
"""
import os
import re
import time
import shutil
import logging
import subprocess
import urllib.request
from pathlib import Path

from django.conf import settings

from . import storage
from . import python_download
from .config import DOWNLOAD_DIR, PYTHON_INSTALL_DIR, TMP_DIR
from .python_download import DOWNLOAD_HEADERS

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


class InstallError(Exception):
    """安装过程可预期错误，消息直接展示给用户"""


def _get_default_install_dir(version: str) -> str:
    """根据版本号自动生成默认安装目录（从应用配置读取基础目录）"""
    from pathlib import Path
    base_dir = Path(storage.load_config().get('install_dir') or str(PYTHON_INSTALL_DIR))
    return str(base_dir / f'Python{version}_installer')


def _download_launcher(url: str, version: str, on_progress=None) -> Path:
    """下载 py launcher MSI 安装包，返回本地文件路径。"""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f'launcher-{version}.msi'
    msi_path = DOWNLOAD_DIR / filename

    # 已下载完整则直接复用
    req = urllib.request.Request(url, headers=DOWNLOAD_HEADERS, method='HEAD')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
    except Exception:
        total = 0

    if msi_path.exists() and total and msi_path.stat().st_size >= total:
        if on_progress:
            on_progress(f'检测到已下载的 launcher 安装包，直接复用: {msi_path}')
        return msi_path
    if msi_path.exists():
        msi_path.unlink()

    if on_progress:
        on_progress(f'开始下载 py launcher: {url}')

    req = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
    resp = urllib.request.urlopen(req, timeout=300)
    downloaded = 0
    chunk_size = 1024 * 256
    with open(msi_path, 'wb') as f:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total and on_progress:
                pct = int(downloaded * 100 / total)
                kb = downloaded / 1024
                total_kb = total / 1024
                on_progress(f'下载中 {pct}% ({kb:.1f}/{total_kb:.1f} KB)')

    if on_progress:
        on_progress(f'下载完成: {msi_path}（{downloaded / 1024:.1f} KB）')
    return msi_path


def _install_launcher_msi(msi_path: Path, on_progress=None) -> dict:
    """使用 msiexec 安装 py launcher MSI 包。

    返回 {success, message}
    """
    log_file = str(Path(TMP_DIR) / 'py_launcher_diag' / 'msi_install.log')
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        'msiexec', '/i', str(msi_path),
        '/passive',
        '/norestart',
        f'/log', log_file,
    ]

    if on_progress:
        on_progress('正在安装 py launcher（MSI 模式）...')

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,
            creationflags=_CREATE_NO_WINDOW,
        )
        out = (proc.stdout or b'').decode('utf-8', errors='replace')[:500]
        err = (proc.stderr or b'').decode('utf-8', errors='replace')[:500]

        if proc.returncode not in (0, 1638):
            # 读取 MSI 日志末尾获取实际错误原因
            log_tail = ''
            try:
                with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                    log_tail = ''.join(lines[-20:]).strip()
            except OSError:
                pass
            msg = f'MSI 安装失败（退出码 {proc.returncode}）'
            if log_tail:
                msg += f'\n日志: {log_tail[:300]}'
            elif err:
                msg += f'\n{err}'
            raise InstallError(msg)

    except subprocess.TimeoutExpired:
        raise InstallError('py launcher 安装超时（超过 5 分钟）')

    if on_progress:
        on_progress('py_launcher MSI 安装完成')

    return {'success': True, 'message': 'py launcher 安装完成'}


def _download(url: str, version: str, on_progress=None) -> Path:
    """下载安装包，返回本地文件路径"""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f'python-{version}-amd64.exe'
    exe_path = DOWNLOAD_DIR / filename

    # 已下载完整则直接复用
    req = urllib.request.Request(url, headers=DOWNLOAD_HEADERS, method='HEAD')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
    except Exception:
        total = 0

    if exe_path.exists() and total and exe_path.stat().st_size >= total:
        if on_progress:
            on_progress(f'检测到已下载的安装包，直接复用: {exe_path}')
        return exe_path
    if exe_path.exists():
        exe_path.unlink()

    if on_progress:
        on_progress(f'开始下载: {url}')

    req = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
    resp = urllib.request.urlopen(req, timeout=300)
    downloaded = 0
    chunk_size = 1024 * 256
    with open(exe_path, 'wb') as f:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total and on_progress:
                pct = int(downloaded * 100 / total)
                mb = downloaded / 1024 / 1024
                total_mb = total / 1024 / 1024
                on_progress(f'下载中 {pct}% ({mb:.1f}/{total_mb:.1f} MB)')

    if on_progress:
        on_progress(f'下载完成: {exe_path}（{downloaded / 1024 / 1024:.1f} MB）')
    return exe_path


def _install_exe(exe_path: Path, install_dir: str, add_to_path: bool = False,
                 install_pip: bool = True, install_py: bool = True, on_progress=None) -> dict:
    """
    执行 Python Installer 静默安装。
    返回 {python_exe, uninstall_exe}
    """
    install_dir = Path(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)

    # 构建静默安装命令
    # Include_pip=1 安装 pip，Include_test=0 不安装测试套件
    # TargetDir 指定安装目录
    cmd = [
        str(exe_path),
        '/passive',           # 静默安装
        f'TargetDir={install_dir}',
        f'Include_pip={1 if install_pip else 0}',
        f'Include_launcher={1 if install_py else 0}',
        f'Include_test=0',
        f'InstallAllUsers=0',
        f'PrependPath={1 if add_to_path else 0}',
    ]

    if on_progress:
        on_progress(f'开始安装到: {install_dir}')
        on_progress('安装过程可能需要几分钟，请耐心等待...')

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=600,
            creationflags=_CREATE_NO_WINDOW,
        )
        out = (proc.stdout or b'').decode('utf-8', errors='replace')
        err = (proc.stderr or b'').decode('utf-8', errors='replace')

        if proc.returncode != 0:
            # 退出码 1638 = 同版本已安装
            if proc.returncode == 1638:
                raise InstallError(
                    f'该版本 Python 已安装（退出码 1638），请先卸载旧版本或选择其他版本')
            raise InstallError(f'安装失败（退出码 {proc.returncode}）:\n{err[:500]}')

    except subprocess.TimeoutExpired:
        raise InstallError('安装超时（超过 10 分钟），请检查系统状态')

    # 定位安装结果
    python_exe = install_dir / 'python.exe'
    if not python_exe.exists():
        # 可能在 Scripts 或根目录
        for candidate in [install_dir / 'python.exe',
                          install_dir / 'Scripts' / 'python.exe']:
            if candidate.exists():
                python_exe = candidate
                break

    # 查找卸载程序
    uninstall_exe = ''
    uninstall_dir = install_dir
    for pattern in ['uninstall.exe', f'python-{_version_no_dots_from_path(install_dir)}-amd64.exe']:
        candidate = uninstall_dir / pattern
        if candidate.exists():
            uninstall_exe = str(candidate)
            break
    # 也检查 InstallPath 下的卸载程序
    if not uninstall_exe:
        for f in install_dir.iterdir():
            if f.name.startswith('uninstall') and f.suffix == '.exe':
                uninstall_exe = str(f)
                break

    if not python_exe.exists():
        raise InstallError(f'安装完成但未找到 python.exe，请检查安装目录: {install_dir}')

    if on_progress:
        on_progress(f'安装成功: {python_exe}')

    return {
        'python_exe': str(python_exe),
        'uninstall_exe': uninstall_exe,
    }


def _version_no_dots_from_path(path: Path) -> str:
    """从路径中提取版本号（去掉点），如 Python3135 -> 3135"""
    name = path.name
    m = re.search(r'Python(\d+)', name)
    return m.group(1) if m else ''


def run_install(options: dict, on_progress=None) -> dict:
    """
    执行完整安装流程。
    options: {
        version, install_dir, add_to_path, install_pip, install_py,
        download_source,   # 可选，本次临时指定的下载源名（'auto'/None 表示走默认/自动）
        msi_url,           # 可选，显式直链；给出时跳过下载源解析（兼容旧调用）
    }
    返回 {success, python_exe, install_dir, version, download_source, exe_url}
    """
    version = options['version']
    install_dir = options.get('install_dir') or _get_default_install_dir(version)
    add_to_path = options.get('add_to_path', False)
    install_pip = options.get('install_pip', True)
    install_py = options.get('install_py', True)

    # 检查是否已安装
    existing = storage.get_installed(version)
    if existing:
        raise InstallError(f'Python {version} 已安装到 {existing.get("install_dir")}，请先卸载')

    # 1. 解析下载源与直链
    #    显式 msi_url 优先（兼容旧调用方），否则走 python_download 的
    #    "临时指定 > 持久化默认 > 自动探测" 解析链
    explicit_url = (options.get('msi_url') or '').strip()
    if explicit_url:
        exe_url = explicit_url
        source_info = {'name': '显式直链', 'url': ''}
        if on_progress:
            on_progress(f'使用显式直链：{exe_url}')
    else:
        resolved = python_download.resolve_download(
            version,
            preferred_name=options.get('download_source'),
            on_progress=on_progress,
        )
        exe_url = resolved['exe_url']
        source_info = resolved['source']

    # 2. 下载安装包
    exe_path = _download(exe_url, version, on_progress)

    # 3. 执行安装
    result = _install_exe(exe_path, install_dir, add_to_path, install_pip, install_py, on_progress)

    # 4. 登记到 python_installer 的存储
    #    尝试从注册表读取更多信息
    from .detect import _read_registry_detail
    reg_info = _read_registry_detail(str(install_dir))
    record = {
        'version': version,
        'install_dir': str(install_dir),
        'python_exe': result['python_exe'],
        'uninstall_exe': result['uninstall_exe'],
        'download_source': source_info.get('name', ''),
        'exe_path': str(exe_path),
        'status': 'available',
    }
    record.update(reg_info)
    storage.add_installed(record)

    return {
        'success': True,
        'version': version,
        'python_exe': result['python_exe'],
        'install_dir': install_dir,
        'download_source': source_info.get('name', ''),
        'exe_url': exe_url,
    }


def run_uninstall(version: str, on_progress=None) -> dict:
    """
    卸载指定版本的 Python。
    返回 {success, message}

    注意：由系统探测导入的记录（imported=True）不是本应用安装的，卸载时仅从
    管理列表移除，绝不物理删除解释器目录，避免误删系统中已有的 Python。
    """
    record = storage.get_installed(version)
    if not record:
        return {'success': False, 'message': f'未找到 Python {version} 的安装记录'}

    # 导入的系统 Python：仅从管理列表移除，不触碰磁盘文件
    if record.get('imported'):
        storage.remove_installed(version)
        if on_progress:
            on_progress(f'已从管理列表移除导入的 Python {version}（未删除系统文件）')
        return {'success': True,
                'message': f'已从管理列表移除 Python {version}（导入的系统 Python 不会被删除）'}

    uninstall_exe = record.get('uninstall_exe', '')
    install_dir = record.get('install_dir', '')

    if uninstall_exe and Path(uninstall_exe).exists():
        if on_progress:
            on_progress(f'执行卸载程序: {uninstall_exe}')
        try:
            proc = subprocess.run(
                [uninstall_exe, '/quiet', '/norestart'],
                capture_output=True,
                timeout=300,
                creationflags=_CREATE_NO_WINDOW,
            )
            if proc.returncode != 0:
                if on_progress:
                    on_progress(f'卸载程序返回退出码 {proc.returncode}')
        except subprocess.TimeoutExpired:
            if on_progress:
                on_progress('卸载超时')
        except Exception as e:
            if on_progress:
                on_progress(f'卸载程序执行失败: {e}')
    else:
        if on_progress:
            on_progress('未找到卸载程序，将手动删除安装目录')

    # 删除安装目录
    if install_dir and Path(install_dir).exists():
        if on_progress:
            on_progress(f'删除安装目录: {install_dir}')
        try:
            shutil.rmtree(install_dir, ignore_errors=True)
        except Exception as e:
            if on_progress:
                on_progress(f'删除目录失败: {e}')

    # 清除记录
    storage.remove_installed(version)

    if on_progress:
        on_progress(f'Python {version} 卸载完成')
    return {'success': True, 'message': f'Python {version} 已卸载'}