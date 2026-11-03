import os
import shutil
import logging
import subprocess
from pathlib import Path
from datetime import datetime

from jiecore import tasks

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def _child_env():
    env = os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
        env.pop(key, None)
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONNOUSERSITE'] = '1'
    return env


def _norm(p: str) -> str:
    return (p or '').replace('\\', '/').rstrip('/').lower()


def read_pyvenv_cfg(venv_path: str) -> dict | None:
    cfg_path = Path(venv_path) / 'pyvenv.cfg'
    if not cfg_path.is_file():
        return None
    result = {}
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if '=' in line:
                    k, _, v = line.partition('=')
                    result[k.strip()] = v.strip()
    except Exception as e:
        logger.warning('读取 pyvenv.cfg 失败: %s -> %s', cfg_path, e)
        return None
    return result


def verify_venv_l1(venv_path: str) -> dict:
    path = Path(venv_path)
    if not path.exists() or not path.is_dir():
        return {'valid': False, 'status': 'missing', 'reason': '目录不存在'}

    cfg = read_pyvenv_cfg(venv_path)
    if cfg is None:
        return {'valid': False, 'status': 'damaged', 'reason': '缺少 pyvenv.cfg'}

    if os.name == 'nt':
        python_exe = path / 'Scripts' / 'python.exe'
    else:
        python_exe = path / 'bin' / 'python'
    if not python_exe.is_file():
        return {'valid': False, 'status': 'damaged', 'reason': '缺少 python.exe'}

    home = cfg.get('home', '')
    if not home:
        return {'valid': False, 'status': 'damaged', 'reason': 'pyvenv.cfg 缺少 home 字段'}

    home_valid = Path(home).exists()
    return {
        'valid': home_valid,
        'status': 'ready' if home_valid else 'owner_lost',
        'reason': '' if home_valid else '归属的 Python 解释器已不存在',
        'home': home,
        'python_exe': str(python_exe),
    }


def verify_venv_l2(venv_path: str) -> dict:
    l1 = verify_venv_l1(venv_path)
    if not l1.get('valid'):
        l1['pip_count'] = 0
        l1['pip_version'] = ''
        return l1

    python_exe = l1['python_exe']
    try:
        proc = subprocess.run(
            [python_exe, '-m', 'pip', '--version'],
            capture_output=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW, env=_child_env())
        pip_works = proc.returncode == 0
        pip_version = ''
        if pip_works:
            out = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
            parts = out.split()
            if len(parts) >= 2:
                pip_version = parts[1]
    except Exception as e:
        logger.warning('pip --version 执行失败: %s', e)
        pip_works = False
        pip_version = ''

    pip_count = 0
    if pip_works:
        try:
            proc2 = subprocess.run(
                [python_exe, '-m', 'pip', 'list', '--format=json'],
                capture_output=True, timeout=30,
                creationflags=_CREATE_NO_WINDOW, env=_child_env())
            print(proc2)
            if proc2.returncode == 0:
                import json
                data = json.loads((proc2.stdout or b'').decode('utf-8', errors='replace'))
                pip_count = len([p for p in data if isinstance(p, dict)])
        except Exception as e:
            logger.warning('pip list 执行失败: %s', e)

    status = 'ready' if pip_works else 'damaged'
    reason = '' if pip_works else 'pip 不可用'
    return {
        'valid': pip_works,
        'status': status,
        'reason': reason,
        'home': l1.get('home', ''),
        'python_exe': python_exe,
        'pip_count': pip_count,
        'pip_version': pip_version,
    }


def _dir_name_illegal(name: str) -> str:
    import re as _re
    if _re.search(r'[^\w\-]', name, _re.UNICODE):
        return '虚拟环境名称只能包含字母、数字、下划线和短横线（不能有中文或空格）'
    return ''


def _is_dir_empty(path_obj) -> bool:
    try:
        return not any(path_obj.iterdir())
    except PermissionError:
        return False


