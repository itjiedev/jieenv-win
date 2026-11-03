import os
import logging

from jiecore.app_services import call_service
from jiecore.provider_catalog import list_providers

from . import snapshot

logger = logging.getLogger(__name__)

_SIZE_SKIP_DIRS = {'.git', '.svn', '.hg', 'node_modules', '__pycache__', '.venv', 'venv', '.env', 'env'}

_COMMON_DEP_FILES = {
    'python': [
        # 第一层：核心项目定义与依赖
        'requirements.txt',
        'pyproject.toml',
        'Pipfile',
        'setup.py',
        'setup.cfg',

        # 第二层：锁文件
        'Pipfile.lock',
        'poetry.lock',
        'uv.lock',
        'pdm.lock',
        'requirements.lock',

        # 第三层：环境与版本管理
        '.python-version',
        'environment.yml',
        'environment.yaml',
        'conda.yml',
        'conda.yaml',

        # 第四层：打包与分发辅助
        'MANIFEST.in',
        '.pypirc',

        # 第五层：工具配置（辅助判断）
        'tox.ini',
        'pytest.ini',
        'mypy.ini',
        '.flake8',
        'ruff.toml',
        '.pylintrc',
    ],
    'node': ['package.json', 'yarn.lock', 'pnpm-lock.yaml'],
    'go': ['go.mod', 'go.sum'],
    'rust': ['Cargo.toml', 'Cargo.lock'],
    'java': ['pom.xml', 'build.gradle', 'build.gradle.kts'],
}


def scan_directory(work_dir: str) -> dict:
    if not work_dir or not os.path.isdir(work_dir):
        return {
            'dir_exists': False,
            'is_empty': False,
            'is_git_repo': False,
            'dir_size_mb': 0.0,
            'has_snapshot': False,
            'snapshot': None,
            'detected_runtimes': [],
            'unknown_files': [],
            'recommended_action': 'invalid_dir',
        }

    work_dir = os.path.abspath(work_dir)

    entries = os.listdir(work_dir)
    is_empty = len(entries) == 0
    is_git_repo = os.path.isdir(os.path.join(work_dir, '.git'))

    dir_size_mb = _estimate_dir_size(work_dir)

    has_snapshot = snapshot.snapshot_exists(work_dir)
    snap_data = None
    if has_snapshot:
        try:
            snap_data = snapshot.read_snapshot(work_dir)
        except Exception as _e:
            logger.warning('snapshot read failed: %s', _e)
            snap_data = None

    detected_runtimes = _collect_runtime_detections(work_dir)

    unknown_files = _collect_unknown_files(work_dir, detected_runtimes)

    recommended_action = _decide_recommended_action(
        has_snapshot, detected_runtimes, is_empty,
    )

    return {
        'dir_exists': True,
        'is_empty': is_empty,
        'is_git_repo': is_git_repo,
        'dir_size_mb': round(dir_size_mb, 2),
        'has_snapshot': has_snapshot,
        'snapshot': snap_data,
        'detected_runtimes': detected_runtimes,
        'unknown_files': unknown_files,
        'recommended_action': recommended_action,
    }


def _estimate_dir_size(work_dir: str) -> float:
    total = 0
    try:
        for root, dirs, files in os.walk(work_dir):
            dirs[:] = [d for d in dirs if d not in _SIZE_SKIP_DIRS]
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total / (1024 * 1024)


def _collect_runtime_detections(work_dir: str) -> list:
    detections = []

    python_result = _scan_python_detailed(work_dir)
    if python_result:
        detections.append(python_result)

    node_result = _scan_manifest_files(work_dir, 'node')
    if node_result:
        detections.append(node_result)

    go_result = _scan_manifest_files(work_dir, 'go')
    if go_result:
        detections.append(go_result)

    rust_result = _scan_manifest_files(work_dir, 'rust')
    if rust_result:
        detections.append(rust_result)

    return detections


