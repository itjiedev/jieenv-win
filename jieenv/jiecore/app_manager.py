"""
应用生命周期管理器
处理应用的安装、启用、禁用、卸载、删除以及依赖检测。
"""
import os
import re
import sys
import time
import shutil
import zipfile
import importlib
import logging

from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.db import transaction

from . import cache_handler as cache
from .app_hooks import call_hook, build_context, HookAbortError
from .app_services import clear_app_services
from .utils import parse_version

logger = logging.getLogger(__name__)


# ── 工具函数 ──────────────────────────────────────────────

# def parse_version(ver: str) -> tuple:
#     """将版本字符串解析为可比较的元组"""
#     parts = []
#     for p in ver.strip().split('.'):
#         try:
#             parts.append(int(p))
#         except ValueError:
#             parts.append(0)
#     return tuple(parts)


# 版本约束正则：可选运算符 + 版本号
_VER_SPEC_RE = re.compile(r'^\s*(>=|<=|==|>|<)?\s*([\d]+(?:\.[\d]+)*)\s*$')


def check_version_match(installed_ver: str, specifier: str) -> bool:
    """
    检查已安装版本是否满足版本约束表达式。
    支持: >=, <=, ==, >, < 以及逗号分隔的多条件组合。
    纯版本号（无运算符）视为 >= 以保持向后兼容。
    空字符串表示任意版本。

    示例:
        check_version_match('1.2.0', '>=1.0.0')   -> True
        check_version_match('1.2.0', '<2.0')       -> True
        check_version_match('1.2.0', '>1.0,<2.0')  -> True
        check_version_match('1.2.0', '==1.2.0')    -> True
        check_version_match('1.2.0', '1.0.0')      -> True  (等同 >=1.0.0)
        check_version_match('1.2.0', '')            -> True  (任意版本)
    """
    if not specifier or not specifier.strip():
        return True

    installed = parse_version(installed_ver)

    for condition in specifier.split(','):
        condition = condition.strip()
        if not condition:
            continue
        m = _VER_SPEC_RE.match(condition)
        if not m:
            logger.warning('无法解析版本约束: %s', condition)
            return False
        op, ver_str = m.group(1) or '>=', m.group(2)
        target = parse_version(ver_str)

        if op == '>=' and not (installed >= target):
            return False
        elif op == '<=' and not (installed <= target):
            return False
        elif op == '>' and not (installed > target):
            return False
        elif op == '<' and not (installed < target):
            return False
        elif op == '==' and not (installed == target):
            return False
    return True


def format_dep(dep_name: str, specifier: str) -> str:
    """格式化依赖描述字符串，用于错误消息"""
    if not specifier or not specifier.strip():
        return dep_name
    return f'{dep_name}{specifier}'


def app_dir(app_name: str) -> Path:
    """返回扩展应用在 apps/ 下的目录路径"""
    return settings.APPS_DIR / app_name


def get_core_app_dir(app_name: str) -> Path:
    """
    返回应用在磁盘上的实际目录。
    核心应用(jiecore)位于 BASE_DIR 下，扩展应用位于 apps/ 下。
    """
    if app_name == 'jiecore':
        return settings.BASE_DIR / app_name
    return settings.APPS_DIR / app_name


# 框架内核应用名（jiecore 对自身的引用，非业务耦合）
FRAMEWORK_APP = 'jiecore'




def is_framework_app(app_name: str) -> bool:
    """
    是否为框架内核应用（jiecore 自身）。
    这是**位置与生命周期**特例：目录位于 BASE_DIR 而非 apps/、模块前缀无 apps.、
    不写入注册表（版本只从磁盘 app_meta 读取）、升级时不清理模块缓存且必须重启。
    骨架保护语义统一由 is_platform_fixed_app() 承担，切勿混用：
    is_platform_fixed 覆盖 apps/ 下由平台统一打包的骨架应用（如 mainapp）。
    """
    return app_name == FRAMEWORK_APP
# is_platform_fixed 判定缓存：{app_name: (app_meta.py 的 mtime, 是否平台骨架应用)}
# is_platform_fixed=True 同时承担原 is_core（骨架保护不可卸载）+ 平台升级后处理
# 的语义
_PLATFORM_FIXED_CACHE: dict[str, tuple[float, bool]] = {}


def is_platform_fixed_app(app_name: str) -> bool:
    """
    是否为平台骨架应用（app_meta 中声明 is_platform_fixed=True）。
    语义：属于平台统一打包、由 platform-xxx.zip 升级包统一更新的骨架应用，
    用户不可单独升级、应通过平台升级入口统一更新。
    已确认的平台骨架集合：jiecore / mainapp / appmanage。
    """
    if is_framework_app(app_name):
        return True
    meta_file = get_core_app_dir(app_name) / 'app_meta.py'
    try:
        mtime = meta_file.stat().st_mtime
    except OSError:
        return False
    cached = _PLATFORM_FIXED_CACHE.get(app_name)
    if cached and cached[0] == mtime:
        return cached[1]
    result = bool(read_app_meta_from_disk(app_name).get('is_platform_fixed', False))
    _PLATFORM_FIXED_CACHE[app_name] = (mtime, result)
    return result


def get_platform_fixed_apps() -> list:
    """
    返回所有标记为平台骨架的应用名称列表。
    来源：jiecore（框架内核自身）+ APPS_DIR 下所有声明 is_platform_fixed=True 的应用。
    用于平台升级后自动执行迁移/钩子/pip/注册等全流程。
    """
    result = []
    if is_platform_fixed_app(FRAMEWORK_APP):
        result.append(FRAMEWORK_APP)
    try:
        for child in settings.APPS_DIR.iterdir():
            if child.is_dir() and not child.name.startswith('_'):
                app_name = child.name
                if is_platform_fixed_app(app_name):
                    result.append(app_name)
    except OSError:
        pass
    return result


def _read_platform_version_direct() -> str:
    """
    直接读取当前平台版本，无需 Django ORM。
    遍历骨架应用找第一个 platform_version 字段，兜底 jiecore version。
    """
    import importlib.util
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


# 应用更新日志文件约定名称（按优先级依次查找）
CHANGELOG_FILENAMES = ('CHANGELOG.md', 'CHANGELOG', 'changelog.md', 'changelog',
                       'CHANGELOG.txt', '更新日志.md', '更新日志.txt')


def read_changelog_from_dir(src_dir: Path) -> str:
    """
    从应用目录读取更新日志文件内容。
    依次查找约定名称的日志文件，找到即返回内容；都不存在时返回空字符串。
    """
    if not src_dir or not Path(src_dir).is_dir():
        return ''
    for name in CHANGELOG_FILENAMES:
        f = Path(src_dir) / name
        if f.is_file():
            try:
                return f.read_text(encoding='utf-8', errors='ignore').strip()
            except OSError as e:
                logger.warning('读取更新日志文件 %s 失败: %s', f, e)
                return ''
    return ''


def read_changelog_from_disk(app_name: str) -> str:
    """读取已安装应用目录下的更新日志文件内容"""
    if app_name == 'jiecore':
        src = settings.BASE_DIR / app_name
    else:
        src = app_dir(app_name)
    return read_changelog_from_dir(src)


def read_app_meta_from_disk(app_name: str) -> dict:
    """
    从应用目录下的 app_meta.py 读取 meta 字典。
    支持扩展应用(apps.xxx)和核心应用(jiecore)。
    """
    if app_name == 'jiecore':
        module_path = f'{app_name}.app_meta'
    else:
        module_path = f'apps.{app_name}.app_meta'
    try:
        # 强制重新加载模块以获取最新数据
        if module_path in sys.modules:
            del sys.modules[module_path]
        mod = importlib.import_module(module_path)
        if hasattr(mod, 'meta') and isinstance(mod.meta, dict):
            return mod.meta
    except (ImportError, AttributeError) as e:
        logger.warning('读取 %s 的 app_meta 失败: %s', app_name, e)
    return {}


