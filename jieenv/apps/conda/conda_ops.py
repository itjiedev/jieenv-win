"""
Conda envs 操作封装。

所有命令通过 subprocess 调用 conda.exe，统一使用 creationflags 隐藏窗口。
包列表持久化到 data/conda/env_pkgs/ 目录（每个环境一个 JSON 快照），
首次访问或用户手动刷新时跑 conda list 生成，后续直接读快照。
"""
import os
import json
import re
import time
import hashlib
import logging
import subprocess
from pathlib import Path

from jiecore import cache_handler
from jiecore.utils import get_cmd_encoding
from .config import CREATE_NO_WINDOW, DATA_DIR, DOWNLOAD_DIR


logger = logging.getLogger(__name__)

_TIMEOUT = 30

_ENV_LIST_HEADER = re.compile(r'^#\s*conda environments:|^#\s*base', re.MULTILINE)
_PKGS_DIR = DATA_DIR / 'env_pkgs'


def normalize_env_prefix(p: str) -> str:
    """Windows 下修正带前导 / 或 \\ 的驱动器路径（os.path.normpath 不会处理）。

    '/D:/foo' -> 'D:\\foo'
    '\\D:\\foo' -> 'D:\\foo'
    '\\\\server\\share' -> '\\\\server\\share' (UNC 不动)
    """
    if not p:
        return p
    p = p.strip()
    if p and p[0] in '/\\' and (len(p) < 2 or p[1] not in '/\\'):
        p = p[1:]
    return os.path.normpath(p)


def _ensure_pkgs_dir() -> None:
    _PKGS_DIR.mkdir(parents=True, exist_ok=True)


def _env_pkgs_file(distro_id: str, env_prefix: str) -> Path:
    """发行版 id + 环境路径 md5 → 快照文件路径。"""
    digest = hashlib.md5(os.path.normpath(env_prefix).encode('utf-8')).hexdigest()[:12]
    return _PKGS_DIR / f"{distro_id}.pkgs.{digest}.json"


def _remove_pkg_snapshot(distro_id: str, env_prefix: str) -> None:
    """删除指定环境的包快照文件。"""
    try:
        f = _env_pkgs_file(distro_id, env_prefix)
        if f.is_file():
            f.unlink()
    except OSError:
        pass


def _clean_pkg_snapshots_for_distro(distro_id: str) -> None:
    """发行版被移除时，清理其所有包快照文件。"""
    try:
        for f in _PKGS_DIR.glob(f"{distro_id}.pkgs.*.json"):
            f.unlink()
    except OSError:
        pass


_PKG_SNAPSHOT_TTL = 900


def clean_orphan_pkg_snapshots(distro_id: str, current_env_prefixes: list) -> int:
    """清理该发行版下，对应环境已不存在的孤儿快照文件。

    根据当前真实环境前缀列表算出"应该存在"的快照文件名 md5 集合，
    遍历磁盘上该发行版的所有快照文件，不在集合里的就是孤儿，删掉。
    返回清理数量（用于日志）。
    """
    if not distro_id:
        return 0
    _ensure_pkgs_dir()
    valid_digests = set()
    for prefix in current_env_prefixes:
        digest = hashlib.md5(os.path.normpath(prefix).encode('utf-8')).hexdigest()[:12]
        valid_digests.add(digest)

    removed = 0
    try:
        for f in _PKGS_DIR.glob(f"{distro_id}.pkgs.*.json"):
            name = f.name
            prefix_part = name[len(distro_id) + len('.pkgs.'):]
            digest = prefix_part[:-len('.json')]
            if digest not in valid_digests:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass

    if removed:
        logger.info('clean_orphan_pkg_snapshots: distro=%s removed=%d', distro_id, removed)
    return removed


_TOS_ERROR_MARKER = 'Terms of Service have not been accepted'


def _run_conda(conda_bat: str, args: list, timeout: int = _TIMEOUT) -> dict:
    result = _run_conda_once(conda_bat, args, timeout)

    if (result['returncode'] != 0
            and (_TOS_ERROR_MARKER in result['stderr']
                 or _TOS_ERROR_MARKER in result['stdout'])):
        _auto_accept_tos(conda_bat)
        result = _run_conda_once(conda_bat, args, timeout)

    return result


