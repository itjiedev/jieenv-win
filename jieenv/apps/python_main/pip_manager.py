"""
pip 包管理通用模块

提供跨环境统一的 pip 包管理能力，供 python 应用自身和贡献者应用调用。
所有操作通过指定 python 解释器路径执行，不依赖当前运行的 Python。

使用方式::

    from apps.python_main.pip_manager import list_packages, install_package

    # 列出某个环境下的包
    packages = list_packages('C:/Python313/python.exe')

    # 安装包
    install_package('C:/Python313/python.exe', 'requests')
"""
import os
import json
import time
import tempfile
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# 子进程隐藏窗口标志
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def _child_env() -> dict:
    """返回调用「目标环境」python 子进程时应使用的环境变量。

    三处关键处理：
    1. 剔除 PYTHONHOME / PYTHONPATH / PYTHONSTARTUP：本服务进程（runtime python，
       常由 PyCharm 等 IDE 注入指向 runtime\\Lib 的 PYTHONPATH）若把这些变量原样传
       给版本不同的目标解释器，目标 python 会去加载 runtime 的标准库，触发
       "Error in sitecustomize ... AssertionError: SRE module mismatch"（re/_sre
       版本错配）等导入错误；剔除后目标 python 只用自身标准库，与 venv 启动子解释
       器时 pop 掉这些变量的做法一致。
    2. 置 PYTHONUTF8 / PYTHONIOENCODING 强制 UTF-8：Windows 下子进程被管道重定向时
       默认按本地代码页编码 stdout，中文摘要会乱码；读取端统一按 UTF-8 解码。
    3. 置 PYTHONNOUSERSITE=1：Python 用户 site-packages（如
       C:\\Users\\{user}\\AppData\\Roaming\\Python\\Python312\\site-packages）按次版本
       号共享，所有 3.12.x 的 python.exe 都会自动将其加入 sys.path，导致 pip list
       显示不属于目标环境的包。设置此变量后目标 python 只搜索自身安装目录下的
       site-packages，pip list 结果与目标环境严格对应。
    """
    env = os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
        env.pop(key, None)
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONNOUSERSITE'] = '1'
    return env


def _run_python(python_exe: str, args: list, timeout: int = 120) -> tuple:
    """
    执行 python 子进程，返回 (returncode, stdout, stderr)。
    """
    if not Path(python_exe).exists():
        return 1, '', f'Python 解释器不存在: {python_exe}'
    try:
        proc = subprocess.run(
            [python_exe] + args,
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
            env=_child_env(),
        )
        out = (proc.stdout or b'').decode('utf-8', errors='replace')
        err = (proc.stderr or b'').decode('utf-8', errors='replace')
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        return 1, '', '命令执行超时'
    except Exception as e:
        return 1, '', str(e)


def list_packages(python_exe: str) -> list:
    """
    列出指定 Python 环境下已安装的包。
    返回 [{name, version}, ...]
    """
    code, out, err = _run_python(python_exe, ['-m', 'pip', 'list', '--format=json'])
    if code != 0:
        logger.warning('pip list 失败: %s', err)
        return []
    try:
        data = json.loads(out)
        return [{'name': p.get('name', ''), 'version': p.get('version', '')}
                for p in data if isinstance(p, dict)]
    except (json.JSONDecodeError, TypeError):
        return []


def install_package(python_exe: str, package: str, upgrade: bool = False) -> dict:
    """
    在指定 Python 环境下安装包。
    返回 {success, message}
    """
    args = ['-m', 'pip', 'install']
    if upgrade:
        args.append('--upgrade')
    args.append(package)
    code, out, err = _run_python(python_exe, args, timeout=300)
    if code == 0:
        return {'success': True, 'message': f'安装成功: {package}'}
    return {'success': False, 'message': f'安装失败: {err.strip() or out.strip()}'[:200]}


def uninstall_package(python_exe: str, package: str) -> dict:
    """
    在指定 Python 环境下卸载包。
    返回 {success, message}
    """
    args = ['-m', 'pip', 'uninstall', '-y', package]
    code, out, err = _run_python(python_exe, args, timeout=120)
    if code == 0:
        return {'success': True, 'message': f'卸载成功: {package}'}
    return {'success': False, 'message': f'卸载失败: {err.strip() or out.strip()}'[:200]}


def get_pip_version(python_exe: str) -> str:
    """获取指定环境下 pip 版本号"""
    code, out, err = _run_python(python_exe, ['-m', 'pip', '--version'])
    if code == 0:
        # 输出如: pip 24.0 from C:\Python313\...
        parts = out.strip().split()
        if len(parts) >= 2:
            return parts[1]
    return ''


def upgrade_pip(python_exe: str) -> dict:
    """升级指定环境下的 pip"""
    args = ['-m', 'pip', 'install', '--upgrade', 'pip']
    code, out, err = _run_python(python_exe, args, timeout=120)
    if code == 0:
        return {'success': True, 'message': 'pip 升级成功'}
    return {'success': False, 'message': f'pip 升级失败: {err.strip()}'[:200]}


# ── 可升级检测 / 流式安装（供后台任务实时回报日志）────────────