def scan_disk_apps() -> dict:
    """扫描 apps/ 目录，返回磁盘上所有有效应用的 app_name -> meta"""
    result = {}
    apps_dir = settings.APPS_DIR
    if not apps_dir.exists():
        return result
    for item in apps_dir.iterdir():
        if item.is_dir() and (item / '__init__.py').exists() and (item / 'app_meta.py').exists():
            meta = read_app_meta_from_disk(item.name)
            if meta:
                result[item.name] = meta
    return result


# ── 核心管理器 ────────────────────────────────────────────

@contextmanager
def registry_transaction():
    """
    注册表相关纯 DML 的事务作用域（所有数据库后端均安全）。
    只能包裹纯 DML；migrate / DDL 操作必须放在事务外执行：
    - SQLite：migrate 需在事务外关闭外键检查，外层事务会直接报错；
    - MySQL：DDL 隐式提交，外层事务无法回滚 DDL，反而造成假原子性；
    - migrate 命令内部自行管理事务，无需也不应被外层事务包裹。
    """
    with transaction.atomic():
        yield


class AppManager:
    """应用生命周期管理"""

    # ── 查询 ──────────────────────────────────────────────

    @staticmethod
    def get_all_apps() -> dict:
        """获取所有已注册应用的数据"""
        from .models import AppRegistry
        return {r.app_name: r.to_dict() for r in AppRegistry.objects.all().order_by('-is_root')}

    @staticmethod
    def get_app(app_name: str) -> dict:
        """获取单个应用的注册信息"""
        from .models import AppRegistry
        try:
            return AppRegistry.objects.get(app_name=app_name).to_dict()
        except AppRegistry.DoesNotExist:
            return {}

    @staticmethod
    def get_enabled_apps() -> dict:
        """获取所有已启用应用 {name: info}"""
        from .models import AppRegistry
        return {
            r.app_name: r.to_dict()
            for r in AppRegistry.objects.filter(is_enabled=True)
        }

    @staticmethod
    def get_root_app() -> Optional[str]:
        """获取根应用名称（整个项目仅一个）"""
        all_apps = AppManager.get_all_apps()
        for name, info in all_apps.items():
            if info.get('is_root'):
                return name
        # 从磁盘扫描
        for name, meta in scan_disk_apps().items():
            if meta.get('is_root'):
                return name
        return None

    # ── 依赖检测 ──────────────────────────────────────────

    @staticmethod
    def check_dependencies(app_name: str, meta: dict = None) -> list:
        """
        检查应用的依赖是否满足。
        返回缺失的依赖列表 [(dep_name, specifier), ...]
        specifier 为原始版本约束字符串（如 '>=1.0.0'、'>1.0,<2.0'）。
        同时检测核心应用 jiecore。
        """
        if meta is None:
            meta = read_app_meta_from_disk(app_name)
        deps = meta.get('dependencies', {})
        if not deps:
            return []

        all_apps = AppManager.get_all_apps()
        missing = []
        for dep_name, specifier in deps.items():
            if dep_name == 'jiecore':
                # 核心应用：直接从磁盘读取 app_meta 检测
                core_meta = read_app_meta_from_disk(dep_name)
                if not core_meta:
                    missing.append((dep_name, specifier))
                elif not check_version_match(core_meta.get('version', '0.0.0'), specifier):
                    missing.append((dep_name, specifier))
            elif dep_name == 'jieenv_platform':
                # 平台版本：遍历骨架应用找 platform_version，兜底 jiecore version
                platform_ver = _read_platform_version_direct()
                if not platform_ver:
                    missing.append((dep_name, specifier))
                elif not check_version_match(platform_ver, specifier):
                    missing.append((dep_name, specifier))
            else:
                installed = all_apps.get(dep_name, {})
                if not installed or not installed.get('is_enabled'):
                    # 应用未安装或未启用
                    missing.append((dep_name, specifier))
                elif not check_version_match(installed.get('version', '0.0.0'), specifier):
                    # 版本不满足约束
                    missing.append((dep_name, specifier))
        return missing

    @staticmethod
    def check_all_dependencies() -> dict:
        """
        检查所有已安装应用的依赖是否满足。
        返回 {app_name: [(dep_name, specifier), ...], ...}，仅包含有缺失的应用。
        平台升级、应用批量刷新后调用，用于发现跨应用/平台版本不兼容。
        """
        all_apps = AppManager.get_all_apps()
        result = {}
        for app_name in all_apps:
            missing = AppManager.check_dependencies(app_name)
            if missing:
                result[app_name] = missing
        return result

    # ── 安装 ──────────────────────────────────────────────
    # 事务策略：注册信息写入等纯 DML 用 registry_transaction() 包裹保证原子性；
    # run_migrations / reverse_migrations 等 DDL 操作始终在事务外执行。

    @staticmethod
    def install_from_path(app_path: str, progress_cb=None) -> dict:
        """
        从 Python 模块路径安装单个应用。
        app_path: Python 风格的点分路径，如 'apps.home' 或 'myplugin'
        解析到对应的文件系统目录，将其作为应用安装到 apps/ 下。
        返回安装结果 {'success': bool, 'message': str}

        progress_cb(message, progress=None, stage=None): 可选进度回调，每一步调用一次。
        """
        _cb = progress_cb or (lambda *a, **kw: None)
        _cb(f'开始安装应用: {app_path}', stage='解析路径')
        t_all = time.time()

        src_dir = resolve_python_path(app_path)
        if not src_dir:
            return {'success': False, 'message': f'无法解析路径: {app_path}'}
        if not src_dir.exists():
            return {'success': False, 'message': f'路径不存在: {src_dir}'}
        if not src_dir.is_dir():
            return {'success': False, 'message': f'路径不是目录: {src_dir}'}

        _cb(f'源目录: {src_dir}', progress=5, stage='读取应用信息')
    
        if not (src_dir / '__init__.py').exists() or not (src_dir / 'app_meta.py').exists():
            return {'success': False, 'message': f'不是有效的应用目录（缺少 __init__.py 或 app_meta.py）: {src_dir}'}
    
        app_name = src_dir.name
        _cb(f'应用名: {app_name}')
    
        meta = read_app_meta_from_tmp(src_dir, app_name)
        if not meta:
            return {'success': False, 'message': f'无法读取 {app_path} 的 app_meta.py'}
        _cb(f'version={meta.get("version", "?")}, display={meta.get("display_name", app_name)}', progress=10)
    
        existing = AppManager.get_app(app_name)
        if existing:
            return {'success': False, 'message': f'应用 {app_name} 已安装'}
    
        _cb('检查应用依赖...', progress=15, stage='依赖检查')
        missing = AppManager.check_dependencies(app_name, meta)
        if missing:
            dep_names = ', '.join(format_dep(n, v) for n, v in missing)
            return {'success': False, 'message': f'缺少依赖: {dep_names}'}
        _cb('依赖检查通过')

        if meta.get('is_root'):
            current_root = AppManager.get_root_app()
            if current_root and current_root != app_name:
                return {'success': False, 'message': f'已存在根应用 {current_root}，一个项目只能有一个根应用'}

        _cb('校验迁移文件...', progress=20, stage='迁移校验')
        mig_error = check_migrations_bundled(src_dir)
        if mig_error:
            return {'success': False, 'message': mig_error}
        _cb('迁移文件校验通过')

        _cb('检测 pip 依赖...', progress=25, stage='pip 依赖')
        from .pip_deps import ensure_pip_dependencies
        pip_result = ensure_pip_dependencies(meta, progress_cb=progress_cb)
        if not pip_result['success']:
            return {'success': False, 'message': pip_result['message']}
    
        try:
            dest = app_dir(app_name)
            already_in_place = src_dir.resolve() == dest.resolve()

            if already_in_place:
                logger.info('应用 %s 已在 apps/ 目录下，跳过文件复制', app_name)
                _cb('应用已在目标位置，跳过文件复制', progress=40, stage='文件准备')
            else:
                _cb(f'复制文件到 {dest} ...', progress=40, stage='文件复制')
                if dest.exists():
                    shutil.rmtree(dest)
                t_copy = time.time()
                shutil.copytree(src_dir, dest)
                _cb(f'文件复制完成 (耗时 {time.time()-t_copy:.1f}s)', progress=50)
    
            _cb('注册到应用注册表...', progress=55, stage='注册应用')
            app_info = {
                'app_name': app_name,
                'display_name': meta.get('display_name', app_name),
                'version': meta.get('version', '0.0.1'),
                'is_enabled': True,
                'is_root': meta.get('is_root', False),
                'url_prefix': meta.get('url_prefix', ''),
                'dependencies': meta.get('dependencies', {}),
                'description': meta.get('description', ''),
                'changelog': read_changelog_from_dir(src_dir),
            }
    
            with registry_transaction():
                from .models import AppRegistry
                AppRegistry.objects.create(**app_info)
            cache.save_app_info(app_name, app_info)
            update_enabled_cache()
            _cb('注册表写入完成', progress=60)
    
            _cb('执行数据库迁移...', progress=60, stage='数据库迁移')
            run_migrations(app_name, progress_cb=progress_cb)
            _cb('数据库迁移完成', progress=75)

            step = '注册' if already_in_place else '安装'
            _cb('热加载应用到 Django...', progress=80, stage='热加载')
            from .hot_reload import activate_app
            pip_installed = pip_result['installed']
            restart_required = not activate_app(app_name) or bool(pip_installed)
            _cb('热加载完成', progress=85)

            _cb('刷新菜单缓存...', progress=90, stage='刷新缓存')
            from .menu_aggregator import refresh_menu_cache
            refresh_menu_cache()

            from .ext_hook_registry import load_app_ext_hooks
            load_app_ext_hooks(app_name)

            message = f'应用 {app_name} {step}成功'
            if pip_installed:
                message += f'，已安装第三方包: {", ".join(pip_installed)}'

            _cb('执行安装钩子...', progress=95, stage='钩子')
            hook_ctx = build_context(app_name, 'install', version=meta.get('version', ''))
            call_hook(app_name, 'on_install', hook_ctx)

            _cb(f'安装完成 (总耗时 {time.time()-t_all:.1f}s)', progress=100, stage='完成')

            return {'success': True, 'message': message,
                    'app_name': app_name, 'restart_required': restart_required}
    
        except Exception as e:
            logger.exception('安装应用 %s 失败', app_name)
            _cb(f'安装失败: {e}', stage='失败')
            return {'success': False, 'message': f'安装失败: {e}'}

    @staticmethod
    def install_from_zip(zip_path: str, progress_cb=None) -> dict:
        """
        从 zip 包安装应用。
        zip 命名格式: appname-version.zip
        返回安装结果 {'success': bool, 'message': str, 'app_name': str}

        progress_cb(message, progress=None, stage=None): 可选进度回调。
        """
        _cb = progress_cb or (lambda *a, **kw: None)
        _cb(f'开始从 zip 安装: {Path(zip_path).name}', stage='解析 zip')
        t_all = time.time()

        zip_file = Path(zip_path)
        if not zip_file.exists():
            return {'success': False, 'message': f'文件不存在: {zip_path}'}

        # 解析文件名获取应用名
        stem = zip_file.stem  # e.g. "myapp-1.0.0"
        parts = stem.rsplit('-', 1)
        if len(parts) != 2:
            return {'success': False, 'message': '文件名格式错误，应为 appname-version.zip'}
        app_name, zip_version = parts
        _cb(f'应用: {app_name} (包版本 {zip_version})', progress=5)

        # 检查是否已安装
        existing = AppManager.get_app(app_name)
        if existing:
            return {'success': False, 'message': f'应用 {app_name} 已安装，请使用升级功能'}

        # 解压到临时目录
        tmp_dir = settings.UPGRADE_TMP_DIR / app_name
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)

        try:
            _cb(f'解压 zip 到临时目录...', progress=10, stage='解压')
            t_unzip = time.time()
            with zipfile.ZipFile(zip_file, 'r') as zf:
                zf.extractall(tmp_dir)
            _cb(f'解压完成 (耗时 {time.time()-t_unzip:.1f}s)', progress=20)

            # 查找解压后的应用目录（可能在子目录中）
            app_src = find_app_dir(tmp_dir, app_name)
            if not app_src:
                return {'success': False, 'message': 'zip 包中未找到有效的应用目录'}

            # 读取 meta
            meta = read_app_meta_from_tmp(app_src, app_name)
            if not meta:
                return {'success': False, 'message': '应用缺少 app_meta.py 或 meta 定义'}
            _cb(f'version={meta.get("version", "?")}, display={meta.get("display_name", app_name)}', progress=25)

            _cb('检查应用依赖...', progress=30, stage='依赖检查')
            missing = AppManager.check_dependencies(app_name, meta)
            if missing:
                dep_names = ', '.join(format_dep(n, v) for n, v in missing)
                return {'success': False, 'message': f'缺少依赖: {dep_names}'}
            _cb('依赖检查通过')

            # 检查根应用唯一性
            if meta.get('is_root'):
                current_root = AppManager.get_root_app()
                if current_root and current_root != app_name:
                    return {'success': False, 'message': f'已存在根应用 {current_root}，一个项目只能有一个根应用'}

            # 校验应用自带迁移文件（安装时只执行 migrate，不做 makemigrations）
            _cb('校验迁移文件...', progress=35, stage='迁移校验')
            mig_error = check_migrations_bundled(app_src)
            if mig_error:
                return {'success': False, 'message': mig_error}
            _cb('迁移文件校验通过')

            # 检测并安装 pip 第三方包依赖（先于文件复制，失败即中止，不产生半成品应用）
            _cb('检测 pip 依赖...', progress=40, stage='pip 依赖')
            from .pip_deps import ensure_pip_dependencies
            pip_result = ensure_pip_dependencies(meta, progress_cb=progress_cb)
            if not pip_result['success']:
                return {'success': False, 'message': pip_result['message']}

            # 复制到 apps/ 目录
            _cb(f'复制文件到 apps/{app_name} ...', progress=50, stage='文件复制')
            dest = app_dir(app_name)
            if dest.exists():
                shutil.rmtree(dest)
            t_copy = time.time()
            shutil.copytree(app_src, dest)
            _cb(f'文件复制完成 (耗时 {time.time()-t_copy:.1f}s)', progress=60)

            # 注册应用（安装即启用，用户主动安装无需额外启用步骤）
            _cb('注册到应用注册表...', progress=65, stage='注册应用')
            app_info = {
                'app_name': app_name,
                'display_name': meta.get('display_name', app_name),
                'version': meta.get('version', zip_version),
                'is_enabled': True,
                'is_root': meta.get('is_root', False),
                'dependencies': meta.get('dependencies', {}),
                'description': meta.get('description', ''),
                'changelog': read_changelog_from_dir(app_src),
            }

            with registry_transaction():
                from .models import AppRegistry
                AppRegistry.objects.create(**app_info)
            cache.save_app_info(app_name, app_info)
            update_enabled_cache()
            _cb('注册表写入完成', progress=70)

            # 执行迁移
            _cb('执行数据库迁移...', progress=70, stage='数据库迁移')
            run_migrations(app_name, progress_cb=progress_cb)
            _cb('数据库迁移完成', progress=80)

            # 热加载
            _cb('热加载应用到 Django...', progress=85, stage='热加载')
            from .hot_reload import activate_app
            pip_installed = pip_result['installed']
            restart_required = not activate_app(app_name) or bool(pip_installed)
            _cb('热加载完成', progress=90)

            # 刷新菜单缓存
            _cb('刷新菜单缓存...', progress=92, stage='刷新缓存')
            from .menu_aggregator import refresh_menu_cache
            refresh_menu_cache()

            from .ext_hook_registry import load_app_ext_hooks
            load_app_ext_hooks(app_name)

            message = f'应用 {app_name} 安装成功'
            if pip_installed:
                message += f'，已安装第三方包: {", ".join(pip_installed)}'

            _cb('执行安装钩子...', progress=95, stage='钩子')
            hook_ctx = build_context(app_name, 'install', version=meta.get('version', zip_version))
            call_hook(app_name, 'on_install', hook_ctx)

            _cb(f'安装完成 (总耗时 {time.time()-t_all:.1f}s)', progress=100, stage='完成')

            return {'success': True, 'message': message,
                    'app_name': app_name, 'restart_required': restart_required}

        except Exception as e:
            logger.exception('安装应用失败')
            _cb(f'安装失败: {e}', stage='失败')
            return {'success': False, 'message': f'安装失败: {e}'}
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── 启用 / 禁用 ───────────────────────────────────────

    @staticmethod
    def enable_app(app_name: str) -> dict:
        """启用应用"""
        app_info = AppManager.get_app(app_name)
        if not app_info:
            return {'success': False, 'message': f'应用 {app_name} 未安装'}
        if app_info.get('is_enabled'):
            return {'success': False, 'message': f'应用 {app_name} 已处于启用状态'}

        # 检查依赖
        meta = read_app_meta_from_disk(app_name)
        missing = AppManager.check_dependencies(app_name, meta)
        if missing:
            dep_names = ', '.join(format_dep(n, v) for n, v in missing)
            return {'success': False, 'message': f'缺少依赖: {dep_names}'}

        # 检查根应用唯一性
        if meta.get('is_root'):
            current_root = AppManager.get_root_app()
            if current_root and current_root != app_name:
                return {'success': False, 'message': f'已存在根应用 {current_root}，一个项目只能有一个根应用'}

        # 检测并安装 pip 第三方包依赖（运行环境可能被重建，启用时兜底检测）
        from .pip_deps import ensure_pip_dependencies
        pip_result = ensure_pip_dependencies(meta)
        if not pip_result['success']:
            return {'success': False, 'message': pip_result['message']}

        with registry_transaction():
            from .models import AppRegistry
            AppRegistry.objects.filter(app_name=app_name).update(is_enabled=True)

        cache.save_app_info(app_name, {'is_enabled': True})
        update_enabled_cache()

        # 执行迁移
        run_migrations(app_name)

        # 热加载：注册到 Django 并重建路由（必须在刷新菜单之前，确保 URL 已注册）
        from .hot_reload import activate_app
        pip_installed = pip_result['installed']
        restart_required = not activate_app(app_name) or bool(pip_installed)

        # 刷新菜单缓存（路由已重建，reverse 可正确解析新应用的 URL）
        from .menu_aggregator import refresh_menu_cache
        refresh_menu_cache()

        message = f'应用 {app_name} 已启用'
        if pip_installed:
            message += f'，已安装第三方包: {", ".join(pip_installed)}'

        # 执行启用钩子
        hook_ctx = build_context(app_name, 'enable', version=meta.get('version', ''))
        call_hook(app_name, 'on_enable', hook_ctx)

        # 加载应用的扩展钩子
        from .ext_hook_registry import load_app_ext_hooks
        load_app_ext_hooks(app_name)

        # 重注册应用 service（对称 disable_app 的 clear_app_services）
        from .app_services import reload_app_services
        reload_app_services(app_name)

        return {'success': True, 'message': message,
                'restart_required': restart_required}

    @staticmethod
    def disable_app(app_name: str) -> dict:
        """禁用应用（平台骨架应用不可禁用）"""
        if is_platform_fixed_app(app_name):
            return {'success': False, 'message': f'平台骨架应用 {app_name} 不能禁用'}
        app_info = AppManager.get_app(app_name)
        if not app_info:
            return {'success': False, 'message': f'应用 {app_name} 未安装'}

        # 检查是否有其他已启用应用依赖此应用
        dependents = find_dependents(app_name)
        if dependents:
            return {'success': False, 'message': f'以下应用依赖 {app_name}: {", ".join(dependents)}，请先禁用它们'}

        with registry_transaction():
            from .models import AppRegistry
            AppRegistry.objects.filter(app_name=app_name).update(is_enabled=False)

        cache.save_app_info(app_name, {'is_enabled': False})
        update_enabled_cache()

        # 热加载：清理模块并重建路由（移除该应用路由，必须在刷新菜单之前）
        from .hot_reload import deactivate_app
        restart_required = not deactivate_app(app_name)

        # 刷新菜单缓存（路由已重建，不会再解析已禁用应用的 URL）
        from .menu_aggregator import refresh_menu_cache
        refresh_menu_cache()

        # 清除应用注册的服务
        clear_app_services(app_name)

        # 清除应用注册的扩展钩子
        from .ext_hook_registry import unregister_hooks
        unregister_hooks(app_name)

        # 清除应用注册的 provider（对称 on_enable 里的 register_provider）
        from .provider_catalog import unregister_providers_by_app
        unregister_providers_by_app(app_name)

        # 执行禁用钩子
        hook_ctx = build_context(app_name, 'disable')
        call_hook(app_name, 'on_disable', hook_ctx)

        return {'success': True, 'message': f'应用 {app_name} 已禁用',
                'restart_required': restart_required}

    # ── 卸载 / 删除 ───────────────────────────────────────

    @staticmethod
    def uninstall_app(app_name: str) -> dict:
        """卸载应用（回滚迁移、清理数据库，保留文件，需重新安装才能使用）"""
        if is_platform_fixed_app(app_name):
            return {'success': False, 'message': f'平台骨架应用 {app_name} 不能卸载'}
        app_info = AppManager.get_app(app_name)
        if not app_info:
            return {'success': False, 'message': f'应用 {app_name} 未安装'}

        dependents = find_dependents(app_name)
        if dependents:
            return {'success': False, 'message': f'以下应用依赖 {app_name}: {", ".join(dependents)}'}

        # 先禁用
        if app_info.get('is_enabled'):
            AppManager.disable_app(app_name)

        # 回滚迁移（清理数据库表和数据，必须在删除注册记录之前执行）
        reverse_migrations(app_name)

        # 删除注册记录
        with registry_transaction():
            from .models import AppRegistry
            AppRegistry.objects.filter(app_name=app_name).delete()

        cache.remove_app_info(app_name)
        update_enabled_cache()

        from .hot_reload import deactivate_app
        restart_required = not deactivate_app(app_name)

        from .menu_aggregator import refresh_menu_cache
        refresh_menu_cache()

        # 清除应用注册的扩展钩子（禁用时已清除，此处兜底）
        from .ext_hook_registry import unregister_hooks
        unregister_hooks(app_name)

        # 清除 provider→app 静态索引（禁用时不清，卸载才彻底清）
        from .provider_catalog import _cleanup_provider_app_index
        _cleanup_provider_app_index(app_name)

        # 执行卸载钩子
        hook_ctx = build_context(app_name, 'uninstall')
        call_hook(app_name, 'on_uninstall', hook_ctx)

        return {'success': True, 'message': f'应用 {app_name} 已卸载（文件保留，数据库已清理）',
                'restart_required': restart_required}

    @staticmethod
    def delete_app(app_name: str) -> dict:
        """彻底删除应用（回滚迁移、清理数据、删除文件、删除注册记录）"""
        if is_platform_fixed_app(app_name):
            return {'success': False, 'message': f'平台骨架应用 {app_name} 不能删除'}

        app_info = AppManager.get_app(app_name)
        is_registered = bool(app_info)

        if is_registered:
            if app_info.get('is_root'):
                return {'success': False, 'message': '根应用不能删除'}
            dependents = find_dependents(app_name)
            if dependents:
                return {'success': False, 'message': f'以下应用依赖 {app_name}: {", ".join(dependents)}'}
            # 先禁用
            if app_info.get('is_enabled'):
                AppManager.disable_app(app_name)
            # 回滚迁移并清理数据库数据（必须在删除文件之前执行）
            reverse_migrations(app_name)
        else:
            # 已卸载但文件仍在的情况，尝试回滚迁移
            reverse_migrations(app_name)

        # 执行删除钩子（在删除文件之前调用，钩子可能需要读取应用文件）
        hook_ctx = build_context(app_name, 'delete')
        call_hook(app_name, 'on_delete', hook_ctx)

        # 删除文件
        dest = app_dir(app_name)
        if dest.exists():
            shutil.rmtree(dest)

        # 删除注册记录（如果还存在）
        with registry_transaction():
            if is_registered:
                from .models import AppRegistry
                AppRegistry.objects.filter(app_name=app_name).delete()
        cache.remove_app_info(app_name)
        update_enabled_cache()

        from .hot_reload import deactivate_app
        restart_required = not deactivate_app(app_name)

        from .menu_aggregator import refresh_menu_cache
        refresh_menu_cache()

        # 清除应用注册的服务（禁用时已清除，此处兑底）
        clear_app_services(app_name)

        # 清除应用注册的扩展钩子（禁用时已清除，此处兜底）
        from .ext_hook_registry import unregister_hooks
        unregister_hooks(app_name)

        # 清除 provider→app 静态索引（彻底删除要彻底清）
        from .provider_catalog import _cleanup_provider_app_index
        _cleanup_provider_app_index(app_name)

        return {'success': True, 'message': f'应用 {app_name} 已彻底删除（含迁移和数据）',
                'restart_required': restart_required}

    # ── 同步磁盘状态 ──────────────────────────────────────

    @staticmethod
    def sync_from_disk():
        """
        扫描磁盘上的应用，与注册表同步。
        新发现的应用按 app_meta 声明的 is_enabled / is_platform_fixed 确定初始启用态
        （两者均未声明则视为未启用），已删除的应用从注册表移除。
        仅影响尚未入注册表的应用，已注册应用的启用态与版本不会被覆盖。
        首次部署引导依赖此行为：数据库为空时需据此把根应用拉起，否则整站 404。
        """
        disk_apps = scan_disk_apps()
        registered = AppManager.get_all_apps()

        with registry_transaction():
            # 新发现的应用
            for name, meta in disk_apps.items():
                if name not in registered:
                    info = {
                        'app_name': name,
                        'display_name': meta.get('display_name', name),
                        'version': meta.get('version', '0.0.1'),
                        'is_enabled': bool(meta.get('is_enabled', False)
                                           or meta.get('is_platform_fixed', False)),
                        'is_root': meta.get('is_root', False),
                        'url_prefix': meta.get('url_prefix', ''),
                        'dependencies': meta.get('dependencies', {}),
                        'description': meta.get('description', ''),
                        'changelog': read_changelog_from_dir(settings.APPS_DIR / name),
                    }
                    from .models import AppRegistry
                    AppRegistry.objects.get_or_create(app_name=name, defaults=info)
                    cache.save_app_info(name, info)

            # 已删除的应用
            for name in registered:
                if name not in disk_apps and name != 'jiecore':
                    from .models import AppRegistry
                    AppRegistry.objects.filter(app_name=name).delete()
                    cache.remove_app_info(name)

        update_enabled_cache()

    # ── 重扫刷新单个应用 ────────────────────────────

    @staticmethod
    def rescan_app(app_name: str, install_deps: bool = True, do_migrate: bool = True,
                   makemigrations: bool = False, dry_run: bool = False) -> dict:
        """
        重新扫描指定应用的磁盘 app_meta.py，把注册信息刷新为磁盘当前值。

        与 sync_from_disk() 的分工：后者只登记“新出现”与清理“已消失”的应用，
        已注册应用的版本/描述一律不覆盖；本方法专门处理已注册应用的信息刷新，
        适用于手工改了 app_meta.py、或直接替换了应用文件而未走升级包的场景。

        执行顺序与 upgrade_from_zip 一致：pip 依赖 → 数据库迁移 → 注册信息落库。
        pip 必须先于迁移：迁移要导入 models.py，缺第三方包会直接失败。
        迁移失败则不写注册表，避免出现“版本已刷新但库结构还是旧的”。

        is_enabled 一律不覆盖：启用态是运行期状态，只由 enable_app/disable_app
        变更，按 app_meta 声明覆盖会把管理员显式禁用的应用重新拉起。
        仅当应用在磁盘存在却尚未入注册表时，才按 app_meta 的 is_enabled/is_platform_fixed
        确定初始启用态（与 sync_from_disk 的新增语义一致）。

        参数:
            install_deps:   检测并自动安装 app_meta 声明的 pip 第三方包
            do_migrate:     执行数据库迁移（migrate）
            makemigrations: 迁移前先生成迁移文件。默认关闭，遵循“应用须自带
                            迁移文件”的约定；仅开发环境手工改模型后需要现场生成时开启
            dry_run:        只报告将要发生的变更，不安装、不迁移、不写库

        返回: {'success', 'message', 'app_name', 'version', 'creating',
               'changes': {field: (旧值, 新值)}, 'warnings': [...],
               'pip_installed': [...], 'migrated': bool, 'restart_required': bool}
        """
        result = {'success': False, 'message': '', 'app_name': app_name,
                  'version': '', 'creating': False, 'changes': {}, 'warnings': [],
                  'pip_installed': [], 'migrated': False, 'restart_required': False}
        warnings = result['warnings']

        src_dir = get_core_app_dir(app_name)
        if not (src_dir / 'app_meta.py').is_file():
            result['message'] = f'应用 {app_name} 磁盘上没有 app_meta.py：{src_dir}'
            return result

        meta = read_app_meta_from_disk(app_name)
        if not meta:
            result['message'] = f'应用 {app_name} 的 app_meta.py 读取失败或缺少 meta 字典'
            return result

        # 框架内核不入注册表（版本只从磁盘 app_meta 读取），其余按注册表现状判断新增/更新
        is_framework = is_framework_app(app_name)
        old_info = {} if is_framework else AppManager.get_app(app_name)
        creating = not is_framework and not old_info
        result['creating'] = creating

        # 从磁盘推导目标值。字段均为 AppRegistry 的真实列，可直接用于 update()
        target = {
            'display_name': meta.get('display_name') or app_name,
            'version': meta.get('version') or '0.0.1',
            'dependencies': meta.get('dependencies') or {},
            'description': meta.get('description') or '',
            'url_prefix': meta.get('url_prefix') or '',
            'changelog': read_changelog_from_dir(src_dir),
            'is_root': bool(meta.get('is_root', False)),
        }
        result['version'] = target['version']
        if creating:
            target['is_enabled'] = bool(meta.get('is_enabled', False)
                                        or meta.get('is_platform_fixed', False))

        # 按模型字段长度上限截断：SQLite 不强制 max_length，超长值只会在换成
        # MySQL 等严格模式引擎后才暴露为写入失败，故在此统一前置处理
        if not is_framework:
            try:
                from .models import AppRegistry
                for field in AppRegistry._meta.get_fields():
                    max_len = getattr(field, 'max_length', 0)
                    value = target.get(field.name)
                    if max_len and isinstance(value, str) and len(value) > max_len:
                        target[field.name] = value[:max_len]
                        warnings.append(f'{field.name} 超出模型长度上限 {max_len}，已截断')
            except Exception as e:
                warnings.append(f'字段长度校验跳过（不影响刷新）: {e}')

        # is_root 变更受“全项目只一个根应用”约束，冲突时保留原值，
        # 否则 config/urls.py 会出现两个根路由互相覆盖
        if not is_framework and not creating and target['is_root'] \
                and not old_info.get('is_root'):
            current_root = AppManager.get_root_app()
            if current_root and current_root != app_name:
                target['is_root'] = False
                warnings.append(f'磁盘声明 is_root=True，但已存在根应用 {current_root}，'
                                f'已保留原值不变更')

        # 应用间依赖只告警不阻断：重扫不改变启用态，缺依赖不影响信息刷新
        missing_apps = AppManager.check_dependencies(app_name, meta)
        if missing_apps:
            warnings.append('缺少应用依赖: '
                            + ', '.join(format_dep(n, v) for n, v in missing_apps))

        # 模型与迁移文件一致性（遵循“应用自带迁移文件”约定）
        mig_error = check_migrations_bundled(src_dir)
        if mig_error:
            if makemigrations and not dry_run:
                warnings.append('模型无迁移文件，将由 --makemigrations 现场生成')
            elif dry_run:
                warnings.append(mig_error)
            else:
                result['message'] = f'{mig_error}；如确需现场生成请加 --makemigrations'
                return result

        # 1. pip 第三方包（先于迁移，缺包会让迁移导入 models.py 失败）
        from .pip_deps import (check_pip_dependencies, ensure_pip_dependencies,
                               to_requirement)
        missing_pkgs, invalid_pkgs = check_pip_dependencies(meta)
        if invalid_pkgs:
            result['message'] = f'第三方包名不合法: {", ".join(invalid_pkgs)}'
            return result
        if missing_pkgs:
            req_text = ', '.join(to_requirement(n, s) for n, s in missing_pkgs)
            if dry_run:
                warnings.append(f'待安装第三方包: {req_text}')
            elif not install_deps:
                warnings.append(f'缺失第三方包但未安装（已指定 --no-deps）: {req_text}')
            else:
                pip_result = ensure_pip_dependencies(meta)
                if not pip_result['success']:
                    # 依赖装不上时中止：此时应用代码已无法正常运行，
                    # 再把注册信息刷成新版本只会掩盖问题
                    result['message'] = pip_result['message']
                    return result
                result['pip_installed'] = pip_result['installed']

        # 2. 数据库迁移（模型有变动时自动追齐库结构）
        if do_migrate and not dry_run:
            before = _applied_migration_count(app_name)
            if makemigrations and not _make_app_migrations(app_name):
                result['message'] = f'应用 {app_name} 生成迁移文件失败，已中止'
                return result
            if not run_migrations(app_name):
                result['message'] = (f'应用 {app_name} 数据库迁移失败，'
                                     f'注册信息未更新（详见 logs/jiecore.log）')
                return result
            after = _applied_migration_count(app_name)
            result['migrated'] = (before is not None and after is not None
                                  and after != before)

        # 3. 算出实际变更（dry_run 也要给出完整差异清单）
        if is_framework:
            changes = {}
        elif creating:
            changes = {f: (None, v) for f, v in target.items()}
        else:
            changes = {f: (old_info.get(f), v)
                       for f, v in target.items() if old_info.get(f) != v}
        result['changes'] = changes

        # 4. 注册信息落库与缓存刷新（框架内核不持久化，跳过）
        if not dry_run and not is_framework:
            with registry_transaction():
                from .models import AppRegistry
                if creating:
                    AppRegistry.objects.get_or_create(
                        app_name=app_name,
                        defaults=dict(target, app_name=app_name))
                else:
                    AppRegistry.objects.filter(app_name=app_name).update(**target)
            cache.save_app_info(app_name, target)
            update_enabled_cache()
            cache.refresh_cache_from_db()
            # app_meta 中的 menus 声明可能一并变了，菜单缓存需同步重建
            try:
                from .menu_aggregator import refresh_menu_cache
                refresh_menu_cache()
            except Exception as e:
                warnings.append(f'菜单缓存刷新失败（可单独执行 refreshcache）: {e}')

        result['restart_required'] = bool(
            result['pip_installed'] or result['migrated']
            or any(f in changes for f in ('version', 'url_prefix', 'is_root'))
        )

        if is_framework:
            # 框架内核不入注册表，预演与实跑都只做只读校验，故先于 dry_run 分支
            prefix = '[预演] ' if dry_run else ''
            result['message'] = (f'{prefix}框架内核 {app_name} 已重新扫描'
                                 f'（不入注册表，版本以磁盘 app_meta 为准）')
        elif dry_run:
            if creating:
                detail = f'尚未入注册表，将按 app_meta 新登记（{len(changes)} 个字段）'
            elif changes:
                detail = f'{len(changes)} 个字段有变更'
            else:
                detail = '信息与磁盘一致，无变更'
            result['message'] = f'[预演] 应用 {app_name} {detail}'
        elif creating:
            result['message'] = f'应用 {app_name} 磁盘存在但未注册，已按 app_meta 登记'
        elif changes:
            result['message'] = f'应用 {app_name} 信息已更新（{len(changes)} 个字段）'
        else:
            result['message'] = f'应用 {app_name} 信息与磁盘一致，无需更新'

        if result['pip_installed']:
            result['message'] += f'；已安装第三方包: {", ".join(result["pip_installed"])}'
        if result['migrated']:
            result['message'] += '；已应用数据库迁移'
        if result['restart_required']:
            result['message'] += '（需重启服务生效）'

        result['success'] = True
        return result