def _run_conda_once(conda_bat: str, args: list, timeout: int) -> dict:
    cmd = [conda_bat] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            env=_safe_env(),
        )
        enc = get_cmd_encoding()
        return {
            'returncode': proc.returncode,
            'stdout': proc.stdout.decode(enc, errors='replace'),
            'stderr': proc.stderr.decode(enc, errors='replace'),
        }
    except subprocess.TimeoutExpired:
        return {'returncode': -1, 'stdout': '', 'stderr': '执行超时'}
    except FileNotFoundError:
        return {'returncode': -2, 'stdout': '', 'stderr': f'conda 不存在: {cmd[0]}'}
    except Exception as e:
        return {'returncode': -3, 'stdout': '', 'stderr': str(e)}


def _auto_accept_tos(conda_bat: str) -> None:
    try:
        proc = subprocess.run(
            [conda_bat, 'tos', 'accept'],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
            env=_safe_env(),
        )
        if proc.returncode != 0:
            enc = get_cmd_encoding()
            logger.warning('conda tos accept 失败: %s',
                           proc.stderr.decode(enc, errors='replace')[:200])
    except Exception as e:
        logger.warning('conda tos accept 异常: %s', e)


def _safe_env() -> dict:
    env = os.environ.copy()
    env['CONDA_NO_TOS'] = '1'
    for key in list(env.keys()):
        if key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
            env.pop(key, None)
        elif key.startswith('PYCHARM_') or key.startswith('JETBRAINS_'):
            env.pop(key, None)
    return env


def list_envs(conda_bat: str) -> list:
    """列出所有 conda 环境（直接跑 conda env list --json，不缓存）。"""
    result = _run_conda(conda_bat, ['env', 'list', '--json'])
    if result['returncode'] != 0:
        logger.warning('conda env list --json 失败: %s', result['stderr'])
        return _list_envs_fallback(conda_bat)

    try:
        data = json.loads(result['stdout'])
    except json.JSONDecodeError:
        return _list_envs_fallback(conda_bat)

    envs = []
    prefixes = data.get('envs', [])
    raw_base = data.get('root_prefix') or ''
    base_prefix = os.path.normpath(raw_base) if raw_base else ''

    guessed_base = ''
    if not base_prefix and prefixes:
        first = os.path.normpath(prefixes[0])
        if os.path.basename(os.path.dirname(first)).lower() != 'envs':
            guessed_base = first

    for prefix in prefixes:
        path = Path(prefix)
        norm_prefix = os.path.normpath(prefix)
        name = path.name
        if (base_prefix and norm_prefix == base_prefix) or norm_prefix == guessed_base:
            name = 'base'
        envs.append({
            'name': name,
            'prefix': prefix,
            'is_active': False,
        })

    return envs


def _list_envs_fallback(conda_bat: str) -> list:
    """fallback：解析 conda env list 纯文本输出"""
    result = _run_conda(conda_bat, ['env', 'list'])
    if result['returncode'] != 0:
        return []

    envs = []
    for line in result['stdout'].splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        active = line.startswith('*')
        if active:
            line = line[1:].strip()

        parts = line.split()
        if len(parts) >= 2:
            name = parts[0]
            prefix = parts[-1]
            envs.append({
                'name': name,
                'prefix': prefix,
                'is_active': active,
            })

    return envs


def _parse_conda_list_json(stdout: str) -> list:
    """解析 conda list --json 输出，提取 name/version/build/channel。

    健壮化处理：conda 有时会在 JSON 前面塞 warning 文本（stdout 污染），
    会导致标准 json.loads 失败。这里先定位第一个 [ 或 { 再截取。
    """
    raw_preview = stdout[:300].replace('\n', '\\n').replace('\r', '\\r')

    if stdout.startswith('\ufeff'):
        stdout = stdout[1:]

    start = -1
    for i, ch in enumerate(stdout):
        if ch in '[{':
            start = i
            break

    if start < 0:
        logger.warning('_parse_conda_list_json: stdout 中未找到 JSON 起始符 [ 或 {, preview=%s', raw_preview)
        return []

    try:
        data = json.loads(stdout[start:])
    except json.JSONDecodeError as e:
        logger.warning('_parse_conda_list_json: JSONDecodeError at pos %d, err=%s, start=%d, preview=%s',
                       e.pos, e.msg, start, raw_preview)
        return []

    packages = []
    pkg_list = data if isinstance(data, list) else data.get('packages', [])
    for pkg in pkg_list:
        packages.append({
            'name': pkg.get('name', ''),
            'version': pkg.get('version', ''),
            'build': pkg.get('build_string', pkg.get('build', '')),
            'channel': pkg.get('channel', ''),
        })

    if not packages and stdout.strip():
        logger.warning('_parse_conda_list_json: 解析成功但包数为 0, data_type=%s, preview=%s',
                       type(data).__name__, raw_preview)

    return packages


