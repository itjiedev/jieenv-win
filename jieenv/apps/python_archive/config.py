"""
python_archive 应用配置

定义应用常量、数据目录、临时目录等。
"""
import json
import logging

from django.conf import settings
from config.constants import ENV_ROOT
from apps.python_main.config import PYTHON_INSTALL_DIR
from apps.python_main.config import TMP_DIR as MAIN_TMP_DIR

logger = logging.getLogger(__name__)

APP_LABEL = 'python_archive'
APP_VERBOSE_NAME = 'Python 独立版本模式'
DATA_DIR = settings.DATA_DIR / APP_LABEL
TMP_DIR = MAIN_TMP_DIR / APP_LABEL
DOWNLOAD_DIR = TMP_DIR / 'downloads' / APP_LABEL

for _d in [DATA_DIR, TMP_DIR, DOWNLOAD_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

VERSION_CACHE_FILE = DATA_DIR / 'python_versions.json'
INSTALLED_FILE = DATA_DIR / 'installed.json'
DOWNLOAD_SOURCE_FILE = DATA_DIR / 'download_source.json'
CONFIG_FILE = DATA_DIR / 'installed_config.json'

VENVS_FILE = DATA_DIR / 'venvs.json'

DEFAULT_VENV_ROOT = ENV_ROOT / 'lang_envs' / 'python_archive_envs'

_DEFAULT_CONFIG = {
    'install_dir': str(PYTHON_INSTALL_DIR/APP_LABEL),
    'venv_default_dir': '',
    'strict_uninstall_check': False,
}