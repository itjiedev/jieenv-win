"""
python_installer 持久化存储

包含两部分：
- 已安装版本记录（列表模式）
- 应用级配置（字典模式，如默认安装目录 install_dir）
"""
import uuid
import logging
from datetime import datetime

from jiecore.json_storage import JsonStore
from apps.python_main.config import PYTHON_INSTALL_DIR
from .config import INSTALLED_FILE, CONFIG_FILE

logger = logging.getLogger(__name__)

_store = JsonStore(INSTALLED_FILE, default=[])

_by_id_cache = {}
_cache_dirty = True


def _invalidate_cache():
    global _cache_dirty
    _cache_dirty = True


def _ensure_cache():
    global _cache_dirty
    if _cache_dirty:
        _by_id_cache.clear()
        for r in _store.all():
            if r.get('id'):
                _by_id_cache[r['id']] = r
        _cache_dirty = False


def list_installed() -> list:
    return _store.all()


def get_installed(version: str) -> dict:
    return _store.find(version=version)


def get_installed_by_id(record_id: str) -> dict:
    _ensure_cache()
    return _by_id_cache.get(record_id)


def get_by_id(record_id: str) -> dict:
    _ensure_cache()
    return _by_id_cache.get(record_id)


def add_installed(record: dict) -> dict:
    record = dict(record)
    record.setdefault('id', uuid.uuid4().hex[:12])
    record.setdefault('locked', False)
    record.setdefault('install_dir', '')
    record.setdefault('python_exe', '')
    record.setdefault('uninstall_exe', '')
    record.setdefault('status', 'available')
    record['installed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _store.add(record)
    _invalidate_cache()
    logger.info('登记 Python Installer 安装: %s -> %s', record.get('version'), record.get('install_dir'))
    return dict(record)


def update_installed(version: str, **fields) -> dict:
    record = _store.find(version=version)
    if not record:
        return None
    result = _store.update(record['id'], **fields)
    _invalidate_cache()
    return result

def remove_installed_by_id(record_id: str) -> bool:
    _ensure_cache()
    record = _by_id_cache.get(record_id)
    if not record:
        return False
    result = _store.remove(record_id)
    if result:
        _invalidate_cache()
        logger.info('移除 Installer 记录 by id: %s', record_id)
    return result


def toggle_lock(record_id: str) -> dict | None:
    _ensure_cache()
    record = _by_id_cache.get(record_id)
    if not record:
        return None
    new_locked = not record.get('locked', False)
    result = _store.update(record_id, locked=new_locked)
    _invalidate_cache()
    return result


def remove_installed(version: str) -> bool:
    record = _store.find(version=version)
    if not record:
        return False
    result = _store.remove(record['id'])
    if result:
        _invalidate_cache()
        logger.info('移除 Python Installer 记录: %s', version)
    return result


def clear_all_installed() -> None:
    _store.write([])
    _invalidate_cache()
    logger.info('已清空全部 Python Installer 安装记录')


_DEFAULT_CONFIG = {
    'install_dir': str(PYTHON_INSTALL_DIR),
    'strict_uninstall_check': False,
}

# 应用配置存储（字典模式，原子写入）
_config_store = JsonStore(CONFIG_FILE, default={})

def load_config() -> dict:
    """读取应用配置，逐键以默认值兜底；文件不存在或损坏时返回默认值"""
    merged = dict(_DEFAULT_CONFIG)
    data = _config_store.read()
    if isinstance(data, dict):
        merged.update(data)
    return merged


def save_config(config: dict):
    """保存应用配置（原子写入 JSON 文件）"""
    _config_store.write(config)