def _run_and_snapshot_packages(conda_bat: str, env_prefix: str, distro_id: str) -> list:
    norm_prefix = os.path.normpath(env_prefix)
    result = _run_conda(conda_bat, ['list', '-p', norm_prefix, '--json'])
    if result['returncode'] != 0:
        logger.warning('conda list 失败 [%s]: %s', norm_prefix, result['stderr'])
        return []
    packages = _parse_conda_list_json(result['stdout'])
    _ensure_pkgs_dir()
    try:
        cache_handler.write_json(_env_pkgs_file(distro_id, env_prefix), packages)
    except OSError:
        pass

    return packages


def list_packages(conda_bat: str, env_prefix: str, distro_id: str = '', force_refresh: bool = False) -> list:
    """列出指定环境已安装的包。

    默认读快照文件（毫秒级）。快照不存在、为空、超过 15 分钟、或
    force_refresh=True 时，跑 conda list --json 并写入快照。
    """
    if not force_refresh and distro_id:
        snap = _env_pkgs_file(distro_id, env_prefix)
        try:
            if snap.is_file():
                mtime = snap.stat().st_mtime
                if time.time() - mtime < _PKG_SNAPSHOT_TTL:
                    cached = cache_handler.read_json(snap, default=None)
                    if cached and isinstance(cached, list):
                        return cached
        except OSError:
            import sys
            logger.warning('读缓存失败 [%s]: %s', snap, sys.exc_info()[1])

    effective_distro = distro_id or '_unknown'
    return _run_and_snapshot_packages(conda_bat, env_prefix, effective_distro)


def refresh_packages(conda_bat: str, env_prefix: str, distro_id: str = '') -> list:
    effective_distro = distro_id or '_unknown'
    return _run_and_snapshot_packages(conda_bat, env_prefix, effective_distro)


def remove_pkg_snapshot(distro_id: str, env_prefix: str) -> None:
    """删除单个环境的包快照（环境被移除时调用）。"""
    _remove_pkg_snapshot(distro_id, env_prefix)


def clean_pkg_snapshots_for_distro(distro_id: str) -> None:
    """发行版移除登记时，清理其所有包快照。"""
    _clean_pkg_snapshots_for_distro(distro_id)


def create_env(conda_bat: str, name: str, python_version: str = '',
               r_version: str = '', extra_packages: list = None) -> dict:
    args = ['create', '-n', name, '-y']
    if python_version:
        args.append(f'python={python_version}')
    if r_version:
        args.append('r-essentials')
    if extra_packages:
        args.extend(extra_packages)

    result = _run_conda(conda_bat, args, timeout=120)
    if result['returncode'] == 0:
        return {'success': True, 'message': f'环境 {name} 创建成功',
                'stdout': result['stdout'][-500:]}
    return {'success': False, 'message': result['stderr'][:200] or '创建失败',
            'stdout': result['stdout'][-500:], 'stderr': result['stderr'][-500:]}


def create_env_streaming(conda_bat: str, name: str, python_version: str = '',
                         r_version: str = '', extra_packages: list = None,
                         log_cb=None) -> dict:
    args = ['create', '-n', name, '-y']
    if python_version:
        args.append(f'python={python_version}')
    if r_version:
        args.append('r-essentials')
    if extra_packages:
        args.extend(extra_packages)

    cmd = [conda_bat] + args
    if log_cb:
        log_cb(f'执行: {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            env=_safe_env(),
            encoding='utf-8',
            errors='replace',
        )
    except FileNotFoundError:
        return {'success': False, 'message': f'conda 不存在: {conda_bat}', 'returncode': -2}
    except Exception as e:
        return {'success': False, 'message': str(e), 'returncode': -3}

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip('\r\n')
        if line and log_cb:
            log_cb(line)
    rc = proc.wait()

    if log_cb:
        log_cb(f'conda 退出码: {rc}（0=成功）')

    if rc == 0:
        return {'success': True, 'message': f'环境 {name} 创建成功', 'returncode': rc}
    return {'success': False, 'message': f'创建失败（退出码 {rc}）', 'returncode': rc}


