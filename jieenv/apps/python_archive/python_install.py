"""
Python 压缩包方式下载与安装

安装流程：
1. 从镜像源或官网下载 zip 压缩包；
2. 解压到指定安装目录；
3. 将环境信息登记到 python_archive 的 storage。

安装选项（options）:
    version_id       版本缓存中的 id（如 python-3-13-5-amd64-zip）
    install_dir      安装目录（默认 PYTHON_INSTALL_DIR/python_archive/Python<version>）
    download_source  本次临时指定的下载源名（'auto'/None 表示走默认/自动）
"""
import os
import shutil
import logging
import zipfile
import urllib.request
from pathlib import Path

from django.conf import settings

from . import storage
from . import storage_venv
from . import python_download
from .config import DOWNLOAD_DIR, PYTHON_INSTALL_DIR, TMP_DIR
from .python_download import DOWNLOAD_HEADERS

logger = logging.getLogger(__name__)


class InstallError(Exception):
    """安装过程可预期错误，消息直接展示给用户"""


def get_default_install_dir(version_id: str) -> str:
    """根据版本 id 自动生成默认安装目录（从应用配置读取基础目录）

    将 id 中的中划线替换为下划线，最后一段替换为 _archive，如：
    python-3-14-8-amd64-zip -> python_3_14_8_amd64_archive
    """
    # 将 id 中的 - 替换为 _
    folder_name = version_id.replace('-', '_')
    # 将最后一段（如 _zip）替换为 _archive
    parts = folder_name.rsplit('_', 1)
    folder_name = parts[0] + '_archive' if len(parts) == 2 else folder_name + '_archive'

    base_dir = Path(storage.load_config().get('install_dir') or str(PYTHON_INSTALL_DIR))
    return str(base_dir / folder_name)


def _download_zip(url: str, filename: str, on_progress=None) -> Path:
    """下载 zip 压缩包，返回本地文件路径"""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DOWNLOAD_DIR / filename

    # 先用 HEAD 获取文件大小
    req = urllib.request.Request(url, headers=DOWNLOAD_HEADERS, method='HEAD')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
    except Exception:
        total = 0

    # 已下载完整则直接复用
    if zip_path.exists() and total and zip_path.stat().st_size >= total:
        if on_progress:
            on_progress(f'检测到已下载的压缩包，直接复用: {zip_path}')
        return zip_path
    if zip_path.exists():
        zip_path.unlink()

    if on_progress:
        on_progress(f'开始下载: {url}')

    req = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
    resp = urllib.request.urlopen(req, timeout=300)
    downloaded = 0
    chunk_size = 1024 * 256
    with open(zip_path, 'wb') as f:
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
        on_progress(f'下载完成: {zip_path}（{downloaded / 1024 / 1024:.1f} MB）')
    return zip_path


def _find_python_exe(install_dir: Path) -> Path:
    """在安装目录中查找 Python 可执行文件。

    查找优先级：
    1. python.exe（标准版本）
    2. python{major}.{minor}t.exe（free-threaded 版本，如 python3.13t.exe）
    3. python*t.exe（其他 free-threaded 模式匹配）
    """
    # 1. 查找标准 python.exe
    for base in (install_dir, install_dir / 'Scripts'):
        candidate = base / 'python.exe'
        if candidate.exists():
            return candidate

    # 2. 查找 free-threaded 版本：python{major}.{minor}t.exe
    for base in (install_dir, install_dir / 'Scripts'):
        for exe in base.glob('python*.t.exe'):
            name = exe.name
            # 匹配 python3.13t.exe 这样的模式
            if name.startswith('python') and 't.exe' in name:
                return exe

    # 3. 兜底：查找任何 python*t.exe
    for base in (install_dir, install_dir / 'Scripts'):
        for exe in base.glob('python*t.exe'):
            return exe

    return None


