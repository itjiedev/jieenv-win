"""
python_installer provider 的 service 函数集合。

每个函数都对应 hooks.py 里 lifecycle / capabilities 引用的 (app_name, func_name)。
调用入口统一由 provider_catalog.invoke_provider() 分发，零硬编码 import。
"""
import logging
import os as _os
import subprocess

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# lifecycle.create：下载 + 静默安装 Python 解释器
# service_mode=context_dict → ctx 含 project_name + project_dir + config_schema 字段
# ═══════════════════════════════════════════════════════════════

def install_python(ctx: dict) -> dict:
    from .python_installer import run_install

    options = {
        'version': ctx['version'],
        'install_dir': ctx.get('install_dir', '') or None,
        'download_source': ctx.get('download_source', 'auto'),
        'add_to_path': ctx.get('add_to_path', False),
        'install_pip': ctx.get('install_pip', True),
        'install_py': ctx.get('install_py', True),
    }
    try:
        result = run_install(options)
        return {'success': True, 'data': result}
    except Exception as e:
        logger.exception('install_python failed')
        return {
            'success': False,
            'error': f'Python {options["version"]} 安装失败: {e}',
        }


# ═══════════════════════════════════════════════════════════════
# lifecycle.preflight：引擎在 create 之前自动调
# 返回 errors 阻断创建、warnings 仅提醒但允许继续
# ═══════════════════════════════════════════════════════════════

def preflight_python_install(ctx: dict) -> dict:
    from . import storage, python_download

    errors = []
    warnings = []
    version = (ctx.get('version') or '').strip()
    install_dir = (ctx.get('install_dir', '') or '').strip()
    download_source = (ctx.get('download_source', 'auto') or 'auto').strip()

    if not version:
        errors.append('Python 版本未填写')
        return {'success': not errors, 'errors': errors, 'warnings': warnings}

    existing = storage.get_installed(version)
    if existing:
        ex_dir = existing.get('install_dir', '')
        if install_dir and _os.path.isdir(install_dir):
            if _os.path.normpath(ex_dir) == _os.path.normpath(install_dir):
                errors.append(
                    f'Python {version} 已安装到 {install_dir}，请更换目录或先卸载'
                )
        else:
            warnings.append(
                f'Python {version} 已存在于 {ex_dir}，本次将在目标目录再装一份'
            )

    if install_dir:
        sensitive_prefixes = [
            'C:\\Windows',
            'C:\\Program Files',
            'C:\\Program Files (x86)',
            'C:\\ProgramData',
        ]
        for sp in sensitive_prefixes:
            if install_dir.lower().startswith(sp.lower()):
                errors.append(f'不建议安装到系统目录 {sp} 下')
                break

        parent = _os.path.dirname(install_dir.rstrip('\\/'))
        if parent and _os.path.isdir(parent):
            if not _os.access(parent, _os.W_OK):
                errors.append(f'安装目录的父路径 {parent} 不可写')

    if download_source and download_source != 'auto':
        source_url = ''
        for src in getattr(python_download, 'DOWNLOAD_SOURCES', []):
            if src.get('name') == download_source:
                source_url = src.get('url', '')
                break
        if source_url:
            try:
                reachable = python_download.probe_url(source_url, timeout=3)
                if not reachable:
                    warnings.append(
                        f'下载源「{download_source}」当前不可达，安装时将自动探测其他可用源'
                    )
            except Exception:
                logger.debug('preflight probe_source failed: %s', download_source)

    return {'success': not errors, 'errors': errors, 'warnings': warnings}


# ═══════════════════════════════════════════════════════════════
# lifecycle.delete：静默卸载（kwargs 模式，仅传 version）
# ═══════════════════════════════════════════════════════════════

def uninstall_python(version: str) -> dict:
    from .python_installer import run_uninstall
    try:
        result = run_uninstall(version)
        ok = bool(result.get('success', True))
        return {'success': ok, 'data': result}
    except Exception as e:
        logger.exception('uninstall_python failed')
        return {'success': False, 'error': f'Python {version} 卸载失败: {e}'}


# ═══════════════════════════════════════════════════════════════
# lifecycle.verify / health：验证指定版本 Python 是否可用
# health 后台 30s 定时调，失败会把 provider 标记为 DEGRADED
# ═══════════════════════════════════════════════════════════════

