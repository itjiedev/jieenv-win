"""
纯常量配置

只定义项目路径、缓存文件路径、数据库配置、Django 基础设置等纯常量。
不包含任何启动期逻辑（注册表加载、兜底扫描等），那些逻辑在 bootstrap.py 中。

模块导入顺序（base.py）：
  security.py → constants.py → bootstrap.py → extra.py → logging.py
"""
import sys
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

# ── 项目目录 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_ROOT = BASE_DIR.parent  # 环境根目录（供子模块引用根级路径）
CACHE_DIR = BASE_DIR / '.cache'
DATA_DIR = BASE_DIR / 'data'
APPS_DIR = BASE_DIR / 'apps'
LOG_DIR = BASE_DIR / 'logs'
TMP_DIR = BASE_DIR / 'tmp'
UPGRADE_TMP_DIR = TMP_DIR / 'upgrade'

# 启动时自动创建必要目录
for _d in [CACHE_DIR, DATA_DIR, APPS_DIR, LOG_DIR, TMP_DIR, UPGRADE_TMP_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# 确保项目根目录在模块搜索路径最前：扩展应用以 apps.<应用名> 形式导入，
# 不依赖启动时的工作目录/脚本位置（兼容 PyCharm 运行配置、后台重启进程等场景）
_BASE_DIR_STR = str(BASE_DIR)
if _BASE_DIR_STR in sys.path:
    sys.path.remove(_BASE_DIR_STR)
sys.path.insert(0, _BASE_DIR_STR)

# ── 平台版本 ──────────────────────────────────────────────
# 平台版本号从所有 is_platform_fixed 骨架应用中查找（纯磁盘扫描，
# 不依赖 Django settings 或 jiecore 模块，避免循环导入），
# 找第一个声明了 platform_version 字段的应用；全没有则回退 jiecore version。
import importlib.util as _ilu
_platform_version = '0.0.0'

def _scan_platform_version():
    """扫描所有骨架应用的 app_meta.py，找 platform_version，纯磁盘遍历。"""
    candidates = []

    # 1. 先扫 APPS_DIR 下的各应用目录
    if APPS_DIR.exists():
        for _child in APPS_DIR.iterdir():
            if _child.is_dir():
                candidates.append(_child / 'app_meta.py')

    # 2. 再扫 BASE_DIR 下的 jiecore（它在 BASE_DIR 不在 APPS_DIR）
    candidates.append(BASE_DIR / 'jiecore' / 'app_meta.py')

    _fallback_jiecore_version = ''
    for _meta_path in candidates:
        if not _meta_path.exists():
            continue
        try:
            _spec = _ilu.spec_from_file_location('_pv_meta', _meta_path)
            if not _spec or not _spec.loader:
                continue
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _meta = getattr(_mod, 'meta', {})
        except Exception:
            continue

        # 骨架应用才参与搜索
        if _meta.get('is_platform_fixed'):
            _pv = _meta.get('platform_version', '')
            if _pv:
                return _pv

        # 顺便抓取 jiecore 的 version 作最终兜底
        if _meta_path.parent.name == 'jiecore':
            _fallback_jiecore_version = _meta.get('version', '')

    return _fallback_jiecore_version or '0.0.0'

_platform_version = _scan_platform_version()
PLATFORM_VERSION = _platform_version

# ── 缓存文件路径 ──────────────────────────────────────────
APP_REGISTRY_CACHE = CACHE_DIR / 'all_apps.json'
ENABLED_APPS_CACHE = CACHE_DIR / 'enabled_apps.json'
MENU_CACHE = CACHE_DIR / 'menus.json'

# ── 数据库配置 ────────────────────────────────────────────
# 数据库为必填配置，项目依赖数据库存储应用注册表、菜单、权限等数据。
# 如需更换数据库引擎，修改下方 DATABASES 配置即可。

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': DATA_DIR / 'jieenvmain.db',
    }
}

# 用 globals().get 兜底，避免 DATABASES 被意外清空时抛 NameError
DATABASES = globals().get('DATABASES') or {}
if not DATABASES.get('default', {}).get('ENGINE'):
    raise ImproperlyConfigured(
        'DATABASES.default.ENGINE 未配置，数据库为必填项。'
        '请在 config/constants.py 中配置 DATABASES。'
    )

# ── 功能开关 ──────────────────────────────────────────────
# 是否启用用户权限系统，设为 True 将启动 admin/auth 相关配置组件，如有需要请仔细检查
USING_AUTH = False

# ── 静态文件 / 国际化 / 杂项 ─────────────────────────────
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'static_collect'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = False

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'