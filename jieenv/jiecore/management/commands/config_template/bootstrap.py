"""
启动期引导逻辑

从缓存文件加载应用注册表，构建 INSTALLED_APPS。
本模块在 settings 加载期被 base.py 导入执行，Django 尚未 setup，故所有操作
均不依赖 ORM，只使用标准库与 constants 中定义的纯常量。

缓存加载策略：
  1. 缓存文件存在 → 正常读取
  2. 缓存缺失 → 报错退出（提示运行 refreshcache 重建缓存）

恢复命令（refreshcache、jieinit）跳过检查，避免死锁。
"""
import json
import sys
from pathlib import Path

from .constants import (
    APP_REGISTRY_CACHE,
    USING_AUTH,
)

def _read_registry(filepath) -> dict:
    """读取注册表 JSON；文件不存在或解析失败返回 None"""
    filepath = Path(filepath)
    if not filepath.is_file():
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _build_installed_apps(registry: dict) -> list:
    """根据注册表构建 INSTALLED_APPS 列表"""
    installed = [
        'django.contrib.contenttypes',
        'django.contrib.sessions',
        'django.contrib.messages',
        'django.contrib.staticfiles',
    ]
    if USING_AUTH:
        installed.extend([
            'django.contrib.admin',
            'django.contrib.auth',
        ])
    installed.append('jiecore')
    for app_name, info in registry.items():
        if info.get('is_enabled'):
            module_path = f'apps.{app_name}'
            if module_path not in installed:
                installed.append(module_path)
    return installed


# 跳过缓存检查的管理命令：这些命令本身可用于恢复缓存或初始化数据库，
# 必须允许在缓存缺失时正常启动 Django（此时 INSTALLED_APPS 为空，
# migrate 会先建 jiecore 表，之后 refreshcache --sync 再注册新 app）
_RECOVERY_COMMANDS = {'refreshcache', 'jieinit', 'migrate', 'checkdeps', 'apprescan'}


# ── 模块加载时执行 ─────────────────────────────────────────

_REGISTRY = _read_registry(APP_REGISTRY_CACHE)

if _REGISTRY is None:
    _is_recovery = (
        len(sys.argv) >= 2
        and sys.argv[1] in _RECOVERY_COMMANDS
    )
    if not _is_recovery:
        _line = '═' * 20
        print(
            f'{_line}\n'
            '  错误：应用注册表缓存文件缺失！\n'
            '  请执行以下命令重建缓存：\n'
            '    python manage.py refreshcache\n'
            f'{_line}\n',
            file=sys.stderr,
        )
        sys.exit(1)
    _REGISTRY = {}

INSTALLED_APPS = _build_installed_apps(_REGISTRY)