# ── 辅助函数 ──────────────────────────────────────────────

def find_app_dir(base_dir: Path, app_name: str) -> Optional[Path]:
    """在解压目录中查找应用目录"""
    # 直接在 base_dir 下
    direct = base_dir / app_name
    if direct.is_dir() and (direct / 'app_meta.py').exists():
        return direct
    # 可能在一级子目录中
    for item in base_dir.iterdir():
        if item.is_dir():
            candidate = item / app_name
            if candidate.is_dir() and (candidate / 'app_meta.py').exists():
                return candidate
            if (item / 'app_meta.py').exists():
                return item
    return None


def resolve_python_path(python_path: str) -> Optional[Path]:
    """
    将 Python 风格的点分路径解析为文件系统目录。
    优先使用 importlib 查找已注册模块，否则从 BASE_DIR 推导。
    示例:
        'apps.home'      -> /project/apps/home/
        'myplugin'       -> /project/myplugin/
    """
    # 优先通过 importlib 解析（已加载或 sys.path 可找到的模块）
    try:
        if python_path in sys.modules:
            mod = sys.modules[python_path]
            if hasattr(mod, '__path__'):
                return Path(mod.__path__[0])
        spec = importlib.util.find_spec(python_path)
        if spec and spec.submodule_search_locations:
            return Path(spec.submodule_search_locations[0])
    except (ModuleNotFoundError, ValueError):
        pass

    # 回退：从 BASE_DIR 推导文件系统路径
    parts = python_path.split('.')
    candidate = settings.BASE_DIR / '/'.join(parts)
    if candidate.is_dir():
        return candidate
    return None


