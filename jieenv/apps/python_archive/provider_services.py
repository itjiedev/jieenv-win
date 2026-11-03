"""
python_archive provider 的 service 函数集合。

每个函数都对应 hooks.py 里 lifecycle / capabilities 引用的 (app_name, func_name)。
调用入口统一由 provider_catalog.invoke_provider() 分发，零硬编码 import。
"""
import logging
import os as _os
import subprocess

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# lifecycle.create：下载绿色版 zip/nupkg + 解压安装
# service_mode=context_dict → ctx 含 project_name + project_dir + config_schema 字段
# ═══════════════════════════════════════════════════════════════

def install_python_archive(ctx: dict) -> dict:
    from .python_install import run_install

    version_id = ctx.get('version_id', '').strip()
    version = ctx.get('version', '').strip()
    name = ctx.get('name', '').strip() or f'Python-{version}'
    install_dir = ctx.get('install_dir', '') or None
    download_source = ctx.get('download_source', 'auto')

    if not version_id or not version:
        return {
            'success': False,
            'error': f'缺少必要参数: version_id={version_id}, version={version}',
        }

    options = {
        'version_id': version_id,
        'version': version,
        'name': name,
        'install_dir': install_dir,
        'download_source': download_source,
    }
    try:
        result = run_install(options)
        return {'success': True, 'data': result}
    except Exception as e:
        logger.exception('install_python_archive failed')
        return {
            'success': False,
            'error': f'Python {version} 绿色版安装失败: {e}',
        }


# ═══════════════════════════════════════════════════════════════
# lifecycle.preflight：引擎在 create 之前自动调
# 返回 errors 阻断创建、warnings 仅提醒但允许继续
# ═══════════════════════════════════════════════════════════════

def preflight_python_archive(ctx: dict) -> dict:
    from . import storage, python_download

    errors = []
    warnings = []
    version_id = (ctx.get('version_id') or '').strip()
    version = (ctx.get('version') or '').strip()
    name = (ctx.get('name') or '').strip()
    install_dir = (ctx.get('install_dir', '') or '').strip()
    download_source = (ctx.get('download_source', 'auto') or 'auto').strip()

    if not version_id:
        errors.append('版本 ID 未填写')
    if not version:
        errors.append('Python 版本号未填写')
    if errors:
        return {'success': False, 'errors': errors, 'warnings': warnings}

    if version_id:
        version_info = storage.get_version_by_id(version_id)
        if not version_info:
            errors.append(f'版本 ID {version_id} 在版本缓存中不存在')

    if name and storage.name_exists(name):
        errors.append(f'环境名称「{name}」已存在，请换一个')

    existing_by_version = storage.get_installed(version)
    if existing_by_version:
        ex_dir = existing_by_version.get('install_dir', '')
        if install_dir and _os.path.normpath(ex_dir) == _os.path.normpath(install_dir):
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
# lifecycle.delete：静默卸载（kwargs 模式，传 record_id）
# ═══════════════════════════════════════════════════════════════

def uninstall_python_archive(record_id: str) -> dict:
    from .python_install import run_uninstall
    try:
        result = run_uninstall(record_id)
        ok = bool(result.get('success', True))
        return {'success': ok, 'data': result}
    except Exception as e:
        logger.exception('uninstall_python_archive failed')
        return {'success': False, 'error': f'Python 绿色版卸载失败: {e}'}


# ═══════════════════════════════════════════════════════════════
# lifecycle.verify：验证指定 record_id 的 Python 是否可用
# ═══════════════════════════════════════════════════════════════

def verify_python_archive(record_id: str) -> dict:
    from . import storage

    record = storage.get_by_id(record_id)
    if not record:
        return {'success': False, 'error': f'记录 {record_id} 不存在'}

    exe = record.get('python_exe', '')
    if not exe or not _os.path.isfile(exe):
        return {
            'success': False,
            'error': f'Python 可执行文件缺失: {exe}',
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
        return {'success': False, 'error': 'Python --version 超时'}
    except Exception as e:
        return {'success': False, 'error': f'Python 验证异常: {e}'}

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'Python 执行异常: {result.stderr.strip()}',
        }

    return {
        'success': True,
        'data': {
            'record_id': record_id,
            'name': record.get('name', ''),
            'version': record.get('version', ''),
            'python_exe': exe,
            'install_dir': record.get('install_dir', ''),
            'version_output': result.stdout.strip(),
        },
    }


# ═══════════════════════════════════════════════════════════════
# lifecycle.health：后台 30s 定时调
# 用 verify 同一套逻辑，record_id 不存在视为 provider 部分降级
# ═══════════════════════════════════════════════════════════════

def health_check(record_id: str) -> dict:
    from . import storage

    record = storage.get_by_id(record_id)
    if not record:
        return {'success': False, 'error': f'记录 {record_id} 不存在'}

    exe = record.get('python_exe', '')
    if not exe or not _os.path.isfile(exe):
        return {
            'success': False,
            'error': f'Python 可执行文件缺失: {exe}',
        }

    try:
        result = subprocess.run(
            [exe, '--version'],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0x08000000,
        )
    except Exception as e:
        return {'success': False, 'error': str(e)}

    if result.returncode != 0:
        return {
            'success': False,
            'error': result.stderr.strip() or 'Python 不可执行',
        }

    return {
        'success': True,
        'data': {'python_exe': exe, 'version_output': result.stdout.strip()},
    }


# ═══════════════════════════════════════════════════════════════
# provider 级别健康检测（无参数，_ProviderHealthCache 定时调）
# 遍历所有已安装记录逐个 health_check，返回汇总
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
                  if health_check(r.get('id', '')).get('success'))
    return {'success': True, 'data': {'total': len(records), 'healthy': healthy}}


# ═══════════════════════════════════════════════════════════════
# capabilities.list_available_versions
# options_from 下拉用，必须返回 list[dict] 带 value / label / default
# ═══════════════════════════════════════════════════════════════

def list_available_python_versions() -> list:
    from . import storage
    from .python_versions import get_python_versions

    versions_data = get_python_versions()
    cached = versions_data.get('versions', [])

    installed = storage.list_installed()
    installed_ids = {r.get('version_id') for r in installed if r.get('version_id')}

    options = []
    for v in cached:
        vid = v.get('id', '')
        ver = v.get('version', '')
        ext = v.get('ext', '')
        label = f'Python {ver} ({ext})'
        if vid in installed_ids:
            label += '  ✅ 已安装'
        options.append({
            'value': vid,
            'label': label,
            'default': vid.endswith('3.13.5') or vid.endswith('3.12.9'),
            'version': ver,
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