def _scan_venv_in_dir(work_dir: str) -> dict | None:
    """Pure os.path scan for a venv inside work_dir — no call_service, no provider dependency."""
    if not work_dir or not os.path.isdir(work_dir):
        return None

    venv_candidates = ['.venv', 'venv', '.env', 'env']

    for c in venv_candidates:
        full = os.path.join(work_dir, c)
        if not os.path.isdir(full):
            continue

        cfg_path = os.path.join(full, 'pyvenv.cfg')
        conda_meta = os.path.join(full, 'conda-meta')
        kind = ''
        home = ''

        if os.path.isfile(cfg_path):
            kind = 'venv'
            try:
                with open(cfg_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('home') and '=' in line:
                            home = line.split('=', 1)[1].strip()
                            break
            except Exception:
                pass
        elif os.path.isdir(conda_meta):
            kind = 'conda'
        elif (
            os.path.isfile(os.path.join(full, 'Scripts', 'python.exe'))
            or os.path.isfile(os.path.join(full, 'bin', 'python'))
        ):
            kind = 'venv-like'

        if kind:
            return {
                'name': c,
                'path': full,
                'kind': kind,
                'is_venv': kind in ('venv', 'venv-like'),
                'home': home,
            }

    return None


def _scan_python_detailed(work_dir: str) -> dict | None:
    evidence = []

    deps_found = False
    deps_files = []
    for dep_file in _COMMON_DEP_FILES['python']:
        full = os.path.join(work_dir, dep_file)
        if os.path.exists(full):
            deps_files.append(dep_file)
            evidence.append(dep_file)
            deps_found = True

    deps_count = _count_requirements(os.path.join(work_dir, 'requirements.txt'))

    local_venv = _scan_venv_in_dir(work_dir)

    scan_raw = []
    try:
        scan_raw = call_service('python_main', 'scan_python_env', {'project_dir': work_dir}) or []
    except Exception as _e:
        logger.debug('scan_python_env failed: %s', _e)

    merged = []
    if local_venv:
        merged.append(local_venv)
    for item in scan_raw:
        name = item.get('name', '')
        abs_path = item.get('path', '')
        if not name and not abs_path:
            continue
        if local_venv and abs_path == local_venv.get('path', ''):
            continue
        if local_venv and name == local_venv.get('name', ''):
            continue
        merged.append(item)

    venv_found = False
    venv_path = None
    venv_python = None
    venv_valid = False
    venv_home = ''
    base_python = ''

    for item in merged:
        if not item.get('is_venv'):
            continue
        abs_path = item.get('path', '')
        name = item.get('name', '')
        home = item.get('home', '')

        if name:
            evidence.append(name + '/')

        candidate_python = os.path.join(abs_path, 'Scripts', 'python.exe')
        if not os.path.isfile(candidate_python):
            candidate_python = os.path.join(abs_path, 'bin', 'python3')

        venv_found = True
        venv_path = name if name else os.path.basename(abs_path)
        venv_python = candidate_python
        venv_valid = os.path.isfile(candidate_python)
        venv_home = home

        cfg_path = os.path.join(abs_path, 'pyvenv.cfg')
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('executable'):
                            exe_path = line.partition('=')[2].strip()
                            if exe_path and os.path.isfile(exe_path):
                                base_python = exe_path
                                break
            except Exception:
                pass

        if not base_python and home:
            candidate_base = os.path.join(home, 'python.exe')
            if os.path.isfile(candidate_base):
                base_python = candidate_base

        break

    needs_action = None
    if venv_found and venv_valid:
        needs_action = None
    elif venv_found and not venv_valid:
        needs_action = 'repair_venv'
    elif deps_found and not venv_found:
        needs_action = 'create_venv'

    if not venv_found and not deps_found:
        return None

    return {
        'lang': 'python',
        'provider_key': 'runtime.python',
        'evidence': evidence,
        'venv_found': venv_found,
        'venv_path': venv_path,
        'venv_python': venv_python,
        'venv_valid': venv_valid,
        'venv_home': venv_home,
        'base_python': base_python,
        'deps_found': deps_found,
        'deps_files': deps_files,
        'deps_count': deps_count,
        'needs_action': needs_action,
    }


def _scan_manifest_files(work_dir: str, lang: str) -> dict | None:
    evidence = []
    manifest_files = []
    for fname in _COMMON_DEP_FILES[lang]:
        full = os.path.join(work_dir, fname)
        if os.path.exists(full):
            manifest_files.append(fname)
            evidence.append(fname)

    if not manifest_files:
        return None

    provider_key_map = {
        'node': 'runtime.node',
        'go': 'runtime.go',
        'rust': 'runtime.rust',
        'java': 'runtime.java',
    }

    return {
        'lang': lang,
        'provider_key': provider_key_map.get(lang, ''),
        'evidence': evidence,
        'manifest_files': manifest_files,
        'deps_found': True,
        'needs_action': None,
    }


def _count_requirements(req_path: str) -> int:
    if not os.path.isfile(req_path):
        return 0
    try:
        with open(req_path, 'r', encoding='utf-8', errors='ignore') as f:
            return sum(
                1 for line in f
                if line.strip() and not line.lstrip().startswith('#')
            )
    except Exception:
        return 0


def _collect_unknown_files(work_dir: str, detected_runtimes: list) -> list:
    known = {'.git', '.svn', '.hg', '.jieenv', '__pycache__', '.idea', '.vscode'}
    for rt in detected_runtimes:
        for ev in rt.get('evidence', []):
            known.add(ev.rstrip('/').rstrip('\\'))
            if ev.endswith('/') or ev.endswith('\\'):
                known.add(ev.rstrip('/').rstrip('\\'))

    unknown = []
    try:
        for entry in os.listdir(work_dir):
            if entry in known:
                continue
            full = os.path.join(work_dir, entry)
            if os.path.isdir(full) and entry in _SIZE_SKIP_DIRS:
                continue
            if entry.startswith('.'):
                continue
            unknown.append(entry)
    except OSError:
        pass
    return unknown


def _decide_recommended_action(has_snapshot: bool, detected_runtimes: list, is_empty: bool) -> str:
    if is_empty:
        return 'empty_dir'
    if has_snapshot:
        return 'restore_from_snapshot'
    if detected_runtimes:
        return 'import_with_components'
    return 'manual_setup'