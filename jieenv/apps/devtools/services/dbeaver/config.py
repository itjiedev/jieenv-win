from apps.devtools.config import DATA_DIR, DOWNLOAD_DIR

DBEAVER_DOWNLOAD_DIR = DOWNLOAD_DIR
DBEAVER_DATA_DIR = DATA_DIR / 'dbeaver'
DBEAVER_INSTALLED_SOURCES_FILE = DBEAVER_DATA_DIR / 'dbeaver_installed_sources.json'
DBEAVER_LATEST_CACHE_FILE = DBEAVER_DATA_DIR / 'dbeaver_latest_cache.json'

import pathlib
for _d in [DBEAVER_DOWNLOAD_DIR, DBEAVER_DATA_DIR]:
    _d.mkdir(parents=True, exist_ok=True)