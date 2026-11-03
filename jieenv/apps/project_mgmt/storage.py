import uuid
import json
import logging
from datetime import datetime

from jiecore.json_storage import JsonStore
from .config import PROJECTS_FILE, TRASH_FILE, LAST_CURSOR_FILE

logger = logging.getLogger(__name__)

_store = JsonStore(PROJECTS_FILE, default=[])
_trash_store = JsonStore(TRASH_FILE, default=[])

_health_cache_dirty = True


def invalidate_health_cache():
    global _health_cache_dirty
    _health_cache_dirty = True


def _touch_health():
    global _health_cache_dirty
    _health_cache_dirty = False


def list_projects() -> list:
    return _store.all()


def get_project(project_id: str) -> dict | None:
    return _store.get(project_id)


def add_project(record: dict) -> dict:
    record = dict(record)
    record.setdefault('id', uuid.uuid4().hex[:12])
    record.setdefault('name', '')
    record.setdefault('template_id', 'empty')
    record.setdefault('project_dir', '')
    record.setdefault('status', 'active')
    record.setdefault('bindings', [])
    record.setdefault('created_at', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    record.setdefault('last_event_cursor', 0)
    result = _store.add(record)
    invalidate_health_cache()
    return result


def list_ready_projects() -> list:
    return _store.all()


def update_project(project_id: str, **fields) -> dict | None:
    result = _store.update(project_id, **fields)
    invalidate_health_cache()
    return result


def remove_project(project_id: str) -> bool:
    result = _store.remove(project_id)
    invalidate_health_cache()
    return result


def move_to_trash(project_id: str) -> bool:
    project = _store.get(project_id)
    if not project:
        return False
    record = dict(project)
    record['deleted_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _trash_store.add(record)
    _store.remove(project_id)
    invalidate_health_cache()
    return True


def list_trash() -> list:
    return list(_trash_store.all())


def restore_from_trash(project_id: str) -> bool:
    project = _trash_store.get(project_id)
    if not project:
        return False
    record = dict(project)
    record.pop('deleted_at', None)
    _store.add(record)
    _trash_store.remove(project_id)
    invalidate_health_cache()
    return True


def delete_permanent(project_id: str) -> bool:
    return _trash_store.remove(project_id)


def read_last_cursor() -> int:
    try:
        with open(LAST_CURSOR_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return int(data.get('cursor', 0))
    except Exception:
        return 0


def write_last_cursor(cursor: int) -> None:
    try:
        with open(LAST_CURSOR_FILE, 'w', encoding='utf-8') as f:
            json.dump({'cursor': cursor}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning('write_last_cursor failed: %s', e)


def apply_events(events: list) -> None:
    if not events:
        return
    logger.info('project_mgmt 启动补漏: 消费 %d 条事件', len(events))