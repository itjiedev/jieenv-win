"""
python_installer 应用配置
"""
from django.conf import settings
from django.conf import settings
from apps.python_main.config import PYTHON_INSTALL_DIR
from apps.python_main.config import TMP_DIR as MAIN_TMP_DIR

APP_LABEL = 'python_installer'
DATA_DIR = settings.DATA_DIR / APP_LABEL
TMP_DIR = MAIN_TMP_DIR / APP_LABEL

# 安装包下载目录
DOWNLOAD_DIR = TMP_DIR / 'downloads'
# 版本列表缓存文件
VERSION_CACHE_FILE = DATA_DIR / 'python_installer_versions.json'
# 已安装版本记录文件
INSTALLED_FILE = DATA_DIR / 'python_installer_installed.json'
# 默认下载源配置文件（不存在或 auto=true 时表示自动探测）
DOWNLOAD_SOURCE_FILE = DATA_DIR / 'download_source.json'
# 应用配置文件
CONFIG_FILE = DATA_DIR / 'python_installer_config.json'

for _d in [DATA_DIR, TMP_DIR, DOWNLOAD_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

VENVS_FILE = DATA_DIR / 'python_installer_venvs.json'
DEFAULT_VENV_ROOT = settings.ENV_ROOT/ 'lang_envs' / 'python_installer_envs'

for _d2 in [DEFAULT_VENV_ROOT]:
    _d2.mkdir(parents=True, exist_ok=True)