def read_app_meta_from_tmp(app_dir: Path, app_name: str) -> dict:
    """从临时解压目录读取 app_meta"""
    meta_file = app_dir / 'app_meta.py'
    if not meta_file.exists():
        return {}
    import importlib.util
    spec = importlib.util.spec_from_file_location(f'_tmp_{app_name}_meta', meta_file)
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            if hasattr(mod, 'meta') and isinstance(mod.meta, dict):
                return mod.meta
        except Exception:
            pass
    return {}


def update_enabled_cache():
    """刷新已启用应用的缓存文件"""
    all_apps = cache.load_all_apps()
    enabled = {}
    for name, info in all_apps.items():
        if info.get('is_enabled'):
            entry = {'is_root': info.get('is_root', False)}
            # url_prefix 优先从注册表读取，缺失则从磁盘 app_meta 补充
            url_prefix = info.get('url_prefix', '')
            if not url_prefix:
                meta = read_app_meta_from_disk(name)
                url_prefix = meta.get('url_prefix', '') if meta else ''
            if url_prefix:
                entry['url_prefix'] = url_prefix
            enabled[name] = entry
    cache.save_enabled_apps(enabled)


def find_dependents(target_app: str) -> list:
    """
    查找哪些已启用的应用依赖 target_app。
    """
    result = []
    all_apps = AppManager.get_all_apps()
    for name, info in all_apps.items():
        if name == target_app or not info.get('is_enabled'):
            continue
        deps = info.get('dependencies', {})
        if target_app in deps:
            result.append(name)
    # 如果目标是核心应用，还需检查是否有应用通过磁盘 meta 声明了对它的依赖
    # （这部分已在上方循环中通过 all_apps 覆盖，因为扩展应用的 dependencies
    #  已包含在注册表数据中）
    return result


