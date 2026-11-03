"""
python_archive 持久化存储

包含两部分：
- 已安装版本记录（列表模式）
- 应用级配置（字典模式，如默认安装目录 install_dir）
"""
import uuid
import logging
from datetime import datetime

from jiecore.json_storage import JsonStore
from . import python_versions
from .config import INSTALLED_FILE, CONFIG_FILE, _DEFAULT_CONFIG, VERSION_CACHE_FILE

logger = logging.getLogger(__name__)

# ------------ 已安装数据文件 -----------------
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
    result = _store.all()
    for rec in result:
        rec.setdefault('locked', False)
    return result


def get_installed(name: str) -> dict:
    """按名称查找安装记录，不存在返回 None"""
    return _store.find(name=name)


def get_by_id(record_id: str) -> dict:
    """按记录主键 id 查找安装记录，不存在返回 None"""
    _ensure_cache()
    return _by_id_cache.get(record_id)


def name_exists(name: str) -> bool:
    """检查名称是否已存在"""
    return _store.find(name=name) is not None


def generate_unique_name(base_name: str) -> str:
    """生成唯一名称，如果已存在同名则在后面加数字"""
    if not name_exists(base_name):
        return base_name
    # 查找已存在的同名记录，找到最大数字后缀
    counter = 1
    while name_exists(f'{base_name}-{counter}'):
        counter += 1
    return f'{base_name}-{counter}'


def add_installed(record: dict) -> dict:
    """登记一条安装记录"""
    record = dict(record)
    record.setdefault('id', uuid.uuid4().hex[:12])
    record.setdefault('name', '')
    record.setdefault('install_dir', '')
    record.setdefault('python_exe', '')
    record.setdefault('status', 'available')
    record.setdefault('locked', False)
    record['installed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _store.add(record)
    _invalidate_cache()
    logger.info('登记 Python Archive 安装: %s (%s) -> %s',
                record.get('name'), record.get('version'), record.get('install_dir'))
    return dict(record)


def remove_installed(record_id: str) -> bool:
    """按 ID 移除安装记录"""
    record = _store.get(record_id)
    if not record:
        return False
    result = _store.remove(record_id)
    if result:
        _invalidate_cache()
        logger.info('移除 Python Archive 记录: %s (%s)', record_id, record.get('name'))
    return result


def rename_installed(record_id: str, new_name: str) -> dict | None:
    """按 ID 修改安装记录名称，返回更新后的 dict；名称已存在时返回 None"""
    if name_exists(new_name):
        return None
    result = _store.update(record_id, name=new_name)
    _invalidate_cache()
    return result


def toggle_lock(record_id: str) -> dict | None:
    """切换安装记录的锁定状态，返回更新后的 dict"""
    record = _store.get(record_id)
    if not record:
        return None
    new_locked = not record.get('locked', False)
    result = _store.update(record_id, locked=new_locked)
    _invalidate_cache()
    return result


def clear_all_installed() -> None:
    """清空全部已安装记录"""
    _store.write([])
    _invalidate_cache()
    logger.info('已清空全部 Python Archive 安装记录')


# --------- 应用配置文件 -----------------------
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


# --------- python 版本的列表文件 ------------------
_version_store = JsonStore(VERSION_CACHE_FILE, default={})


def get_version_by_id(record_id: str) -> dict:
    """按版本 id 从版本列表中查找条目"""
    versions = python_versions.get_python_versions()['versions']
    return next((item for item in versions if item["id"] == record_id), None)