def remove_env_streaming(conda_bat: str, env_prefix: str, log_cb=None) -> dict:
    args = ['env', 'remove', '-p', env_prefix, '-y']
    cmd = [conda_bat] + args
    if log_cb:
        log_cb(f'执行: {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            env=_safe_env(),
            encoding='utf-8',
            errors='replace',
        )
    except FileNotFoundError:
        return {'success': False, 'message': f'conda 不存在: {conda_bat}', 'returncode': -2}
    except Exception as e:
        return {'success': False, 'message': str(e), 'returncode': -3}

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip('\r\n')
        if line and log_cb:
            log_cb(line)
    rc = proc.wait()

    if log_cb:
        log_cb(f'conda 退出码: {rc}（0=成功）')

    if rc == 0:
        return {'success': True, 'message': '环境已删除', 'returncode': rc}
    return {'success': False, 'message': f'删除失败（退出码 {rc}）', 'returncode': rc}


def check_conda_update(conda_bat: str) -> dict:
    """检查 conda 本身是否有新版本（dry-run，不修改任何东西）。

    返回: {'has_update': bool, 'current': str, 'latest': str, 'message': str}
    """
    cmd = [conda_bat, 'update', '-n', 'base', 'conda', '--dry-run', '--json']
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW,
            encoding='utf-8', errors='replace',
            env=_safe_env(),
        )
    except Exception as e:
        return {'has_update': False, 'current': '', 'latest': '',
                'message': f'检查失败: {e}'}

    if result.returncode != 0:
        return {'has_update': False, 'current': '', 'latest': '',
                'message': (result.stderr or '检查失败')[:300]}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {'has_update': False, 'current': '', 'latest': '',
                'message': '输出解析失败'}

    actions = data.get('actions', {})
    unlink = actions.get('UNLINK', [])
    link = actions.get('LINK', [])

    cur_conda = next((p for p in unlink if p.get('name') == 'conda'), None)
    new_conda = next((p for p in link if p.get('name') == 'conda'), None)

    if cur_conda and new_conda and cur_conda.get('version') != new_conda.get('version'):
        return {
            'has_update': True,
            'current': cur_conda['version'],
            'latest': new_conda['version'],
            'message': f'Conda {cur_conda["version"]} → {new_conda["version"]}',
        }
    return {'has_update': False,
            'current': (cur_conda or {}).get('version', ''),
            'latest': '', 'message': '已是最新版本'}


def upgrade_conda_streaming(conda_bat: str, log_cb=None) -> dict:
    args = ['update', '-n', 'base', 'conda', '-y']
    cmd = [conda_bat] + args
    if log_cb:
        log_cb(f'执行: {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            env=_safe_env(),
            encoding='utf-8',
            errors='replace',
        )
    except FileNotFoundError:
        return {'success': False, 'message': f'conda 不存在: {conda_bat}', 'returncode': -2}
    except Exception as e:
        return {'success': False, 'message': str(e), 'returncode': -3}

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip('\r\n')
        if line and log_cb:
            log_cb(line)
    rc = proc.wait()

    if log_cb:
        log_cb(f'conda 退出码: {rc}（0=成功）')

    if rc == 0:
        return {'success': True, 'message': 'Conda 升级成功', 'returncode': rc}
    return {'success': False, 'message': f'升级失败（退出码 {rc}）', 'returncode': rc}


# ── 下载 + 安装 ─────────────────────────────────────────

import urllib.request
import tempfile
import time


