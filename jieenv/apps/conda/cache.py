"""
conda 模块 JSON 文件缓存 — 版本列表 + 环境列表。

持久化缓存，支持跨 Django 进程/重启保留。
版本缓存 key 带 condarc hash，镜像/频道切换自动失效。
"""
import hashlib
import json
import logging
import time
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)


def _file_hash(path: str | Path) -> str:
    try:
        h = hashlib.sha1()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
        return h.hexdigest()[:10]
    except Exception:
        return ''


def _read_cache(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read().strip()
            if not raw:
                return {}
            return json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('读取缓存 %s 失败: %s', path, e)
        return {}


def _write_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning('写入缓存 %s 失败: %s', path, e)


def _expire_entries(data: dict, now: float, ttl: int) -> dict:
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and (now - v.get('ts', 0)) < ttl}


# ── 版本缓存 ────────────────────────────────────────────────

def get_version_cache(record_id: str, condarc_hash: str, package: str) -> list | None:
    if not record_id or not condarc_hash:
        return None
    cache = _read_cache(config.VERSIONS_CACHE_FILE)
    key = f"{record_id}:{condarc_hash}:{package}"
    entry = cache.get(key)
    if not entry:
        return None
    now = time.time()
    if now - entry.get('ts', 0) >= config.VERSIONS_CACHE_TTL:
        return None
    return entry.get('data')


def set_version_cache(record_id: str, condarc_hash: str, package: str, data: list) -> None:
    if not record_id or not condarc_hash:
        return
    cache = _read_cache(config.VERSIONS_CACHE_FILE)
    key = f"{record_id}:{condarc_hash}:{package}"
    cache[key] = {'ts': time.time(), 'data': data}
    cache = _expire_entries(cache, time.time(), config.VERSIONS_CACHE_TTL)
    _write_cache(config.VERSIONS_CACHE_FILE, cache)


def invalidate_version_cache(record_id: str) -> None:
    if not record_id:
        return
    cache = _read_cache(config.VERSIONS_CACHE_FILE)
    prefix = f"{record_id}:"
    keys_to_del = [k for k in cache if k.startswith(prefix)]
    if keys_to_del:
        for k in keys_to_del:
            cache.pop(k, None)
        _write_cache(config.VERSIONS_CACHE_FILE, cache)


# ── 环境列表缓存 ──────────────────────────────────────────

def get_envs_cache(record_id: str) -> list | None:
    if not record_id:
        return None
    cache = _read_cache(config.ENVS_CACHE_FILE)
    entry = cache.get(record_id)
    if not entry:
        return None
    now = time.time()
    if now - entry.get('ts', 0) >= config.ENVS_CACHE_TTL:
        return None
    return entry.get('data')


def set_envs_cache(record_id: str, data: list) -> None:
    if not record_id:
        return
    cache = _read_cache(config.ENVS_CACHE_FILE)
    cache[record_id] = {'ts': time.time(), 'data': data}
    _write_cache(config.ENVS_CACHE_FILE, cache)


def invalidate_envs_cache(record_id: str) -> None:
    if not record_id:
        return
    cache = _read_cache(config.ENVS_CACHE_FILE)
    if cache.pop(record_id, None):
        _write_cache(config.ENVS_CACHE_FILE, cache)