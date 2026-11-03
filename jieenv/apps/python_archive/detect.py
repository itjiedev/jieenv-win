"""
探测指定文件夹中的 Python 环境。

python_archive 使用 zip 压缩包解压安装 Python，不会注册到 Windows 注册表，
因此本模块通过用户指定的文件夹来发现 Python 环境：

1. 检查指定文件夹中是否存在 python.exe
2. 执行 python --version 获取精确版本号
3. 测试 pip 是否可用

对外接口::

    from apps.python_archive.detect import detect_folder, import_folder

    detect_folder(folder_path)  -> {'success': True, 'version': '3.13.5', 'python_exe': '...', 'pip_available': True}
    import_folder(folder_path)  -> {'success': True, 'message': '导入成功', 'version': '3.13.5'}
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

_VERSION_RE = re.compile(r'Python\s+(\d+\.\d+\.\d+)')


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
            [exe, '--version'],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            return ''
        out = (proc.stdout or proc.stderr or b'').decode('utf-8', errors='replace').strip()
        m = _VERSION_RE.search(out)
        if m:
            return m.group(1)
    except subprocess.TimeoutExpired:
        logger.debug('探测解释器版本超时: %s', exe)
    except Exception as e:
        logger.debug('探测解释器版本失败 %s: %s', exe, e)
    return ''


def _check_pip(exe: str) -> bool:
    """检查解释器的 pip 是否可用。"""
    if not exe:
        return False
    exe = os.path.abspath(exe)
    if not os.path.isfile(exe):
        return False

    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP")}

    try:
        proc = subprocess.run(
            [exe, "-m", "pip", "-V"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,          # 建议 >= 10
            env=env,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as e:
        logger.debug('_check_pip %s failed: %s', exe, e)
        return False

    if proc.returncode != 0:
        logger.debug('_check_pip %s rc=%s stderr=%s stdout=%s',
                     exe, proc.returncode, proc.stderr, proc.stdout)
        return False
    return b"pip" in (proc.stdout or b"").lower()


def _find_python_exe(folder: Path) -> Path:
    """在目录中查找 Python 可执行文件。

    查找优先级：
    1. python.exe（标准版本）
    2. python{major}.{minor}t.exe（free-threaded 版本，如 python3.13t.exe）
    3. python*t.exe（其他 free-threaded 模式匹配）
    """
    # 1. 查找标准 python.exe
    python_exe = folder / 'python.exe'
    if python_exe.exists():
        return python_exe

    # 2. 查找 free-threaded 版本：python{major}.{minor}t.exe
    for exe in folder.glob('python*.t.exe'):
        name = exe.name
        # 匹配 python3.13t.exe 这样的模式
        if name.startswith('python') and 't.exe' in name:
            return exe

    # 3. 兜底：查找任何 python*t.exe
    for exe in folder.glob('python*t.exe'):
        return exe

    return None


def detect_folder(folder_path: str) -> dict:
    """检测指定文件夹中的 Python 环境。

    返回 {'success': True, 'version': '3.13.5', 'python_exe': '...', 'pip_available': True}
    或 {'success': False, 'message': '未找到 python.exe'}
    """
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        return {'success': False, 'message': f'目录不存在: {folder_path}'}

    chosen = _find_python_exe(folder)
    if chosen is None:
        return {'success': False, 'message': '未找到 python.exe 或 python*t.exe'}

    # 获取版本号
    version = _probe_version(str(chosen))
    if not version:
        return {'success': False, 'message': '无法获取 Python 版本'}

    # 检查 pip
    pip_available = _check_pip(str(chosen))

    return {
        'success': True,
        'version': version,
        'python_exe': str(chosen),
        'install_dir': str(folder),
        'pip_available': pip_available,
    }


def import_folder(folder_path: str) -> dict:
    """将指定文件夹中的 Python 环境导入安装记录。

    返回 {'success': True, 'message': '导入成功', 'version': '3.13.5'}
    """
    # 检查是否已导入过该目录（避免重复导入）
    normalized_path = str(Path(folder_path).resolve())
    for record in storage.list_installed():
        existing_dir = str(Path(record.get('install_dir', '')).resolve())
        if existing_dir == normalized_path:
            return {
                'success': False,
                'message': f'该目录已导入过：{record.get("name", "")}（版本 {record.get("version", "")}）'
            }

    # 先检测
    detect_result = detect_folder(folder_path)
    if not detect_result.get('success'):
        return detect_result

    # python.exe 或 pip 任一异常则不导入
    if not detect_result.get('pip_available'):
        return {'success': False, 'message': 'pip 不可用，无法导入该 Python 环境'}

    version = detect_result['version']
    folder_path_name = Path(folder_path).name
    python_exe = detect_result['python_exe']

    extra_meta = {}
    try:
        import platform as _plat
        proc = subprocess.run(
            [python_exe, '-c',
             'import platform,sysconfig;'
             'print("ARCH="+platform.machine());'
             'print("FT="+str(sysconfig.get_config_var("Py_GIL_DISABLED") or 0))'],
            capture_output=True, timeout=_PROBE_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode == 0:
            out = (proc.stdout or b'').decode('utf-8', errors='replace')
            for line in out.splitlines():
                if line.startswith('ARCH='):
                    m = line[5:].strip().lower()
                    if 'amd64' in m or 'x86_64' in m:
                        extra_meta['arch'] = 'amd64'
                    elif 'arm64' in m or 'aarch64' in m:
                        extra_meta['arch'] = 'arm64'
                    elif 'x86' in m or 'i386' in m or 'i686' in m:
                        extra_meta['arch'] = 'x86'
                    else:
                        extra_meta['arch'] = m or ''
                elif line.startswith('FT='):
                    extra_meta['free_threaded'] = line[3:].strip() == '1'
    except Exception:
        pass

    # 生成名称：默认使用 Python<version>，如果已存在则加数字后缀
    # base_name = f'Python {version}'
    name = storage.generate_unique_name(folder_path_name)

    # 添加记录
    record = {
        'name': name,
        'version': version,
        'install_dir': detect_result['install_dir'],
        'python_exe': python_exe,
        'pip_available': detect_result['pip_available'],
        'status': 'available',
        'imported': True,  # 标记为导入的记录
    }
    record.update({k: v for k, v in extra_meta.items() if v})
    storage.add_installed(record)

    return {
        'success': True,
        'message': f'成功导入 {name}',
        'name': name,
        'version': version,
    }