def ensure_app_registered(app_name: str):
    """
    确保应用已注册到 Django 的 app registry 中。
    如果尚未注册，则动态查找并注册其 AppConfig。
    返回是否成功注册。
    """
    from django.apps import apps as django_apps
    try:
        django_apps.get_app_config(app_name)
        return True  # 已注册
    except LookupError:
        pass

    # 动态导入 apps 模块，查找 AppConfig 子类
    module_path = f'apps.{app_name}.apps'
    try:
        mod = importlib.import_module(module_path)
    except ImportError:
        logger.debug('应用 %s 无 apps.py，无法动态注册', app_name)
        return False

    from django.apps import AppConfig
    # 导入应用的 models 模块（AppConfig 需要 app_module 参数）
    app_module_path = f'apps.{app_name}'
    try:
        app_module = importlib.import_module(app_module_path)
    except ImportError:
        logger.debug('无法导入应用模块 %s', app_module_path)
        return False

    for attr_name in dir(mod):
        attr = getattr(mod, attr_name)
        if (isinstance(attr, type) and issubclass(attr, AppConfig)
                and attr is not AppConfig):
            try:
                # Django 5.x: AppConfig.__init__(app_name, app_module)
                config = attr(app_module_path, app_module)
                # 必须手动设置 apps 引用，否则迁移命令会报错
                config.apps = django_apps
                django_apps.app_configs[app_name] = config
                # 导入 models，让 Django 知道应用的模型定义
                config.import_models()
                logger.info('动态注册应用 %s 到 Django app registry', app_name)
                return True
            except Exception as e:
                logger.warning('动态注册应用 %s 失败: %s', app_name, e)
                return False
    return False


