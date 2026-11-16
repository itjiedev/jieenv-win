import pathlib

from apps.devtools.config import APP_DIR, DATA_DIR, DOWNLOAD_DIR

PYCHARM_DOWNLOAD_DIR = DOWNLOAD_DIR
PYCHARM_DATA_DIR = DATA_DIR / 'pycharm'
PYCHARM_SILENT_CONFIG_FILE = PYCHARM_DATA_DIR / 'silent.config'
PYCHARM_INSTALLED_SOURCES_FILE = PYCHARM_DATA_DIR / 'pycharm_installed_sources.json'
PYCHARM_VERSION_CACHE_FILE = PYCHARM_DATA_DIR / 'pycharm_versions_cache.json'

LIMIT_VERSION = 2016

for _d in [PYCHARM_DOWNLOAD_DIR]:
    _d.mkdir(parents=True, exist_ok=True)