def verify_python(version: str) -> dict:
    from . import storage

    record = storage.get_installed(version)
    if not record:
        return {'success': False, 'error': f'Python {version} 未找到安装记录'}

    exe = record.get('python_exe', '')
    if not exe or not _os.path.isfile(exe):
        return {
            'success': False,
            'error': f'Python {version} 可执行文件缺失: {exe}',
        }

    try:
        result = subprocess.run(
            [exe, '--version'],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0x08000000,
        )
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': f'Python {version} --version 超时'}
    except Exception as e:
        return {'success': False, 'error': f'Python {version} 验证异常: {e}'}

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'Python {version} 执行异常: {result.stderr.strip()}',
        }

    return {
        'success': True,
        'data': {
            'version': version,
            'python_exe': exe,
            'install_dir': record.get('install_dir', ''),
            'version_output': result.stdout.strip(),
        },
    }


# ═══════════════════════════════════════════════════════════════
# provider 级别健康检测（无参数，_ProviderHealthCache 定时调）
# 遍历所有已安装版本逐个 verify_python，返回汇总
# ═══════════════════════════════════════════════════════════════

def provider_health_check() -> dict:
    from . import storage
    try:
        records = storage.list_installed()
    except Exception as e:
        return {'success': False, 'error': f'storage 访问失败: {e}'}
    if not records:
        return {'success': True, 'data': {'total': 0, 'healthy': 0}}
    healthy = sum(1 for r in records
                  if verify_python(r.get('version', '')).get('success'))
    return {'success': True, 'data': {'total': len(records), 'healthy': healthy}}


# ═══════════════════════════════════════════════════════════════
# capabilities.list_available_versions
# options_from 下拉用，必须返回 list[dict] 带 value / label / default / version
# ═══════════════════════════════════════════════════════════════

def list_available_python_versions() -> list:
    from . import storage

    installed = storage.list_installed()
    installed_versions = {
        r.get('version') for r in installed if r.get('version')
    }

    known_versions = [
        '3.14.0', '3.13.3', '3.13.2', '3.13.1', '3.13.0',
        '3.12.9', '3.12.8', '3.12.7', '3.12.6',
        '3.11.11', '3.11.10', '3.11.9',
        '3.10.15', '3.10.14',
        '3.9.20', '3.9.19',
    ]

    all_versions = sorted(
        set(known_versions) | installed_versions,
        reverse=True,
    )

    options = []
    for v in all_versions:
        is_installed = v in installed_versions
        label = f'Python {v}'
        if is_installed:
            label += '  ✅ 已安装'
        options.append({
            'value': v,
            'label': label,
            'default': v == '3.12.9',
            'version': v,
        })

    return options


# ═══════════════════════════════════════════════════════════════
# capabilities.list_sources：下载源选项
# ═══════════════════════════════════════════════════════════════

def list_download_sources() -> list:
    from . import python_download

    sources = python_download.list_sources()
    default_name = python_download.get_default_source()

    options = []
    for s in sources:
        name = s.get('name', '')
        if name == 'auto':
            label = '自动探测（推荐）'
        else:
            label = s.get('display_name') or s.get('url', '') or name
        is_default = (default_name == name) or (default_name is None and name == 'auto')
        options.append({
            'value': name,
            'label': label,
            'default': is_default,
            'version': '',
        })

    return options


# ═══════════════════════════════════════════════════════════════
# capabilities.scan_project_env：扫描项目目录是否已有 venv
# ═══════════════════════════════════════════════════════════════

def scan_project_python_env(ctx: dict) -> dict:
    project_dir = ctx.get('project_dir', '')
    if not project_dir or not _os.path.isdir(project_dir):
        return {'success': True, 'data': {'has_python_env': False}}

    candidates = ['.venv', 'venv', 'env', '.env']
    found = []
    for name in candidates:
        venv_dir = _os.path.join(project_dir, name)
        if _os.path.isdir(venv_dir):
            exe = _os.path.join(venv_dir, 'Scripts', 'python.exe')
            if _os.path.isfile(exe):
                found.append({'name': name, 'python_exe': exe})

    return {
        'success': True,
        'data': {
            'has_python_env': len(found) > 0,
            'venvs_found': found,
        },
    }