def check_migrations_bundled(src_dir: Path) -> Optional[str]:
    """
    校验应用是否自带迁移文件。
    应用定义了模型时，必须随包携带 migrations 迁移文件；
    安装/启用等操作只执行 migrate，不做 makemigrations。
    返回错误消息；校验通过返回 None。
    """
    models_file = src_dir / 'models.py'
    has_models = False
    if models_file.exists():
        try:
            content = models_file.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            content = ''
        has_models = re.search(r'class\s+\w+\s*\([^)]*models\.Model', content) is not None
    if not has_models:
        return None
    mig_dir = src_dir / 'migrations'
    if mig_dir.is_dir():
        for f in mig_dir.iterdir():
            if f.is_file() and f.suffix == '.py' and f.name != '__init__.py':
                return None
    return ('应用定义了模型但未携带迁移文件，请先在开发环境执行 makemigrations '
            '生成迁移文件，并将 migrations 目录随应用一起打包')


def run_migrations(app_name: str, progress_cb=None) -> bool:
    """
    为指定应用执行数据库迁移（仅 migrate，不做 makemigrations）。
    约定：应用必须自带 migrations 迁移文件。
    若迁移因数据表已存在而失败（如历史遗留的孤儿表），
    且全部模型表均已存在，则自动 fake 迁移记录以恢复一致。
    返回是否成功：无法注册到 Django 或迁移执行失败返回 False。
    既有调用方均忽略返回值，此改动向后兼容。
    """
    try:
        from django.core.management import call_command
        from django.apps import apps as django_apps
        if not ensure_app_registered(app_name):
            if progress_cb:
                progress_cb(f'应用 {app_name} 无法注册到 Django，跳过迁移', stage='迁移跳过')
            logger.debug('应用 %s 无法注册到 Django，跳过迁移', app_name)
            return False
        app_config = django_apps.get_app_config(app_name)
        app_label = app_config.label
        if progress_cb:
            progress_cb(f'开始数据库迁移: {app_name} (label={app_label})', stage='数据库迁移')
        t0 = time.time()
        try:
            call_command('migrate', app_label, verbosity=0)
        except Exception as e:
            if 'already exists' in str(e) and _all_model_tables_exist(app_config):
                call_command('migrate', app_label, fake=True, verbosity=0)
                logger.info('应用 %s 数据表已存在，已 fake 迁移记录', app_name)
                if progress_cb:
                    progress_cb(f'数据表已存在，fake 迁移记录 (耗时 {time.time()-t0:.1f}s)', stage='迁移完成')
            else:
                raise
        else:
            if progress_cb:
                progress_cb(f'数据库迁移完成 (耗时 {time.time()-t0:.1f}s)', stage='迁移完成')
    except Exception as e:
        logger.warning('应用 %s 迁移失败: %s', app_name, e)
        if progress_cb:
            progress_cb(f'应用 {app_name} 迁移失败: {e}', stage='迁移失败')
        return False
    return True


