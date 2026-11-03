"""
缓存存储处理器
数据存数据库，缓存写 .cache/ 目录，用于提升读取性能。
"""
import json
import copy
import logging
from pathlib import Path
from django.conf import settings

logger = logging.getLogger(__name__)


# ── 底层 JSON 读写 ────────────────────────────────────────

def read_json(filepath: Path, default=None):
    """读取 JSON 文件，文件不存在或解析失败时返回 default"""
    if not filepath.exists():
        return default if default is not None else {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default if default is not None else {}


def write_json(filepath: Path, data):
    """将数据写入 JSON 文件（原子写入），自动创建父目录"""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tmp = filepath.with_suffix(filepath.suffix + '.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(filepath)
    except IOError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ── 存储路径 ──────────────────────────────────────────────

def registry_path():
    return settings.APP_REGISTRY_CACHE


def enabled_path():
    return settings.ENABLED_APPS_CACHE


def menu_path():
    return settings.MENU_CACHE


# ── 应用注册表操作 ────────────────────────────────────────

def load_all_apps() -> dict:
    """加载全部应用注册数据"""
    return read_json(registry_path(), {})


def save_all_apps(data: dict):
    """保存全部应用注册数据"""
    write_json(registry_path(), data)


def load_enabled_apps() -> dict:
    """加载已启用应用数据 {app_name: {'is_root': bool, ...}}"""
    return read_json(enabled_path(), {})


def save_enabled_apps(data: dict):
    """保存已启用应用数据"""
    write_json(enabled_path(), data)


def load_menu_data() -> list:
    """加载菜单数据"""
    return read_json(menu_path(), [])


def save_menu_data(data: list):
    """保存菜单数据"""
    write_json(menu_path(), data)


# ── 单应用快捷操作 ────────────────────────────────────────

def load_app_info(app_name: str) -> dict:
    """加载单个应用的注册信息"""
    all_apps = load_all_apps()
    return all_apps.get(app_name, {})


def save_app_info(app_name: str, info: dict):
    """保存/更新单个应用的注册信息"""
    all_apps = load_all_apps()
    all_apps.setdefault(app_name, {}).update(info)
    save_all_apps(all_apps)


def remove_app_info(app_name: str):
    """从注册表中移除一个应用"""
    all_apps = load_all_apps()
    all_apps.pop(app_name, None)
    save_all_apps(all_apps)
    # 同步更新 enabled 缓存
    enabled = load_enabled_apps()
    enabled.pop(app_name, None)
    save_enabled_apps(enabled)


# ── 刷新缓存 ───────────────────────────────────────────────

def refresh_cache_from_db():
    """从数据库读取数据并刷新 JSON 缓存文件。

    数据库不可用（未迁移 / 表不存在）时直接抛出异常，不做自愈。
    调用方（jieinit / refreshcache / Web 刷新按钮）须确保数据库已就绪。
    """
    from .models import AppRegistry

    rows = list(AppRegistry.objects.all())

    registry = {}
    for r in rows:
        registry[r.app_name] = r.to_dict()
    save_all_apps(registry)

    enabled = {}
    for name, info in registry.items():
        if info.get('is_enabled'):
            entry = {'is_root': info.get('is_root', False)}
            if info.get('url_prefix'):
                entry['url_prefix'] = info['url_prefix']
            enabled[name] = entry
    save_enabled_apps(enabled)