def download_installer(url: str, filename: str, progress_cb=None) -> Path:
    """下载 conda 安装包到 data/conda/downloads/，返回本地文件路径。

    progress_cb(received, total, speed_str) —— 每 512KB 回调一次，
    total 为 None 时表示服务端没返回 Content-Length。
    """
    target = DOWNLOAD_DIR / filename
    tmp = target.with_suffix(target.suffix + '.part')

    if target.is_file() and target.stat().st_size > 1024 * 1024:
        if progress_cb:
            progress_cb(target.stat().st_size, target.stat().st_size, '已存在')
        return target

    req = urllib.request.Request(url, headers={
        'User-Agent': 'jieEnv-Conda/1.0 (+https://jieenv.local)',
    })

    received = 0
    chunk_size = 64 * 1024
    report_interval = 8
    last_report_mb = 0

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = resp.headers.get('Content-Length')
            total = int(total) if total else None

            with open(tmp, 'wb') as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)

                    cur_mb = received // (report_interval * 1024 * 1024)
                    if cur_mb > last_report_mb and progress_cb:
                        last_report_mb = cur_mb
                        progress_cb(received, total, '')

        tmp.replace(target)
        return target

    except Exception:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


def run_silent_install(installer_path: str, install_dir: str,
                       progress_cb=None) -> dict:
    """执行 conda 静默安装（JustMe + RegisterPython=0 + AddToPath=0）。

    progress_cb(file_count, total_size) —— 安装进行中每 2 秒回调一次，
    扫描安装目录的文件数和累计字节数，让上层有"心跳"感。

    返回 {returncode, stdout, stderr}。
    """
    import os as _os
    dir_arg = _os.path.normpath(install_dir)
    cmd = [
        installer_path,
        '/S',
        '/InstallationType=JustMe',
        '/RegisterPython=0',
        '/AddToPath=0',
        f'/D={dir_arg}',
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        last_cb = 0
        while proc.poll() is None:
            now = time.time()
            if progress_cb and now - last_cb >= 2:
                last_cb = now
                fc, ts = _scan_dir_usage(install_dir)
                try:
                    progress_cb(fc, ts)
                except Exception:
                    pass
            time.sleep(0.5)

        stdout, stderr = proc.communicate(timeout=60)
        enc = get_cmd_encoding()
        return {
            'returncode': proc.returncode,
            'stdout': (stdout or b'').decode(enc, errors='replace'),
            'stderr': (stderr or b'').decode(enc, errors='replace'),
        }
    except subprocess.TimeoutExpired:
        return {'returncode': -1, 'stdout': '', 'stderr': '安装超时（10 分钟）'}
    except FileNotFoundError:
        return {'returncode': -2, 'stdout': '', 'stderr': f'安装包不存在: {installer_path}'}
    except Exception as e:
        return {'returncode': -3, 'stdout': '', 'stderr': str(e)}


def _scan_dir_usage(path: str) -> tuple:
    """递归扫描目录，返回 (文件数, 累计字节数)。目录不存在时返回 (0, 0)。"""
    p = Path(path)
    if not p.is_dir():
        return 0, 0
    fc = 0
    ts = 0
    try:
        for entry in p.rglob('*'):
            if entry.is_file():
                fc += 1
                try:
                    ts += entry.stat().st_size
                except OSError:
                    pass
    except (PermissionError, OSError):
        pass
    return fc, ts


def _find_conda_executable(install_dir: str) -> str:
    """在安装目录里定位 conda 可执行文件路径。"""
    install_dir = os.path.normpath(install_dir)
    candidates = [
        os.path.join(install_dir, 'Scripts', 'conda.bat'),
        os.path.join(install_dir, 'Scripts', 'conda.exe'),
        os.path.join(install_dir, 'condabin', 'conda.bat'),
        os.path.join(install_dir, 'bin', 'conda'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return ''


def search_package_versions(conda_bat: str, package_name: str, timeout: int = 45,
                             aggregate_threshold: int = 30) -> list:
    """conda search --full-name <package> --json，返回版本列表。

    和 Conda Navigator 对齐：
    - 当 unique version 数量 <= aggregate_threshold 时，直接全列（如 r-base 只有 10 条）
    - 当 unique version 数量 > aggregate_threshold 时，按大版本去重取最高小版本（如 python 有 151 条）

    返回格式:
      聚合模式: [{"major": "3.15", "full": "3.15.2"}, ...]
      直接模式: [{"major": "3.6", "full": "3.6.1"}, ...] （major 还是大版本分组，便于前端显示）
    """
    result = _run_conda(conda_bat, ['search', '--full-name', package_name, '--json'], timeout=timeout)
    if result['returncode'] != 0:
        logger.warning('conda search %s 失败: %s', package_name, result['stderr'][:300])
        return []

    stdout = result['stdout']
    if stdout.startswith('\ufeff'):
        stdout = stdout[1:]

    start = -1
    for i, ch in enumerate(stdout):
        if ch in '[{':
            start = i
            break
    if start < 0:
        return []

    try:
        data = json.loads(stdout[start:])
    except json.JSONDecodeError:
        return []

    items = data.get(package_name, [])
    if not items:
        return []

    all_full = sorted(
        {item.get('version', '') for item in items if item.get('version')},
        key=_version_tuple, reverse=True,
    )

    if len(all_full) <= aggregate_threshold:
        result = []
        for full in all_full:
            parts = full.split('.')
            major = f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else full
            result.append({'major': major, 'full': full})
        return result

    from collections import OrderedDict
    major_map = OrderedDict()
    for full in all_full:
        parts = full.split('.')
        major = f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else full
        prev = major_map.get(major, '')
        if not prev or _version_tuple(full) > _version_tuple(prev):
            major_map[major] = full

    groups = sorted(major_map.items(), key=lambda x: _version_tuple(x[0]), reverse=True)
    return [{'major': m, 'full': f} for m, f in groups]


def _version_tuple(v: str) -> tuple:
    """把 "3.15.2" 转成 (3, 15, 2) 用于排序，非数字片段用 999 兜底。"""
    parts = []
    for p in v.split('.'):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(999)
    return tuple(parts)


def post_install_setup(install_dir: str) -> list:
    """安装完成后的初始化（已暂停，返回空列表）。"""
    return []


def search_packages(conda_bat: str, keyword: str, timeout: int = 30) -> dict:
    """模糊搜索包名。

    conda search <keyword> --json（不带 --full-name），从结果中提取唯一包名。
    返回 {'success': True, 'packages': [{'name': 'pandas'}, ...]}
    或 {'success': False, 'message': '...'}
    """
    args = ['search', keyword, '--json']
    result = _run_conda(conda_bat, args, timeout=timeout)

    stdout = result['stdout']
    if stdout.startswith('\ufeff'):
        stdout = stdout[1:]

    start = -1
    for i, ch in enumerate(stdout):
        if ch in '[{':
            start = i
            break
    if start < 0:
        return {'success': False, 'message': 'conda 未返回有效数据'}

    try:
        data = json.loads(stdout[start:])
    except json.JSONDecodeError:
        return {'success': False, 'message': 'conda 返回非 JSON'}

    if result['returncode'] != 0:
        return {'success': False,
                'message': (result['stderr'] or '搜索失败')[:300]}

    if not isinstance(data, dict):
        return {'success': True, 'packages': []}

    names = set(data.keys())
    result_list = [{'name': n} for n in sorted(names)]
    return {'success': True, 'packages': result_list}


def remove_package(conda_bat: str, env_prefix: str, package_name: str) -> dict:
    """conda remove -p <env_prefix> <pkg> -y（同步）。

    返回 {'success', 'message', 'returncode'}。
    """
    env_prefix = normalize_env_prefix(env_prefix)
    args = ['remove', '-p', env_prefix, package_name, '-y']
    result = _run_conda(conda_bat, args, timeout=120)

    if result['returncode'] == 0:
        return {'success': True, 'message': f'已卸载 {package_name}',
                'returncode': result['returncode']}
    return {'success': False,
            'message': (result['stderr'] or '卸载失败')[:500],
            'returncode': result['returncode']}


def remove_package_streaming(conda_bat: str, env_prefix: str,
                             package_name: str, log_cb=None) -> dict:
    """conda remove -p <env_prefix> <pkg> -y（流式，吐给 log_cb）。"""
    env_prefix = normalize_env_prefix(env_prefix)
    cmd = [conda_bat, 'remove', '-p', env_prefix, package_name, '-y']
    if log_cb:
        log_cb(f'执行: {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            env=_safe_env(),
            encoding='utf-8',
            errors='replace',
        )
    except FileNotFoundError:
        return {'success': False, 'message': f'conda 不存在: {conda_bat}', 'returncode': -2}
    except Exception as e:
        return {'success': False, 'message': str(e), 'returncode': -3}

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip('\r\n')
        if line and log_cb:
            log_cb(line)
    rc = proc.wait()

    if log_cb:
        log_cb(f'conda 退出码: {rc}（0=成功）')

    if rc == 0:
        return {'success': True, 'message': f'卸载成功: {package_name}',
                'returncode': rc}
    return {'success': False, 'message': f'卸载失败（退出码 {rc}）',
            'returncode': rc}


def install_packages_streaming(conda_bat: str, env_prefix: str,
                               packages: list, log_cb=None) -> dict:
    env_prefix = normalize_env_prefix(env_prefix)
    pkg_args = []
    for p in packages:
        name = p.get('name', '').strip()
        if not name:
            continue
        ver = p.get('version', '').strip()
        if ver and ver not in ('latest', '*'):
            pkg_args.append(f'{name}={ver}')
        else:
            pkg_args.append(name)

    if not pkg_args:
        return {'success': False, 'message': '未选择任何包', 'returncode': -1}

    cmd = [conda_bat, 'install', '-p', env_prefix] + pkg_args + ['-y']
    if log_cb:
        log_cb(f'执行: {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            env=_safe_env(),
            encoding='utf-8',
            errors='replace',
        )
    except FileNotFoundError:
        return {'success': False, 'message': f'conda 不存在: {conda_bat}', 'returncode': -2}
    except Exception as e:
        return {'success': False, 'message': str(e), 'returncode': -3}

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip('\r\n')
        if line and log_cb:
            log_cb(line)
    rc = proc.wait()

    if log_cb:
        log_cb(f'conda 退出码: {rc}（0=成功）')

    if rc == 0:
        return {'success': True, 'message': f'安装成功: {", ".join(pkg_args)}',
                'returncode': rc}
    return {'success': False, 'message': f'安装失败（退出码 {rc}）',
            'returncode': rc}

def check_updates(conda_bat: str, env_prefix: str, timeout: int = 60) -> dict:
    env_prefix = normalize_env_prefix(env_prefix)
    cmd = [conda_bat, 'update', '-p', env_prefix, '--all', '--dry-run', '--json']
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            encoding='utf-8', errors='replace',
            env=_safe_env(),
        )
    except subprocess.TimeoutExpired:
        return {}
    except Exception:
        return {}

    if proc.returncode != 0 or not proc.stdout:
        return {}

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, Exception):
        return {}

    actions = data.get('actions', {})
    unlink = {p['name']: p for p in actions.get('UNLINK', []) if p.get('name') and p.get('version')}
    link = {p['name']: p for p in actions.get('LINK', []) if p.get('name') and p.get('version')}

    updates = {}
    for name, new_pkg in link.items():
        old_pkg = unlink.get(name)
        if old_pkg and old_pkg.get('version') != new_pkg.get('version'):
            updates[name] = new_pkg['version']
    return updates


def clone_env_streaming(conda_bat: str, src_prefix: str, new_name: str,
                        log_cb=None) -> dict:
    src_prefix = normalize_env_prefix(src_prefix)
    new_prefix = os.path.join(os.path.dirname(src_prefix), new_name)
    args = [conda_bat, 'create', '-p', new_prefix, '--clone', src_prefix, '-y']
    return _run_streaming(args, log_cb)


def rename_env_streaming(conda_bat: str, old_prefix: str, new_name: str,
                         log_cb=None) -> dict:
    old_prefix = normalize_env_prefix(old_prefix)
    args = [conda_bat, 'rename', '-p', old_prefix, new_name]
    return _run_streaming(args, log_cb)


def export_env_yml(conda_bat: str, env_prefix: str) -> dict:
    env_prefix = normalize_env_prefix(env_prefix)
    result = _run_conda(conda_bat,
                        ['env', 'export', '-p', env_prefix, '--no-builds'],
                        timeout=60)
    return result


def create_env_from_yml_streaming(conda_bat: str, yml_path: str, env_name: str,
                                  log_cb=None) -> dict:
    args = [conda_bat, 'env', 'create', '-f', yml_path]
    if env_name:
        args += ['-n', env_name]
    return _run_streaming(args, log_cb)


def _run_streaming(args: list, log_cb=None, timeout: int = 1800) -> dict:
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=CREATE_NO_WINDOW,
            env=_safe_env(), encoding='utf-8', errors='replace',
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip('\r\n')
            if line and log_cb:
                log_cb(line)
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        rc = -1
        if log_cb:
            log_cb(f'执行超时（{timeout}s）')
    except Exception as e:
        rc = -1
        if log_cb:
            log_cb(f'执行失败: {e}')
    return {'returncode': rc}