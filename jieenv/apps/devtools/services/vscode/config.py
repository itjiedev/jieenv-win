from apps.devtools.config import DATA_DIR, DOWNLOAD_DIR

VSCODE_DOWNLOAD_DIR = DOWNLOAD_DIR
VSCODE_DATA_DIR = DATA_DIR / 'vscode'
VSCODE_INSTALLED_SOURCES_FILE = VSCODE_DATA_DIR / 'vscode_installed_sources.json'
VSCODE_LATEST_CACHE_FILE = VSCODE_DATA_DIR / 'vscode_latest_cache.json'

import pathlib
for _d in [VSCODE_DOWNLOAD_DIR]:
    _d.mkdir(parents=True, exist_ok=True)