def _extract_zip(zip_path: Path, install_dir: str, on_progress=None) -> dict:
    """解压 zip/nupkg 到安装目录，返回 {python_exe}

    对于 .nupkg 格式，先解压到临时目录，检查里面是否有 tools 目录：
    - 如果有 tools 目录，将 tools 里的所有文件复制到安装目录
    - 如果没有 tools 目录，将临时目录里的所有文件复制到安装目录
    """
    install_dir = Path(install_dir)

    # 安装前检查目标文件夹是否已存在同名
    if install_dir.exists() and any(install_dir.iterdir()):
        raise InstallError(
            f'安装目录已存在且有内容: {install_dir}\n'
            f'请先删除该目录或选择其他安装路径'
        )

    install_dir.mkdir(parents=True, exist_ok=True)

    # 判断是否为 nupkg 格式
    is_nupkg = zip_path.suffix.lower() == '.nupkg'

    if is_nupkg:
        # nupkg 格式：先解压到项目临时目录
        import uuid
        tmp_path = TMP_DIR / f'nupkg_extract_{uuid.uuid4().hex[:8]}'
        try:
            tmp_path.mkdir(parents=True, exist_ok=True)
            if on_progress:
                on_progress(f'检测到 nupkg 格式，解压到临时目录: {tmp_path}')

            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    file_list = zf.namelist()
                    total_files = len(file_list)
                    extracted = 0
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        zf.extract(info, tmp_path)
                        extracted += 1
                        if on_progress and extracted % 100 == 0:
                            pct = int(extracted * 100 / total_files) if total_files else 0
                            on_progress(f'解压中 {pct}% ({extracted}/{total_files} 文件)')

                if on_progress:
                    on_progress(f'解压完成: {extracted} 个文件')

            except zipfile.BadZipFile as e:
                raise InstallError(f'压缩包损坏，请重新下载: {e}')
            except Exception as e:
                raise InstallError(f'解压失败: {e}')

            # 检查是否有 tools 目录
            tools_dir = tmp_path / 'tools'
            source_dir = tools_dir if tools_dir.exists() and tools_dir.is_dir() else tmp_path

            if on_progress:
                if tools_dir.exists():
                    on_progress(f'检测到 tools 目录，从 tools 复制文件到安装目录')
                else:
                    on_progress(f'未检测到 tools 目录，从根目录复制文件到安装目录')

            # 复制文件到安装目录
            _copy_tree(source_dir, install_dir, on_progress)
        finally:
            # 清理临时目录
            if tmp_path.exists():
                try:
                    shutil.rmtree(tmp_path)
                    if on_progress:
                        on_progress(f'已清理临时目录: {tmp_path}')
                except Exception as e:
                    logger.warning('清理临时目录失败 %s: %s', tmp_path, e)
    else:
        # 标准 zip 格式：直接解压到安装目录
        if on_progress:
            on_progress(f'开始解压到: {install_dir}')

        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                file_list = zf.namelist()
                total_files = len(file_list)
                extracted = 0
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    zf.extract(info, install_dir)
                    extracted += 1
                    if on_progress and extracted % 100 == 0:
                        pct = int(extracted * 100 / total_files) if total_files else 0
                        on_progress(f'解压中 {pct}% ({extracted}/{total_files} 文件)')

            if on_progress:
                on_progress(f'解压完成: {extracted} 个文件')

        except zipfile.BadZipFile as e:
            raise InstallError(f'压缩包损坏，请重新下载: {e}')
        except Exception as e:
            raise InstallError(f'解压失败: {e}')

    # 定位 python.exe
    python_exe = _find_python_exe(install_dir)

    if python_exe is None:
        raise InstallError(
            f'解压完成但未找到 python.exe 或 python*t.exe，请检查压缩包内容: {install_dir}'
        )

    if on_progress:
        on_progress(f'安装成功: {python_exe}')

    return {
        'python_exe': str(python_exe),
    }


def _copy_tree(src_dir: Path, dst_dir: Path, on_progress=None) -> None:
    """将源目录中的所有文件和子目录复制到目标目录。"""
    copied = 0
    for item in src_dir.iterdir():
        dst_item = dst_dir / item.name
        if item.is_dir():
            if dst_item.exists():
                shutil.rmtree(dst_item)
            shutil.copytree(item, dst_item)
            # 统计复制的文件数
            for root, _dirs, files in os.walk(item):
                copied += len(files)
        else:
            shutil.copy2(item, dst_item)
            copied += 1

        if on_progress and copied % 100 == 0:
            on_progress(f'复制中... ({copied} 个文件)')

    if on_progress:
        on_progress(f'复制完成: {copied} 个文件')


