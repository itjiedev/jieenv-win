"""
Conda 安装记录存储 — 基于 jiecore.json_storage.JsonStore。

存储路径: settings.DATA_DIR / 'conda' / 'installations.json'

JSON 根为数组，每条记录有自动生成的 id（uuid hex[:12]）作为主键，
install_dir 为业务唯一键（路径规范化后比较）。
"""
import os
import hashlib

from jiecore.json_storage import JsonStore

from . import config

_store = JsonStore(config.DATA_DIR / 'installations.json')


def _norm(p: str) -> str:
    return os.path.normpath(p.replace('/', os.sep)).lower()


def _id_for(install_dir: str) -> str:
    n = _norm(install_dir)
    return hashlib.md5(n.encode('utf-8')).hexdigest()[:12]


def list_installations() -> list:
    return _store.all()


def get_installation(install_dir: str = '', record_id: str = '') -> dict | None:
    if record_id:
        return _store.get(record_id)
    if install_dir:
        n = _norm(install_dir)
        return _store.find('install_dir', install_dir=n)
    return None


def upsert_installation(record: dict) -> dict:
    record = dict(record)
    install_dir = record.get('install_dir', '')
    record['id'] = _id_for(install_dir) if install_dir else record.get('id', '')
    n = _norm(install_dir)
    with _store._lock:
        data = _store.read()
        for i, item in enumerate(data):
            if isinstance(item, dict) and _norm(item.get('install_dir', '')) == n:
                data[i] = record
                _store.write(data)
                return record
        data.append(record)
        _store.write(data)
    return record


def sync_with_detected(detected_list: list[dict]) -> dict:
    """把 storage 与最新探测结果同步 ——
    同路径保留原 ID（upsert_installation 内部已按路径规范化 md5 生成固定 ID），
    新增路径 upsert，探测不到的旧记录移除。
    返回统计: {'added': N, 'updated': N, 'removed': N}
    """
    current = list_installations()
    current_by_norm = {
        _norm(c.get('install_dir', '')): c
        for c in current if c.get('install_dir')
    }

    detected_norms = set()
    added = updated = 0
    for item in detected_list:
        install_dir = item.get('install_dir', '')
        n = _norm(install_dir)
        detected_norms.add(n)
        if n in current_by_norm:
            updated += 1
        else:
            added += 1
        upsert_installation(dict(item))

    removed = 0
    for n, rec in current_by_norm.items():
        if n not in detected_norms:
            remove_installation(record_id=rec.get('id', ''))
            removed += 1

    return {'added': added, 'updated': updated, 'removed': removed}


def remove_installation(install_dir: str = '', record_id: str = '') -> bool:
    with _store._lock:
        data = _store.read()
        remain = []
        removed = False
        for item in data:
            if not isinstance(item, dict):
                remain.append(item)
                continue
            if record_id and item.get('id') == record_id:
                removed = True
                continue
            if install_dir and _norm(item.get('install_dir', '')) == _norm(install_dir):
                removed = True
                continue
            remain.append(item)
        if removed:
            _store.write(remain)
        return removed