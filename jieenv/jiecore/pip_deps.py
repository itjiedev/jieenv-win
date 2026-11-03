"""
第三方包（pip）依赖管理
处理扩展应用 pip_dependencies 的声明检测与自动安装。

应用在自己的 app_meta.py 中声明:
    'pip_dependencies': {
        'requests': '>=2.28.0',
        'openpyxl': '==3.1.2',
    }
安装/启用/升级应用时由 jiecore 检测并自动安装缺失的包。

安全设计:
- 包名按 PEP 503 命名规范严格校验，拒绝非法包名；
- 一律通过 subprocess 参数列表调用 pip，绝不使用 shell，杜绝命令注入；
- 安装时附带 constraints 文件锁定当前 Django 版本，防止 pip 连带升级核心框架；
- 安装完成后用 importlib.metadata 二次验证结果。

卸载约定:
- 卸载/删除应用时不自动 pip uninstall，因为多个应用可能共用同一个包，
  误删会破坏其他应用；残留的包仅占用空间，无副作用。
"""
import re
import os
import sys
import time
import subprocess
import logging
from importlib import metadata as importlib_metadata
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# 合法的 PyPI 包名（字母/数字开头结尾，中间允许 . _ -），用于拦截恶意包名
PIP_NAME_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$')

# 单次 pip install 的超时时间（秒）
PIP_INSTALL_TIMEOUT = 600

# 子进程隐藏窗口标志
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def normalize_pkg_name(name: str) -> str:
    """PEP 503 包名规范化：小写，且 -/_/. 等价"""
    return re.sub(r'[-_.]+', '-', str(name)).lower()


def get_installed_version(pkg_name: str) -> str:
    """
    查询包当前已安装的版本，未安装返回空字符串。
    使用 importlib.metadata 而非 import，避免包名与导入名不一致
    （如 Pillow -> PIL）以及导入副作用。
    """
    try:
        return importlib_metadata.version(normalize_pkg_name(pkg_name))
    except Exception:
        return ''


def to_requirement(pkg_name: str, specifier: str) -> str:
    """
    将包名与版本约束拼装为 pip 需求表达式。
    纯版本号（无运算符）按 >= 处理，与 check_version_match 语义一致。
    """
    specifier = str(specifier or '').strip()
    if not specifier:
        return pkg_name
    if specifier[0].isdigit():
        specifier = '>=' + specifier
    return f'{pkg_name}{specifier}'


def check_pip_dependencies(meta: dict) -> tuple:
    """
    检测 app_meta 声明的 pip 第三方包依赖。
    返回 (missing, invalid):
      missing: [(pkg_name, specifier), ...] 未安装或版本不满足约束的包
      invalid: [pkg_name, ...] 不符合 PEP 503 命名、拒绝处理的包名
    """
    from .app_manager import check_version_match
    deps = meta.get('pip_dependencies') or {}
    missing, invalid = [], []
    for name, specifier in deps.items():
        name = str(name).strip()
        specifier = str(specifier or '').strip()
        if not PIP_NAME_RE.match(name):
            invalid.append(name)
            continue
        installed = get_installed_version(name)
        if not installed:
            missing.append((name, specifier))
        elif specifier and not check_version_match(installed, specifier):
            missing.append((name, specifier))
    return missing, invalid


def build_core_constraints() -> Path:
    """
    生成锁定核心框架当前版本的 constraints 文件。
    pip 安装依赖时禁止连带升级 Django，避免破坏整个项目。
    """
    import django
    constraint_file = settings.UPGRADE_TMP_DIR / 'pip_constraints.txt'
    constraint_file.parent.mkdir(parents=True, exist_ok=True)
    constraint_file.write_text(f'Django=={django.__version__}\n', encoding='utf-8')
    return constraint_file


def ensure_pip_dependencies(meta: dict, progress_cb=None) -> dict:
    """
    检测并自动安装应用声明的 pip 第三方包依赖。
    返回 {'success': bool, 'message': str, 'installed': [pkg_name, ...]}
    installed 为本次新安装的包列表（为空表示环境无变化）。
    任一包安装失败即整体失败，调用方应中止应用安装/启用/升级。

    progress_cb(message, progress=None, stage=None): 可选进度回调，每一步调用一次。
    """
    missing, invalid = check_pip_dependencies(meta)
    if invalid:
        if progress_cb:
            progress_cb(f'第三方包名不合法: {", ".join(invalid)}')
        return {'success': False, 'installed': [],
                'message': f'第三方包名不合法: {", ".join(invalid)}'}
    if not missing:
        if progress_cb:
            progress_cb('pip 依赖检测完成，无需新安装包')
        return {'success': True, 'installed': [], 'message': ''}

    requirements = [to_requirement(n, s) for n, s in missing]
    logger.info('开始安装第三方包: %s', ', '.join(requirements))
    if progress_cb:
        progress_cb(f'开始 pip 安装: {", ".join(requirements)}', stage='pip 安装')

    constraint_file = None
    try:
        constraint_file = build_core_constraints()
        cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check']
        index_url = getattr(settings, 'PIP_INDEX_URL', '')
        if index_url:
            cmd += ['--index-url', index_url]
        cmd += ['--constraint', str(constraint_file)]
        cmd += requirements
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, timeout=PIP_INSTALL_TIMEOUT,
                              creationflags=_CREATE_NO_WINDOW)
        elapsed = time.time() - t0
        stdout = (proc.stdout or b'').decode('utf-8', errors='replace')
        stderr = (proc.stderr or b'').decode('utf-8', errors='replace')
        if proc.returncode != 0:
            tail = '\n'.join((stderr or stdout).splitlines()[-10:])
            logger.error('pip 安装失败:\n%s', tail)
            if progress_cb:
                progress_cb(f'pip 安装失败 (耗时 {elapsed:.1f}s): {tail.strip()[:300]}', stage='pip 失败')
            return {'success': False, 'installed': [],
                    'message': f'第三方包安装失败: {", ".join(requirements)}；{tail.strip()[:500]}'}
        if progress_cb:
            progress_cb(f'pip 安装完成 (耗时 {elapsed:.1f}s)', stage='pip 完成')
    except subprocess.TimeoutExpired:
        logger.error('pip 安装超时(>%ss): %s', PIP_INSTALL_TIMEOUT, ', '.join(requirements))
        if progress_cb:
            progress_cb(f'pip 安装超时(>{PIP_INSTALL_TIMEOUT}s): {", ".join(requirements)}', stage='pip 超时')
        return {'success': False, 'installed': [],
                'message': f'第三方包安装超时: {", ".join(requirements)}'}
    except Exception as e:
        logger.exception('执行 pip 安装失败')
        if progress_cb:
            progress_cb(f'pip 进程执行失败: {e}', stage='pip 异常')
        return {'success': False, 'installed': [], 'message': f'pip 进程执行失败: {e}'}
    finally:
        if constraint_file and constraint_file.exists():
            try:
                constraint_file.unlink()
            except OSError:
                pass

    still_missing = [n for n, s in missing if not get_installed_version(n)]
    if still_missing:
        if progress_cb:
            progress_cb(f'pip 执行完成但包仍未安装: {", ".join(still_missing)}', stage='pip 校验失败')
        return {'success': False, 'installed': [],
                'message': f'pip 执行完成但以下包仍未成功安装: {", ".join(still_missing)}'}

    logger.info('第三方包安装完成: %s', ', '.join(n for n, _ in missing))
    return {'success': True, 'installed': [n for n, _ in missing], 'message': ''}