def run_install(options: dict, on_progress=None) -> dict:
    """
    执行完整安装流程（下载 + 解压 + 登记）。

    options: {
        version_id,       # 版本缓存中的 id
        version,          # 版本号（如 3.13.5）
        name,             # 环境名称（必填）
        install_dir,      # 安装目录
        download_source,  # 本次临时指定的下载源名
        zip_url,          # 可选，显式直链
    }

    返回 {success, python_exe, install_dir, version, download_source, zip_url}
    """
    version_id = options['version_id']
    version = options['version']
    name = options['name']
    install_dir = options.get('install_dir') or get_default_install_dir(version_id)

    # 安装前检查目标文件夹是否已存在同名
    install_path = Path(install_dir)
    if install_path.exists() and any(install_path.iterdir()):
        raise InstallError(
            f'安装目录已存在且有内容: {install_dir}\n'
            f'请先删除该目录或选择其他安装路径'
        )

    # 1. 解析下载源与直链
    explicit_url = (options.get('zip_url') or '').strip()
    if explicit_url:
        zip_url = explicit_url
        source_info = {'name': '显式直链', 'url': ''}
        if on_progress:
            on_progress(f'使用显式直链：{zip_url}')
    else:
        resolved = python_download.resolve_download(
            version_id,
            preferred_name=options.get('download_source'),
            on_progress=on_progress,
        )
        zip_url = resolved['download_url']
        source_info = resolved['source']

    # 2. 从 URL 提取文件名
    from urllib.parse import urlparse, unquote
    url_path = urlparse(zip_url).path
    filename = unquote(url_path.rsplit('/', 1)[-1]) if '/' in url_path else unquote(url_path)
    if not filename:
        filename = f'python-{version}-amd64.zip'

    # 3. 下载压缩包
    zip_path = _download_zip(zip_url, filename, on_progress)

    # 4. 解压安装
    result = _extract_zip(zip_path, install_dir, on_progress)

    # 5. 登记到 python_archive 的存储
    record = {
        'name': name,
        'version': version,
        'version_id': version_id,
        'install_dir': str(install_dir),
        'python_exe': result['python_exe'],
        'download_source': source_info.get('name', ''),
        'zip_path': str(zip_path),
        'status': 'available',
    }
    for meta_key in ('arch', 'free_threaded', 'with_tests', 'implements_pep'):
        if meta_key in options and options[meta_key] not in (None, '', False):
            record[meta_key] = options[meta_key]
    storage.add_installed(record)

    return {
        'success': True,
        'version': version,
        'python_exe': result['python_exe'],
        'install_dir': str(install_dir),
        'download_source': source_info.get('name', ''),
        'zip_url': zip_url,
    }


def run_uninstall(record_id: str, on_progress=None) -> dict:
    """
    卸载指定 ID 的 Python（压缩包模式）。

    卸载流程：
    1. 清理 PATH 环境变量中该版本的路径
    2. 删除安装文件夹
    3. 清除 storage 记录

    返回 {success, message}
    """
    record = storage.get_by_id(record_id)
    if not record:
        return {'success': False, 'message': f'未找到 ID 为 {record_id} 的安装记录'}

    name = record.get('name', '')
    version = record.get('version', '')
    install_dir = record.get('install_dir', '')

    # 1. 清理 PATH 环境变量
    if install_dir:
        if on_progress:
            on_progress('清理环境变量...')
        try:
            from . import env_ops
            env_ops.remove_from_path(install_dir)
        except Exception as e:
            if on_progress:
                on_progress(f'清理环境变量失败: {e}')
            logger.warning('清理环境变量失败: %s', e)

    # 2. 删除安装目录
    if install_dir and Path(install_dir).exists():
        if on_progress:
            on_progress(f'删除安装目录: {install_dir}')
        try:
            shutil.rmtree(install_dir, ignore_errors=True)
        except Exception as e:
            if on_progress:
                on_progress(f'删除目录失败: {e}')

    # 3. 级联清除归属该实例的虚拟环境记录（磁盘目录不强制删，留给用户处理）
    venv_cleaned = storage_venv.clear_by_owner(record_id)
    if venv_cleaned > 0 and on_progress:
        on_progress(f'已级联清除 {venv_cleaned} 条虚拟环境管理记录')

    # 4. 清除记录
    storage.remove_installed(record_id)

    if on_progress:
        on_progress(f'{name} 卸载完成')
    return {'success': True, 'message': f'{name} 已卸载'}