def _applied_migration_count(app_name: str):
    """统计应用已应用的迁移条数，用于判断本次 migrate 是否真的改动了库结构。
    无法确定时（应用未注册、django_migrations 表尚未建立）
    返回 None，调用方据此跳过对比。使用占位符绑定，与数据库引擎无关。"""
    try:
        from django.db import connection
        from django.apps import apps as django_apps
        try:
            label = django_apps.get_app_config(app_name).label
        except LookupError:
            return None
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT COUNT(*) FROM django_migrations WHERE app = %s', [label])
            row = cursor.fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def _make_app_migrations(app_name: str) -> bool:
    """为应用现场生成迁移文件。
    项目约定“应用须自带迁移文件”，安装/启用/升级流程一律不调用本函数；
    仅供开发环境下手工改了模型后由 rescan_app(--makemigrations) 显式触发。"""
    try:
        from django.core.management import call_command
        from django.apps import apps as django_apps
        if not ensure_app_registered(app_name):
            logger.warning('应用 %s 无法注册到 Django，跳过 makemigrations', app_name)
            return False
        call_command('makemigrations', django_apps.get_app_config(app_name).label,
                     verbosity=0, interactive=False)
        return True
    except Exception as e:
        logger.warning('应用 %s 生成迁移文件失败: %s', app_name, e)
        return False


def _all_model_tables_exist(app_config) -> bool:
    """检查应用全部模型的数据库表是否均已存在"""
    from django.db import connection
    models = list(app_config.get_models(include_auto_created=True))
    if not models:
        return False
    with connection.cursor() as cursor:
        existing = set(connection.introspection.table_names(cursor))
    return all(m._meta.db_table in existing for m in models)


def drop_leftover_tables(app_config):
    """
    强制删除应用模型对应的残留数据表。
    作为 migrate zero 回滚失败的兜底：卸载/删除应用时确保表不残留。
    与数据库引擎无关：标识符引用与外键检查开关均按 vendor 分支处理。
    """
    from django.db import connection
    try:
        with connection.cursor() as cursor:
            existing = set(connection.introspection.table_names(cursor))
        tables = [
            m._meta.db_table
            for m in app_config.get_models(include_auto_created=True)
            if m._meta.db_table in existing
        ]
        if not tables:
            return
        vendor = connection.vendor
        with connection.cursor() as cursor:
            # 先关外键检查再删表，否则被引用的表先删会失败。各引擎开关语法不同，
            # 没有会话级开关的引擎（如 PostgreSQL）依赖下方逐表容错：本函数不在
            # transaction.atomic() 内，Django 默认 AUTOCOMMIT，单条 DROP 失败不会
            # 污染后续语句
            if vendor == 'sqlite':
                cursor.execute('PRAGMA foreign_keys = OFF')
            elif vendor == 'mysql':
                cursor.execute('SET FOREIGN_KEY_CHECKS = 0')
            try:
                for table in tables:
                    # 标识符引用符各引擎不同（sqlite/PostgreSQL 用双引号、MySQL 用
                    # 反引号），必须由 Django 的 quote_name 生成，不能写死双引号
                    quoted = connection.ops.quote_name(table)
                    try:
                        cursor.execute(f'DROP TABLE IF EXISTS {quoted}')
                    except Exception as e:
                        # 单表失败（如存在外部依赖）不阻断其余表的清理
                        logger.warning('删除残留数据表 %s 失败: %s', table, e)
            finally:
                if vendor == 'sqlite':
                    cursor.execute('PRAGMA foreign_keys = ON')
                elif vendor == 'mysql':
                    cursor.execute('SET FOREIGN_KEY_CHECKS = 1')
        logger.info('已删除应用 %s 的残留数据表: %s', app_config.label, ', '.join(tables))
    except Exception as e:
        logger.warning('删除应用 %s 残留数据表失败: %s', app_config.label, e)