def create_venv(install_dir: str, target_path: str,
                include_system_site: bool = False, on_output=None) -> dict:
    install_dir = (install_dir or '').strip()
    target_path = (target_path or '').strip()
    if not install_dir or not target_path:
        return {'success': False, 'message': '安装目录和目标路径不能为空'}

    name_ok = _dir_name_illegal(Path(target_path).name)
    if name_ok:
        return {'success': False, 'message': name_ok}

    python_exe = Path(install_dir) / ('python.exe' if os.name == 'nt' else 'python')
    if not python_exe.is_file():
        return {'success': False, 'message': f'Python 解释器不存在: {python_exe}'}

    target_dir = Path(target_path)
    if target_dir.exists():
        if not target_dir.is_dir():
            return {'success': False, 'message': f'目标路径是一个文件，不是目录: {target_dir}'}
        if not _is_dir_empty(target_dir):
            return {'success': False, 'message':
                    f'目标目录已存在且非空，请选择不存在的路径或先清空: {target_dir}'}

    target_dir.parent.mkdir(parents=True, exist_ok=True)

    args = ['-m', 'venv', str(target_dir)]
    if include_system_site:
        args.insert(0, '--system-site-packages')

    try:
        proc = subprocess.Popen(
            [str(python_exe)] + args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW, env=_child_env())
        for line in proc.stdout:
            line = (line or b'').decode('utf-8', errors='replace').rstrip()
            if line and on_output:
                on_output(line)
        proc.wait()
        if proc.returncode != 0:
            return {'success': False, 'message': f'创建失败（退出码 {proc.returncode}）',
                    'venv_path': str(target_dir)}
    except Exception as e:
        return {'success': False, 'message': f'启动 venv 失败: {e}'}

    verify = verify_venv_l2(str(target_dir))
    if not verify.get('valid'):
        return {'success': False, 'message': f'创建后验证失败: {verify.get("reason", "")}',
                'venv_path': str(target_dir), 'verify': verify}

    return {
        'success': True,
        'message': f'虚拟环境创建成功: {target_dir}',
        'venv_path': str(target_dir),
        'verify': verify,
    }


def import_venv(venv_path: str, force: bool = False) -> dict:
    venv_path = (venv_path or '').strip()
    if not venv_path:
        return {'success': False, 'message': '请选择一个目录'}

    verify = verify_venv_l2(venv_path)
    if not verify.get('valid') and not force:
        l1 = verify_venv_l1(venv_path)
        reason = verify.get('reason') or l1.get('reason') or '未知原因'
        if verify.get('status') == 'owner_lost':
            return {'success': False, 'message': f'归属的 Python 解释器不存在，无法导入（{reason}）'}
        return {'success': False, 'message': f'不是有效的虚拟环境: {reason}'}

    cfg = read_pyvenv_cfg(venv_path)
    home = (cfg or {}).get('home', '')
    if not home:
        return {'success': False, 'message': '无法从 pyvenv.cfg 读取归属 Python 路径'}

    from . import storage
    owner_record = None
    for rec in storage.list_installed():
        if _norm(rec.get('install_dir', '')) == _norm(home):
            owner_record = rec
            break

    if not owner_record and not force:
        return {'success': False, 'message':
                f'该虚拟环境归属的 Python（{home}）未登记在本应用中，请先安装或导入该 Python'}

    return {
        'success': True,
        'message': '验证通过，可以登记',
        'venv_path': venv_path,
        'home': home,
        'owner_record': owner_record,
        'verify': verify,
        'python_exe': verify.get('python_exe') or str(Path(venv_path) / 'Scripts' / 'python.exe'),
    }


def delete_venv(venv_path: str, delete_dir: bool = True) -> dict:
    verify = verify_venv_l1(venv_path)
    if verify.get('status') == 'missing':
        return {'success': False, 'message': '目录已不存在'}

    if delete_dir:
        try:
            shutil.rmtree(venv_path)
        except Exception as e:
            return {'success': False, 'message': f'删除目录失败: {e}'}
    return {'success': True, 'message': '删除成功'}


def detect_venvs(install_dir: str) -> list:
    install_norm = _norm(install_dir)
    results = []
    candidates = []

    install_path = Path(install_dir)
    if install_path.is_dir():
        candidates.append(install_path / 'venvs')
        candidates.append(install_path / 'venv')
        for child in install_path.iterdir():
            if child.is_dir():
                name = child.name.lower()
                if name.startswith('.venv') or name == 'venv':
                    candidates.append(child)

    seen = set()
    for c in candidates:
        if not c.is_dir():
            continue
        try:
            for sub in c.iterdir():
                if not sub.is_dir():
                    continue
                sp = str(sub)
                if sp in seen:
                    continue
                seen.add(sp)
                v = verify_venv_l1(sp)
                if v.get('valid') and _norm(v.get('home', '')) == install_norm:
                    results.append({
                        'path': sp,
                        'name': sub.name,
                        'python_exe': v.get('python_exe', ''),
                    })
        except PermissionError:
            continue

    return results