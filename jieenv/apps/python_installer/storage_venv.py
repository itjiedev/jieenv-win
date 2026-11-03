import uuid
import logging
from datetime import datetime

from jiecore.json_storage import JsonStore
from .config import VENVS_FILE

logger = logging.getLogger(__name__)

_store = JsonStore(VENVS_FILE, default=[])

_by_id_cache = {}
_by_owner_cache = {}
_cache_dirty = True


def _invalidate_cache():
    global _cache_dirty
    _cache_dirty = True


def _ensure_cache():
    global _cache_dirty
    if _cache_dirty:
        _by_id_cache.clear()
        _by_owner_cache.clear()
        for r in _store.all():
            r.setdefault('locked', False)
            vid = r.get('id')
            if vid:
                _by_id_cache[vid] = r
            oid = r.get('owner_record_id')
            if oid:
                _by_owner_cache.setdefault(oid, []).append(r)
        _cache_dirty = False


def list_venvs(owner_record_id: str = None) -> list:
    _ensure_cache()
    if owner_record_id:
        return list(_by_owner_cache.get(owner_record_id, []))
    return _store.all()


def get_venv_by_id(venv_id: str) -> dict | None:
    _ensure_cache()
    return _by_id_cache.get(venv_id)


def get_venv_by_path(venv_path: str) -> dict | None:
    venv_path_norm = venv_path.replace('\\', '/').lower()
    for r in _store.all():
        vp = (r.get('venv_path') or '').replace('\\', '/').lower()
        if vp == venv_path_norm:
            return r
    return None


def add_venv(record: dict) -> dict:
    record = dict(record)
    record.setdefault('id', uuid.uuid4().hex[:12])
    record.setdefault('name', '')
    record.setdefault('owner_record_id', '')
    record.setdefault('owner_install_dir', '')
    record.setdefault('owner_version', '')
    record.setdefault('venv_path', '')
    record.setdefault('python_exe', '')
    record.setdefault('pip_count', 0)
    record.setdefault('include_system_site', False)
    record.setdefault('created_source', 'created')
    record.setdefault('project_id', '')
    record.setdefault('project_work_dir', '')
    record.setdefault('status', 'ready')
    record.setdefault('created_at', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    record.setdefault('last_verified_at', '')
    record.setdefault('locked', False)
    _store.add(record)
    _invalidate_cache()
    logger.info('登记虚拟环境: %s -> %s', record.get('name'), record.get('venv_path'))
    return dict(record)


def update_venv(venv_id: str, **kwargs) -> dict | None:
    result = _store.update(venv_id, **kwargs)
    _invalidate_cache()
    return result


def toggle_venv_lock(venv_id: str) -> dict | None:
    """切换虚拟环境的锁定状态，返回更新后的 dict"""
    record = _store.get(venv_id)
    if not record:
        return None
    new_locked = not record.get('locked', False)
    return update_venv(venv_id, locked=new_locked)


def remove_venv(venv_id: str) -> bool:
    record = _store.get(venv_id)
    if not record:
        return False
    result = _store.remove(venv_id)
    if result:
        _invalidate_cache()
        logger.info('移除虚拟环境记录: %s (%s)', venv_id, record.get('name'))
    return result


def clear_by_owner(owner_record_id: str) -> int:
    """级联删除归属某个 Python 实例的全部虚拟环境记录，返回删除数量。"""
    if not owner_record_id:
        return 0
    victims = list_venvs(owner_record_id=owner_record_id)
    for v in victims:
        _store.remove(v['id'])
    if victims:
        _invalidate_cache()
        logger.info('级联删除归属 %s 的虚拟环境记录: %d 条', owner_record_id, len(victims))
    return len(victims)


def clear_orphan_venvs(owner_ids: list) -> int:
    owner_set = set(owner_ids or [])
    orphans = [r for r in _store.all() if r.get('owner_record_id') and r['owner_record_id'] not in owner_set]
    for r in orphans:
        _store.remove(r['id'])
    if orphans:
        _invalidate_cache()
        logger.info('清理归属已丢失的虚拟环境记录: %d 条', len(orphans))
    return len(orphans)


def count_by_owner(owner_record_id: str) -> int:
    _ensure_cache()
    return len(_by_owner_cache.get(owner_record_id, []))