import os
import shutil
import logging
import subprocess
from pathlib import Path

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


def create_venv(python_exe: str, venv_dir: str,
                with_pip: bool = True,
                system_site: bool = False,
                on_output=None) -> dict:
    python_exe = (python_exe or '').strip()
    venv_dir = (venv_dir or '').strip()
    if not python_exe or not venv_dir:
        return {'success': False, 'message': 'python_exe 和 venv_dir 不能为空'}

    p_exe = Path(python_exe)
    if not p_exe.is_file():
        return {'success': False, 'message': f'Python 解释器不存在: {p_exe}'}

    target = Path(venv_dir)
    if target.exists():
        if not target.is_dir():
            return {'success': False, 'message': f'目标路径是文件而非目录: {target}'}
        try:
            if any(target.iterdir()):
                return {'success': False,
                        'message': f'目标目录已存在且非空: {target}'}
        except PermissionError:
            return {'success': False, 'message': f'无权限访问目标目录: {target}'}

    target.parent.mkdir(parents=True, exist_ok=True)

    args = ['-m', 'venv', str(target)]
    if system_site:
        args.insert(0, '--system-site-packages')

    try:
        proc = subprocess.Popen(
            [str(p_exe)] + args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW, env=_child_env())
        for line in proc.stdout:
            line = (line or b'').decode('utf-8', errors='replace').rstrip()
            if line and on_output:
                on_output(line)
        proc.wait()
        if proc.returncode != 0:
            return {'success': False, 'message': f'创建失败（退出码 {proc.returncode}）'}
    except Exception as e:
        return {'success': False, 'message': f'启动 venv 失败: {e}'}

    verify = verify_venv(str(target))
    if not verify.get('success'):
        return {'success': False,
                'message': f'创建后验证失败: {verify.get("message", "")}'}

    return {
        'success': True,
        'message': f'虚拟环境创建成功: {target}',
        'venv_python': verify.get('venv_python', ''),
    }


def verify_venv(venv_dir: str, use_venv: bool = True) -> dict:
    if not use_venv:
        return {'success': True, 'venv_python': '', 'python_version': ''}

    venv_dir = (venv_dir or '').strip()
    if not venv_dir:
        return {'success': False, 'message': 'venv_dir 不能为空',
                'venv_python': '', 'python_version': ''}

    path = Path(venv_dir)
    if not path.exists() or not path.is_dir():
        return {'success': False, 'message': '目录不存在',
                'venv_python': '', 'python_version': ''}

    cfg_path = path / 'pyvenv.cfg'
    if not cfg_path.is_file():
        return {'success': False, 'message': '缺少 pyvenv.cfg',
                'venv_python': '', 'python_version': ''}

    if os.name == 'nt':
        venv_python = path / 'Scripts' / 'python.exe'
    else:
        venv_python = path / 'bin' / 'python'
    if not venv_python.is_file():
        return {'success': False, 'message': '缺少 python.exe',
                'venv_python': str(venv_python), 'python_version': ''}

    try:
        proc = subprocess.run(
            [str(venv_python), '-c',
             'import sys; print(sys.version_info.major, sys.version_info.minor, end="")'],
            capture_output=True, timeout=10,
            creationflags=_CREATE_NO_WINDOW, env=_child_env())
        if proc.returncode != 0:
            return {'success': False, 'message': 'Python 可执行文件无法运行',
                    'venv_python': str(venv_python), 'python_version': ''}
        python_version = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
    except Exception as e:
        return {'success': False, 'message': f'执行 Python 检查失败: {e}',
                'venv_python': str(venv_python), 'python_version': ''}

    try:
        proc2 = subprocess.run(
            [str(venv_python), '-m', 'pip', '--version'],
            capture_output=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW, env=_child_env())
        if proc2.returncode != 0:
            return {'success': False, 'message': 'pip 不可用',
                    'venv_python': str(venv_python), 'python_version': python_version}
    except Exception as e:
        return {'success': False, 'message': f'pip 检查失败: {e}',
                'venv_python': str(venv_python), 'python_version': python_version}

    return {
        'success': True,
        'message': '',
        'venv_python': str(venv_python),
        'python_version': python_version,
    }


def remove_venv(venv_dir: str) -> dict:
    venv_dir = (venv_dir or '').strip()
    if not venv_dir:
        return {'success': False, 'message': 'venv_dir 不能为空'}

    path = Path(venv_dir)
    if not path.exists():
        return {'success': True, 'data': {'skipped': True}, 'message': '目录已不存在'}

    if not path.is_dir():
        return {'success': False, 'message': '路径不是目录'}

    try:
        shutil.rmtree(str(path))
    except Exception as e:
        return {'success': False, 'message': f'删除目录失败: {e}'}

    return {'success': True, 'message': f'已删除: {venv_dir}'}