def reverse_migrations(app_name: str):
    """
    回滚应用的所有数据库迁移，并清理相关数据。
    必须在删除应用文件之前调用（回滚需要迁移文件存在）。
    清理内容：
    - 回滚所有迁移（删除应用创建的数据表）
    - 兜底删除回滚后仍残留的模型数据表
    - 清理 django_migrations 记录
    - 清理 django_content_type 记录
    - 清理 auth_permission 记录
    """
    try:
        from django.core.management import call_command
        from django.apps import apps as django_apps
        from django.db import connection

        if not ensure_app_registered(app_name):
            logger.debug('应用 %s 无法注册到 Django，跳过迁移回滚', app_name)
            return

        app_config = django_apps.get_app_config(app_name)
        app_label = app_config.label

        # 1. 回滚所有迁移（删除应用创建的表）
        try:
            call_command('migrate', app_label, 'zero', verbosity=0)
            logger.info('已回滚应用 %s 的所有迁移', app_name)
        except Exception as e:
            logger.warning('应用 %s 迁移回滚失败，尝试强制删除数据表: %s', app_name, e)

        # 2. 兜底：删除回滚后仍残留的数据表（如历史孤儿表）
        drop_leftover_tables(app_config)

        # 3. 清理 django_migrations 记录与 auth_permission/content_type
        #    （纯 DML，可安全包裹事务；此处不在 migrate 的 DDL 作用域内）
        try:
            from django.conf import settings as _settings
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        'DELETE FROM django_migrations WHERE app = %s', [app_label]
                    )
                from django.contrib.contenttypes.models import ContentType
                ct_ids = list(
                    ContentType.objects.filter(app_label=app_label)
                    .values_list('pk', flat=True)
                )
                if getattr(_settings, 'USING_AUTH', False) and ct_ids:
                    from django.contrib.auth.models import Permission
                    Permission.objects.filter(content_type_id__in=ct_ids).delete()
                ContentType.objects.filter(app_label=app_label).delete()
            logger.info('已清理应用 %s 的迁移记录、content_type 和 permission', app_name)
        except Exception as e:
            logger.warning('清理应用 %s 的迁移记录/content_type/permission 失败(可忽略): %s', app_name, e)

    except Exception as e:
        logger.warning('应用 %s 迁移回滚处理失败: %s', app_name, e)


def is_runserver_mode() -> bool:
    """判断当前服务是否以 runserver 方式运行（开发模式）"""
    return 'runserver' in sys.argv


def restart_server():
    """
    重启服务进程。

    重启语义按入口而异，统一由 server_process 的策略注册收敛：
    - 壳-子进程拆分模式（JIEENV_SUBPROCESS_MODE）：写重启请求文件后退出，
      壳进程检测到子进程退出 + 重启文件 → 拉新子进程 → 窗口 reload；
    - 入口（如桌面壳）注册了自定义实现时直接委托给它；
    - runserver 模式无法自动拉起新进程，终止当前进程由用户手动重启；
    - Windows 默认模式（纯 Web 入口）：优雅关闭 WSGI 服务器
      （停线程→关 socket→释放端口）→ 启动新进程 → 退出当前进程；
    - Linux：先关 WSGI 服务器，再 os.execv 直接替换进程。
    新进程启动时也会在其 ready() 中重新登记 PID，双重保障。

    返回 dict: {success: bool, manual_restart_required: bool, message: str}
    """
    from .server_process import (
        get_restart_handler, close_wsgi_server, is_subprocess_mode,
        write_restart_request,
        remove_stop_request, _close_db_for_shutdown,
    )

    # 入口注册了自定义重启策略（如桌面壳的就地重启）则直接委托。
    # 在函数体内查而不是靠替换模块属性：后者需同时补丁 views 与 app_manager
    # 两处（views 用 from ... import 已绑定名字），新增导入点会静默失效
    handler = get_restart_handler()
    if handler is not None:
        return handler()

    # runserver 模式下无法自动拉起新进程，直接终止当前进程
    if is_runserver_mode():
        logger.warning('当前服务以 runserver 方式运行，即将自动终止，需用户手动重启')
        import threading
        def _terminate():
            _close_db_for_shutdown()
            logging.shutdown()      # flush 日志，os._exit 会跳过 atexit 与缓冲刷新
            os._exit(0)
        threading.Timer(1.0, _terminate).start()
        return {
            'success': False,
            'manual_restart_required': True,
            'message': '当前服务以 runserver 方式运行，服务将自动停止，请手动重新启动',
        }

    # 重启互斥：防止并发重启拉起多个进程抢端口（#11）
    from .server_process import _restart_lock
    if not _restart_lock.acquire(blocking=False):
        logger.warning('重启已在进行中，忽略重复请求')
        return {'success': False, 'message': '重启已在进行中，请稍后'}

    try:
        # 壳-子进程拆分模式：写重启文件后退出，由壳进程负责拉新子进程（#1）
        if is_subprocess_mode():
            logger.info('壳-子进程模式，写重启请求文件后退出，等待壳拉起新进程')
            if not write_restart_request():
                _restart_lock.release()
                return {'success': False, 'manual_restart_required': True,
                        'message': '写入重启请求文件失败，无法重启'}
            import threading
            def _exit_for_shell_restart():
                _close_db_for_shutdown()
                logging.shutdown()
                os._exit(0)
            threading.Timer(0.5, _exit_for_shell_restart).start()
            return {'success': True, 'message': '服务正在重启（壳模式）'}

        logger.info('服务即将重启...')
        # 清理残留的停止请求文件，避免新进程刚起来就自杀（#9）
        remove_stop_request()

        # Windows venv 下 sys.executable 是启动器桩进程，分离重启后会随旧进程退出
        # 被连带终止；优先使用真实解释器（sys._base_executable）避免此问题
        executable = getattr(sys, '_base_executable', None) or sys.executable
        # 确保脚本路径为绝对路径，避免 DETACHED_PROCESS 下工作目录不同导致找不到文件（#13）
        if sys.argv and not os.path.isabs(sys.argv[0]):
            argv0 = os.path.abspath(sys.argv[0])
        else:
            argv0 = sys.argv[0]
        args = [executable, argv0] + sys.argv[1:]

        # 准备新进程环境变量：清除重启标记与子进程模式标记（#14）
        new_env = {k: v for k, v in os.environ.items()
                   if k not in ('JIEENV_RESTARTING', 'JIEENV_SUBPROCESS_MODE')}
        new_env['JIEENV_RESTARTING'] = '1'

        if sys.platform == 'win32':
            import subprocess
            import threading

            def _shutdown_and_restart():
                """后台线程：优雅关闭 WSGI 服务器 → 启动新进程 → 退出当前进程"""
                # 关闭逻辑与优雅停止共用（停 handler 线程 → 关 socket → 释放端口），
                # 服务器对象由入口通过 register_wsgi_server 登记
                if not close_wsgi_server():
                    logger.warning('WSGI 服务器关闭失败（未注册 handler），继续重启但端口可能未释放')

                import time
                time.sleep(0.5)  # 等待 OS 完全回收端口

                # 启动新进程（#8: Popen 失败时不静默死亡）
                try:
                    devnull = subprocess.DEVNULL
                    proc = subprocess.Popen(
                        args,
                        stdin=devnull, stdout=devnull, stderr=devnull,
                        env=new_env,
                        creationflags=0x00000008 | 0x08000000,  # DETACHED_PROCESS | CREATE_NO_WINDOW
                    )
                    logger.info('新服务进程已启动 PID=%d，PID 文件将由新进程自行登记', proc.pid)
                    # 不在此处 write_pid_file：新进程会在 apps.ready() 中自行登记，
                    # 避免旧进程写入指向未 ready 进程的 PID（#15）
                except Exception as e:
                    logger.error('启动新服务进程失败: %s', e)
                    return  # 旧服务已关、新服务没起，不 os._exit，保留现场供诊断

                # 退出当前进程（#12: 先关 DB 连接）
                _close_db_for_shutdown()
                logging.shutdown()
                os._exit(0)

            # 必须在后台线程执行关闭，因为当前线程本身就是 WSGI 服务器 handler 线程，
            # 无法 join 自己；后台线程负责关闭其他 handler 线程并退出进程
            t = threading.Thread(target=_shutdown_and_restart, daemon=True)
            t.start()
            return {'success': True, 'manual_restart_required': False,
                    'message': '服务正在重启'}
        else:
            # Linux：先关 WSGI 服务器再 execv，与 Windows 路径对称（#10）
            close_wsgi_server()
            _close_db_for_shutdown()
            os.execv(executable, args)
            # os.execv 成功时不会执行到这里；失败时返回错误
            return {'success': False, 'manual_restart_required': False,
                    'message': '服务重启失败（os.execv 执行失败）'}
    finally:
        _restart_lock.release()