def list_outdated(python_exe: str) -> list:
    """列出指定环境下有新版本可升级的包。

    返回 [{name, version, latest_version}, ...]。该命令会逐个查询索引，耗时较长。
    """
    code, out, err = _run_python(python_exe,
                                 ['-m', 'pip', 'list', '--outdated', '--format=json'],
                                 timeout=300)
    if code != 0:
        logger.warning('pip list --outdated 失败: %s', err)
        return []
    try:
        data = json.loads(out)
        return [{'name': p.get('name', ''),
                 'version': p.get('version', ''),
                 'latest_version': p.get('latest_version', '')}
                for p in data if isinstance(p, dict)]
    except (json.JSONDecodeError, TypeError):
        return []


def run_pip_stream(python_exe: str, args: list, on_output,
                   timeout: int = 1800) -> int:
    """流式执行 ``python -m pip <args>``，逐行回调 on_output(line)，返回退出码。

    合并 stderr 到 stdout 并关闭 pip 进度条（调用方在 args 里加 --progress-bar off），
    使安装/升级过程能实时反映到 JieTaskLog 日志窗；超时后终止子进程并返回非 0。
    """
    if not Path(python_exe).exists():
        on_output(f'Python 解释器不存在: {python_exe}')
        return 1
    try:
        proc = subprocess.Popen(
            [python_exe, '-m', 'pip'] + args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding='utf-8', errors='replace', bufsize=1,
            creationflags=_CREATE_NO_WINDOW, env=_child_env())
    except Exception as e:
        on_output(f'启动 pip 失败: {e}')
        return 1
    start = time.time()
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                on_output(line)
            if time.time() - start > timeout:
                proc.kill()
                on_output(f'命令执行超时（超过 {timeout // 60} 分钟），已终止')
                return 1
    except Exception as e:
        proc.kill()
        on_output(f'读取 pip 输出失败: {e}')
        return 1
    proc.wait()
    return proc.returncode


def install_package_stream(python_exe: str, package: str, on_output,
                           upgrade: bool = False, timeout: int = 1800) -> dict:
    """流式安装/升级单个包（或 name==version 规格），实时回调日志。

    返回 {success, message}。upgrade=True 时追加 --upgrade。
    """
    args = ['install', '--progress-bar', 'off']
    if upgrade:
        args.append('--upgrade')
    args.append(package)
    verb = '升级' if upgrade else '安装'
    on_output(f'开始{verb}: {package}（解释器 {python_exe}）')
    code = run_pip_stream(python_exe, args, on_output, timeout)
    if code == 0:
        return {'success': True, 'message': f'{verb}成功: {package}'}
    return {'success': False, 'message': f'{verb}失败: {package}（退出码 {code}）'}


def upgrade_packages_stream(python_exe: str, packages: list, on_output,
                            timeout: int = 3600) -> dict:
    """流式批量升级多个包（一键全升），实时回调日志。返回 {success, message}。"""
    packages = [p for p in (packages or []) if p]
    if not packages:
        return {'success': False, 'message': '没有需要升级的包'}
    args = ['install', '--upgrade', '--progress-bar', 'off'] + packages
    on_output(f'开始批量升级 {len(packages)} 个包: {", ".join(packages)}')
    code = run_pip_stream(python_exe, args, on_output, timeout)
    if code == 0:
        return {'success': True, 'message': f'已升级 {len(packages)} 个包'}
    return {'success': False, 'message': f'批量升级失败（退出码 {code}）'}


def export_requirements(python_exe: str) -> dict:
    """导出指定环境的 requirements.txt 文本（pip freeze）。

    返回 {success, content|message}。content 为完整 requirements 文本（空环境返回空串）。
    """
    code, out, err = _run_python(python_exe, ['-m', 'pip', 'freeze'], timeout=120)
    if code != 0:
        logger.warning('pip freeze 失败: %s', err)
        return {'success': False, 'message': f'导出失败: {err.strip() or out.strip()}'[:200]}
    return {'success': True, 'content': out}


def install_requirements_stream(python_exe: str, specs: list, on_output,
                                 timeout: int = 3600) -> dict:
    """流式批量安装 requirements 有效规格（单次 pip 会话），实时回调日志。

    实现方式：将 specs 逐行写入临时 requirements 文件，执行
    ``pip install -r <临时文件> --progress-bar off``；临时文件 finally 删除
    （异常路径同样清理）。
    采用临时文件而非拼包列表：Windows CreateProcess 命令行 32767 字符上限下，
    数百条规格即触顶，-r 文件方式无长度限制且保持单次 pip 会话。
    头部摘要日志由调用方（worker）负责输出，本函数不再重复。
    返回 {success, message}，退出码语义沿用 run_pip_stream（0=成功）。
    """
    specs = [s for s in (specs or []) if s]
    if not specs:
        return {'success': False, 'message': '没有需要安装的依赖'}
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                         encoding='utf-8', delete=False) as tmp:
            tmp.write('\n'.join(specs))
            tmp_path = tmp.name
        # Windows 下必须先关闭文件再交给 pip 子进程读取（NamedTemporaryFile 文件锁）
        code = run_pip_stream(python_exe,
                              ['install', '-r', tmp_path, '--progress-bar', 'off'],
                              on_output, timeout)
        if code == 0:
            return {'success': True, 'message': f'已安装 {len(specs)} 个包'}
        return {'success': False,
                'message': f'批量安装失败（退出码 {code}，共 {len(specs)} 个包）'}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass