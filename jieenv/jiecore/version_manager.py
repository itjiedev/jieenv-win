"""
版本管理与升级模块
处理应用的版本比对、升级包解压、文件覆盖、数据库更新和服务重启。

升级流程（安全顺序）：
1. 版本比对（包版本必须高于当前版本）
2. 解压校验 + 依赖检测
3. 备份当前版本文件
4. 覆盖应用文件
5. 执行数据库迁移（无论启用与否，保持库结构同步）
6. 执行应用自带的升级程序（可选 upgrade.py）
7. 更新注册信息与全部缓存
8. 任一步骤失败：恢复文件备份（注册信息尚未变更，无需回滚）
"""
import os
import shutil
import zipfile
import time
import logging
import importlib.util
from pathlib import Path
from typing import Optional

from django.conf import settings

from . import cache_handler as cache
from .utils import parse_version
from .app_manager import (
    read_app_meta_from_disk, run_migrations,
    check_migrations_bundled, read_changelog_from_dir,
    update_enabled_cache, format_dep, get_core_app_dir, AppManager,
    is_framework_app, get_platform_fixed_apps,
)

logger = logging.getLogger(__name__)


def _make_ignore(*exclude_dirs: Path):
    """
    生成 copytree 可用的 ignore 回调，排除指定的绝对目录。
    防止备份/覆盖时把升级临时目录、备份目标本身又递归复制进去，
    导致路径无限嵌套最终触发 WinError 206（文件名或扩展名太长）。
    """
    exclude_resolved = {p.resolve() for p in exclude_dirs}

    def _ignore(src, names):
        src_path = Path(src).resolve()
        ignored = set()
        for name in names:
            if (src_path / name).resolve() in exclude_resolved:
                ignored.add(name)
        return ignored

    return _ignore


def _get_platform_version() -> str:
    """
    读取当前平台版本。
    遍历所有 is_platform_fixed 骨架应用，找第一个声明了 platform_version 字段的；
    全没有则回退到 jiecore 自身 version。
    """
    for app_name in get_platform_fixed_apps():
        app_meta_path = get_core_app_dir(app_name) / 'app_meta.py'
        if app_meta_path.exists():
            try:
                spec = importlib.util.spec_from_file_location('_pv_check', app_meta_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                pv = getattr(mod, 'meta', {}).get('platform_version', '')
                if pv:
                    return pv
            except Exception:
                pass
    disk_meta = read_app_meta_from_disk('jiecore')
    return disk_meta.get('version', '')


def _get_platform_changelog_text() -> str:
    """
    读取平台 changelog 文本。
    遍历所有 is_platform_fixed 骨架应用目录，找第一个存在 platform_changelog.txt 的；
    都没有返回空字符串。
    """
    for app_name in get_platform_fixed_apps():
        cl_path = get_core_app_dir(app_name) / 'platform_changelog.txt'
        if cl_path.exists():
            try:
                return cl_path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                continue
    return ''


def check_platform_upgrade_available(zip_path: str) -> dict:
    """
    检查平台升级包是否可用。
    平台升级包文件名格式：platform-<version>.zip
    返回 {'can_upgrade': bool, 'current_version': str, 'new_version': str, 'message': str}
    """
    zip_file = Path(zip_path)
    if not zip_file.exists():
        return {'can_upgrade': False, 'message': f'文件不存在: {zip_path}',
                'current_version': '', 'new_version': ''}

    stem = zip_file.stem
    parts = stem.rsplit('-', 1)
    if len(parts) != 2 or parts[0] != 'platform':
        return {'can_upgrade': False, 'message': '文件名格式错误，应为 platform-version.zip',
                'current_version': '', 'new_version': ''}

    _, new_version = parts
    current_version = _get_platform_version()
    if not current_version:
        return {'can_upgrade': False, 'message': '无法获取当前平台版本',
                'current_version': '', 'new_version': new_version}

    can = parse_version(new_version) > parse_version(current_version)
    return {
        'can_upgrade': can,
        'current_version': current_version,
        'new_version': new_version,
        'message': f'平台可以升级 {current_version} -> {new_version}' if can
        else f'当前平台版本 {current_version} 已是最新(包版本 {new_version})',
    }


def upgrade_platform_from_zip(zip_path: str, progress_cb=None) -> dict:
    """
    平台升级主函数。
    解压 zip → 扫描实际内容 → 备份已有条目 → 覆盖（有什么覆盖什么）→
    固定APP全部迁移+钩子+注册信息更新+pip依赖 → 刷新缓存/菜单/路由 → 清理备份。
    任一步骤失败自动回滚。

    progress_cb(message, progress=None, stage=None): 可选进度回调。
    """
    _cb = progress_cb or (lambda *a, **kw: None)
    _cb('开始平台升级', stage='解析升级包')
    t_all = time.time()

    zip_file = Path(zip_path)
    if not zip_file.exists():
        return {'success': False, 'message': f'文件不存在: {zip_path}'}

    check = check_platform_upgrade_available(str(zip_file))
    if not check['can_upgrade']:
        return {'success': False, 'message': check['message']}

    new_version = check['new_version']
    _cb(f'目标版本: {new_version}', progress=5)

    tmp_dir = settings.UPGRADE_TMP_DIR / 'platform'
    platform_backup_dir = settings.UPGRADE_TMP_DIR / 'platform_backup'
    env_root = settings.ENV_ROOT

    _zip_path_obj = Path(zip_file).resolve()

    if settings.UPGRADE_TMP_DIR.exists():
        for child in settings.UPGRADE_TMP_DIR.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file() and child.resolve() != _zip_path_obj:
                    child.unlink(missing_ok=True)
            except Exception:
                pass
    settings.UPGRADE_TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True)

    try:
        _cb(f'解压平台升级包...', progress=10, stage='解压')
        t_unzip = time.time()
        with zipfile.ZipFile(zip_file, 'r') as zf:
            zf.extractall(tmp_dir)
        _cb(f'解压完成 (耗时 {time.time()-t_unzip:.1f}s)', progress=20)

        items_to_update = []
        for item in tmp_dir.iterdir():
            items_to_update.append(item.name)

        if not items_to_update:
            return {'success': False, 'message': '升级包中未发现任何可覆盖的文件'}
        _cb(f'发现 {len(items_to_update)} 个条目待覆盖: {", ".join(items_to_update)}', progress=25)

        _cb('备份已有条目...', progress=25, stage='备份')
        if platform_backup_dir.exists():
            shutil.rmtree(platform_backup_dir)
        platform_backup_dir.mkdir(parents=True, exist_ok=True)
        _ignore_upg = _make_ignore(settings.UPGRADE_TMP_DIR)
        backup_created = False
        original_files = {}
        t_backup = time.time()
        for name in items_to_update:
            src_path = env_root / name
            if src_path.exists():
                dest_path = platform_backup_dir / name
                if src_path.is_dir():
                    shutil.copytree(src_path, dest_path, ignore=_ignore_upg)
                    original_files[name] = _collect_dir_relpaths(src_path)
                else:
                    shutil.copy2(src_path, dest_path)
                    original_files[name] = set()
                backup_created = True
        _cb(f'备份完成 (耗时 {time.time()-t_backup:.1f}s)', progress=35)

        try:
            _cb('收集旧版本号...')
            old_versions = {}
            for app_name in get_platform_fixed_apps():
                ad = get_core_app_dir(app_name)
                if ad.exists():
                    old_versions[app_name] = read_app_meta_from_disk(app_name).get('version', '0.0.0')

            _cb('覆盖文件...', progress=40, stage='覆盖文件')
            t_cp = time.time()
            for name in items_to_update:
                pkg_path = tmp_dir / name
                dest_path = env_root / name
                if pkg_path.is_dir():
                    shutil.copytree(pkg_path, dest_path, dirs_exist_ok=True)
                else:
                    shutil.copy2(pkg_path, dest_path)
            _cb(f'文件覆盖完成 (耗时 {time.time()-t_cp:.1f}s)', progress=55)

            fixed_apps = [a for a in get_platform_fixed_apps() if get_core_app_dir(a).exists()]
            total_fixed = len(fixed_apps)
            if total_fixed:
                _cb(f'开始处理 {total_fixed} 个固定APP后处理...', progress=55, stage='固定APP后处理')
                for i, app_name in enumerate(fixed_apps, 1):
                    _cb(f'[{i}/{total_fixed}] 处理 {app_name} ...')
                    _post_upgrade_app(
                        app_name,
                        old_version=old_versions.get(app_name),
                        new_version=None,
                        skip_pip=False,
                        progress_cb=progress_cb,
                    )
                    per = 55 + int((i / total_fixed) * 30)
                    _cb(f'[{i}/{total_fixed}] {app_name} 完成', progress=per)
            else:
                _cb('无固定APP需要处理', progress=85)

            _cb('全局刷新缓存/菜单/路由...', progress=88, stage='全局刷新')
            cache.refresh_cache_from_db()
            from .menu_aggregator import refresh_menu_cache
            refresh_menu_cache()
            from .hot_reload import rebuild_urlconf
            rebuild_urlconf()
            _cb('全局刷新完成', progress=92)

            _cb('执行全量依赖检测...', progress=93, stage='依赖检测')
            dep_issues = AppManager.check_all_dependencies()
            dep_warning = ''
            if dep_issues:
                dep_warning = f'；发现 {len(dep_issues)} 个应用依赖不满足（见详情）'

            if platform_backup_dir.exists():
                shutil.rmtree(platform_backup_dir, ignore_errors=True)

            restart_required = True
            message = f'平台已升级至 {new_version}，已覆盖: {", ".join(items_to_update)}'
            if fixed_apps:
                message += f'；已处理固定APP迁移: {", ".join(fixed_apps)}'
            message += '（需重启服务生效）'
            if dep_warning:
                message += dep_warning

            _cb(f'平台升级完成 (总耗时 {time.time()-t_all:.1f}s)', progress=100, stage='完成')
            return {
                'success': True,
                'message': message,
                'new_version': new_version,
                'restart_required': restart_required,
            }

        except Exception as e:
            _cb(f'平台升级失败，开始回滚...', stage='回滚')
            if platform_backup_dir.exists():
                for name in items_to_update:
                    backup_path = platform_backup_dir / name
                    target_path = env_root / name
                    if not backup_path.exists():
                        continue
                    _rollback_merge(target_path, backup_path, original_files.get(name, set()))
                shutil.rmtree(platform_backup_dir, ignore_errors=True)
                _cb('回滚完成')
            logger.exception('平台升级失败，已回滚文件')
            _cb(f'平台升级失败: {e}', stage='失败')
            return {'success': False, 'message': f'平台升级失败并已回滚: {e}'}

    except Exception as e:
        logger.exception('平台升级失败')
        _cb(f'平台升级失败: {e}', stage='失败')
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if zip_file.exists():
            zip_file.unlink(missing_ok=True)


def _collect_dir_relpaths(dir_path: Path) -> set:
    """收集目录下所有文件的相对路径（用于回滚时清理升级新增文件）"""
    rels = set()
    if not dir_path.exists() or not dir_path.is_dir():
        return rels
    for root, _dirs, files in os.walk(dir_path):
        for f in files:
            full = Path(root) / f
            rels.add(str(full.relative_to(dir_path)).replace(os.sep, '/'))
    return rels


def _rollback_merge(target: Path, backup: Path, original_files: set) -> None:
    """
    合并模式回滚：先用备份恢复旧文件（dirs_exist_ok），
    再删掉升级期间新增的文件（original_files 里没有的）。
    """
    if backup.is_dir():
        shutil.copytree(backup, target, dirs_exist_ok=True)
        current_files = _collect_dir_relpaths(target)
        for rel in current_files - original_files:
            extra = target / rel
            if extra.exists():
                extra.unlink()
    else:
        if target.exists():
            target.unlink()
        shutil.copy2(backup, target)


def _post_upgrade_app(app_name: str, *, old_version: str = None,
                      new_version: str = None, skip_pip: bool = False,
                      progress_cb=None) -> None:
    """
    文件覆盖后对单个应用执行统一后处理：
    迁移 → 钩子 → pip 依赖 → 注册信息更新 → 缓存 → 热加载模块清理。
    应用升级和平台升级共用此函数，避免后处理代码重复。

    Args:
        app_name: 应用名
        old_version: 升级前版本号（钩子用），None 时从磁盘 app_meta 读取
        new_version: 升级后版本号（钩子用），None 时从磁盘 app_meta 读取
        skip_pip: 是否跳过 pip 依赖安装（应用升级已在覆盖前装过，传 True）
        progress_cb: 可选进度回调
    """
    _cb = progress_cb or (lambda *a, **kw: None)
    _cb(f'  [{app_name}] 执行数据库迁移...', stage='迁移')
    run_migrations(app_name, progress_cb=progress_cb)
    _cb(f'  [{app_name}] 迁移完成')

    if old_version is None:
        old_version = read_app_meta_from_disk(app_name).get('version', '0.0.0')
    if new_version is None:
        new_version = read_app_meta_from_disk(app_name).get('version', old_version)

    _cb(f'  [{app_name}] 检查 upgrade.py 钩子...')
    run_app_upgrade_hook(app_name, old_version, new_version)

    if is_framework_app(app_name):
        _cb(f'  [{app_name}] 框架内核，跳过 pip/注册后处理')
        return

    app_dir = get_core_app_dir(app_name)

    if not skip_pip:
        from .pip_deps import ensure_pip_dependencies
        new_meta = read_meta_from_path(app_dir)
        if new_meta:
            _cb(f'  [{app_name}] 检查 pip 依赖...', stage='pip 依赖')
            ensure_pip_dependencies(new_meta, progress_cb=progress_cb)

    _cb(f'  [{app_name}] 更新注册表与缓存...', stage='注册后处理')
    new_meta = read_meta_from_path(app_dir)
    if not new_meta:
        return

    app_info = {
        'version': new_meta.get('version', new_version),
        'display_name': new_meta.get('display_name', app_name),
        'dependencies': new_meta.get('dependencies', {}),
        'description': new_meta.get('description', ''),
        'changelog': read_changelog_from_dir(app_dir),
    }

    from .models import AppRegistry
    from .hot_reload import purge_app_modules
    AppRegistry.objects.filter(app_name=app_name).update(**app_info)
    cache.save_app_info(app_name, app_info)
    update_enabled_cache()
    cache.refresh_cache_from_db()
    purge_app_modules(app_name)
    _cb(f'  [{app_name}] 后处理完成')


def check_upgrade_available(app_name: str, zip_path: str) -> dict:
    """
    检查升级包是否可用。
    返回 {'can_upgrade': bool, 'current_version': str, 'new_version': str, 'message': str}
    """
    zip_file = Path(zip_path)
    if not zip_file.exists():
        return {'can_upgrade': False, 'message': f'文件不存在: {zip_path}',
                'current_version': '', 'new_version': ''}

    # 从文件名解析版本
    stem = zip_file.stem
    parts = stem.rsplit('-', 1)
    if len(parts) != 2:
        return {'can_upgrade': False, 'message': '文件名格式错误，应为 appname-version.zip',
                'current_version': '', 'new_version': ''}

    _, new_version = parts
    app_info = AppManager.get_app(app_name)
    # 核心应用(jiecore)可能不在注册表中，从磁盘读取版本
    current_version = app_info.get('version', '') if app_info else ''
    if not current_version:
        disk_meta = read_app_meta_from_disk(app_name)
        current_version = disk_meta.get('version', '')
    if not current_version:
        return {'can_upgrade': False, 'message': f'应用 {app_name} 未安装',
                'current_version': '', 'new_version': new_version}

    can = parse_version(new_version) > parse_version(current_version)

    return {
        'can_upgrade': can,
        'current_version': current_version,
        'new_version': new_version,
        'message': f'可以升级 {current_version} -> {new_version}' if can
        else f'当前版本 {current_version} 已是最新(包版本 {new_version})',
    }


def upgrade_from_zip(app_name: str, zip_path: str, progress_cb=None) -> dict:
    """
    从 zip 包升级应用。
    流程：版本比对 → 解压校验 → 依赖检测 → 备份 → 覆盖文件 → 迁移 →
          应用内置升级程序(可选) → 更新注册与缓存（失败自动回滚文件）

    progress_cb(message, progress=None, stage=None): 可选进度回调。
    """
    _cb = progress_cb or (lambda *a, **kw: None)
    _cb(f'开始升级应用: {app_name}', stage='解析升级包')
    t_all = time.time()

    zip_file = Path(zip_path)
    if not zip_file.exists():
        return {'success': False, 'message': f'文件不存在: {zip_path}'}

    stem = zip_file.stem
    parts = stem.rsplit('-', 1)
    if len(parts) != 2:
        return {'success': False, 'message': '文件名格式错误'}
    pkg_name, new_version = parts

    if pkg_name != app_name:
        return {'success': False, 'message': f'包名 {pkg_name} 与目标应用 {app_name} 不匹配'}

    check = check_upgrade_available(app_name, zip_path)
    if not check['can_upgrade']:
        return {'success': False, 'message': check['message']}
    _cb(f'{check["current_version"]} -> {new_version}', progress=5)

    tmp_dir = settings.UPGRADE_TMP_DIR / app_name

    _zip_path_obj = Path(zip_file).resolve()

    if settings.UPGRADE_TMP_DIR.exists():
        for child in settings.UPGRADE_TMP_DIR.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file() and child.resolve() != _zip_path_obj:
                    child.unlink(missing_ok=True)
            except Exception:
                pass
    settings.UPGRADE_TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True)

    try:
        _cb(f'解压升级包...', progress=10, stage='解压')
        t_unzip = time.time()
        with zipfile.ZipFile(zip_file, 'r') as zf:
            zf.extractall(tmp_dir)
        _cb(f'解压完成 (耗时 {time.time()-t_unzip:.1f}s)', progress=15)

        app_src = find_upgrade_dir(tmp_dir, app_name)
        if not app_src:
            return {'success': False, 'message': '升级包中未找到有效的应用目录'}

        new_meta = read_meta_from_path(app_src)
        if not new_meta:
            return {'success': False, 'message': '升级包缺少 app_meta.py'}
        _cb(f'version={new_meta.get("version", "?")}, display={new_meta.get("display_name", app_name)}', progress=20)

        _cb('检查应用依赖...', progress=25, stage='依赖检查')
        missing = AppManager.check_dependencies(app_name, new_meta)
        if missing:
            dep_names = ', '.join(format_dep(n, v) for n, v in missing)
            return {'success': False, 'message': f'新版本依赖未满足: {dep_names}'}
        _cb('依赖检查通过')

        _cb('校验迁移文件...', progress=30, stage='迁移校验')
        mig_error = check_migrations_bundled(app_src)
        if mig_error:
            return {'success': False, 'message': mig_error}
        _cb('迁移文件校验通过')

        _cb('检测 pip 依赖...', progress=35, stage='pip 依赖')
        from .pip_deps import ensure_pip_dependencies
        pip_result = ensure_pip_dependencies(new_meta, progress_cb=progress_cb)
        if not pip_result['success']:
            return {'success': False, 'message': pip_result['message']}

        _cb('备份当前版本...', progress=45, stage='备份')
        app_dir = get_core_app_dir(app_name)
        backup_dir = settings.UPGRADE_TMP_DIR / f'{app_name}_backup'
        original_files = set()
        if app_dir.exists():
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            t_backup = time.time()
            _ignore_upg = _make_ignore(settings.UPGRADE_TMP_DIR)
            shutil.copytree(app_dir, backup_dir, ignore=_ignore_upg)
            original_files = _collect_dir_relpaths(app_dir)
            _cb(f'备份完成 (耗时 {time.time()-t_backup:.1f}s)', progress=50)

        is_framework = is_framework_app(app_name)
        models_changed = check_models_changed(app_dir / 'models.py', app_src / 'models.py')

        try:
            _cb('覆盖文件...', progress=55, stage='覆盖文件')
            t_cp = time.time()
            shutil.copytree(app_src, app_dir, dirs_exist_ok=True)
            _cb(f'文件覆盖完成 (耗时 {time.time()-t_cp:.1f}s)', progress=65)

            _cb('执行统一后处理...', progress=65, stage='后处理')
            old_app_info = AppManager.get_app(app_name)
            old_version = old_app_info.get('version', '') if old_app_info else ''
            if not old_version:
                old_version = read_app_meta_from_disk(app_name).get('version', '0.0.0')
            _post_upgrade_app(
                app_name,
                old_version=old_version,
                new_version=new_meta.get('version', new_version),
                skip_pip=True,
                progress_cb=progress_cb,
            )

            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)

            _cb('热加载与刷新缓存...', progress=90, stage='热加载')
            from .hot_reload import rebuild_urlconf
            if not is_framework:
                from .hot_reload import purge_app_modules
                purge_app_modules(app_name)
            url_ok = rebuild_urlconf()

            from .menu_aggregator import refresh_menu_cache
            refresh_menu_cache()
            pip_installed = pip_result['installed']
            restart_required = models_changed or not url_ok or bool(pip_installed)
            restart_required = restart_required or is_framework

            message = f'应用 {app_name} 已升级至 {new_version}'
            if pip_installed:
                message += f'，已安装第三方包: {", ".join(pip_installed)}'
            if restart_required:
                message += '（需重启服务生效）'

            _cb(f'升级完成 (总耗时 {time.time()-t_all:.1f}s)', progress=100, stage='完成')
            return {
                'success': True,
                'message': message,
                'new_version': new_version,
                'restart_required': restart_required,
            }

        except Exception as e:
            _cb(f'升级失败，开始回滚...', stage='回滚')
            try:
                if backup_dir.exists():
                    _rollback_merge(app_dir, backup_dir, original_files)
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    _cb('回滚完成')
            except Exception:
                logger.exception('升级失败后恢复备份文件也失败，请手动检查 %s', app_dir)
            logger.exception('升级失败，已回滚文件')
            _cb(f'升级失败: {e}', stage='失败')
            return {'success': False, 'message': f'升级失败并已回滚: {e}'}

    except Exception as e:
        logger.exception('升级失败')
        _cb(f'升级失败: {e}', stage='失败')
    finally:
        extract_dir = tmp_dir
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        if zip_file.exists():
            zip_file.unlink(missing_ok=True)


