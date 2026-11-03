import json
import logging

from django.conf import settings
from config.constants import ENV_ROOT
from apps.mainapp.config import DB_INSTALL_ROOT

logger = logging.getLogger(__name__)

APP_LABEL = 'dbmysql'
DBMYSQL_DATA_DIR = settings.DATA_DIR / APP_LABEL
DBMYSQL_TMP_DIR = settings.TMP_DIR / APP_LABEL

DBMYSQL_CONFIG = DBMYSQL_DATA_DIR / 'dbmysql_config.json'
VERSION_CACHE_FILE = DBMYSQL_DATA_DIR / 'dbmysql_versions.json'
STORAGE_FILE = DBMYSQL_DATA_DIR / 'dbmysql_instances.json'

for _d in [DBMYSQL_DATA_DIR, DBMYSQL_TMP_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


# ── 应用配置管理 ─────────────────────────────────────────

_DEFAULT_CONFIG = {
    'install_dir': str(DB_INSTALL_ROOT),
}

def load_config() -> dict:
    """读取应用配置（JSON），文件不存在或损坏时返回默认值"""
    if not DBMYSQL_CONFIG.exists():
        return dict(_DEFAULT_CONFIG)
    try:
        with open(DBMYSQL_CONFIG, 'r', encoding='utf-8') as f:
            data = json.load(f)
        merged = dict(_DEFAULT_CONFIG)
        merged.update(data if isinstance(data, dict) else {})
        return merged
    except (json.JSONDecodeError, IOError) as e:
        logger.warning('读取 dbmysql 配置失败，使用默认值: %s', e)
        return dict(_DEFAULT_CONFIG)


def save_config(config: dict):
    """保存应用配置到 JSON 文件"""
    DBMYSQL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(DBMYSQL_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)