def scan_upgrade_packages() -> list:
    """
    扫描升级目录下的所有升级包。
    返回 [{'file': str, 'app_name': str, 'version': str, 'can_upgrade': bool}, ...]
    """
    upgrade_dir = settings.UPGRADE_TMP_DIR
    if not upgrade_dir.exists():
        return []

    packages = []
    for f in upgrade_dir.iterdir():
        if f.suffix.lower() == '.zip':
            stem = f.stem
            parts = stem.rsplit('-', 1)
            if len(parts) == 2:
                app_name, version = parts
                if app_name == 'platform':
                    check = check_platform_upgrade_available(str(f))
                else:
                    check = check_upgrade_available(app_name, str(f))
                packages.append({
                    'file': str(f),
                    'filename': f.name,
                    'app_name': app_name,
                    'version': version,
                    'can_upgrade': check['can_upgrade'],
                    'current_version': check.get('current_version', ''),
                    'message': check['message'],
                })
    return packages


# ── 辅助函数 ──────────────────────────────────────────────

def find_upgrade_dir(base_dir: Path, app_name: str) -> Optional[Path]:
    """在解压目录中查找升级后的应用目录"""
    direct = base_dir / app_name
    if direct.is_dir() and (direct / 'app_meta.py').exists():
        return direct
    for item in base_dir.iterdir():
        if item.is_dir():
            candidate = item / app_name
            if candidate.is_dir() and (candidate / 'app_meta.py').exists():
                return candidate
            if (item / 'app_meta.py').exists():
                return item
    return None


def check_models_changed(old_models: Path, new_models: Path) -> bool:
    """
    对比新旧版本的 models.py 是否变更。
    Django 模型类在进程内不支持热替换，变更时必须重启服务才能使
    迁移与模型定义一致。
    """
    def read_or_empty(p: Path) -> str:
        try:
            return p.read_text(encoding='utf-8') if p.exists() else ''
        except OSError:
            return ''
    return read_or_empty(old_models).strip() != read_or_empty(new_models).strip()


def run_app_upgrade_hook(app_name: str, old_version: str, new_version: str):
    """
    执行应用自带的升级程序（可选）。
    约定：应用包根目录提供 upgrade.py，定义函数
        def upgrade(old_version: str, new_version: str) -> None
    文件覆盖与数据库迁移完成后调用，用于应用自定义的数据转换、
    配置初始化等升级逻辑。函数抛异常视为升级失败（触发回滚）。
    """
    hook_file = get_core_app_dir(app_name) / 'upgrade.py'
    if not hook_file.exists():
        return
    spec = importlib.util.spec_from_file_location(f'_upgrade_hook_{app_name}', hook_file)
    if not (spec and spec.loader):
        logger.warning('应用 %s 的 upgrade.py 无法加载，已跳过', app_name)
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hook = getattr(mod, 'upgrade', None)
    if not callable(hook):
        logger.warning('应用 %s 的 upgrade.py 未定义 upgrade 函数，已跳过', app_name)
        return
    logger.info('执行应用 %s 的内置升级程序 (%s -> %s)', app_name, old_version, new_version)
    hook(old_version, new_version)


def read_meta_from_path(app_dir: Path) -> dict:
    """从指定路径读取 app_meta"""
    meta_file = app_dir / 'app_meta.py'
    if not meta_file.exists():
        return {}
    import importlib.util
    spec = importlib.util.spec_from_file_location('_upgrade_meta', meta_file)
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            if hasattr(mod, 'meta') and isinstance(mod.meta, dict):
                return mod.meta
        except Exception:
            pass
    return {}