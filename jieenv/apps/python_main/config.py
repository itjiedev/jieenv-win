"""
python 应用配置

定义应用常量、数据目录、临时目录等。
"""
import logging
import pathlib

from django.conf import settings
from apps.mainapp.config import LANG_INSTALL_ROOT

logger = logging.getLogger(__name__)

APP_LABEL = 'python_main'
# 应用自身目录（随应用打包分发的静态资源，如 silent.config 静默安装配置）
APP_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = settings.DATA_DIR / APP_LABEL
TMP_DIR = settings.TMP_DIR / APP_LABEL
PYTHON_INSTALL_DIR = LANG_INSTALL_ROOT

# 开发工具（PyCharm 等）默认安装根目录
DEV_TOOLS_INSTALL_ROOT = settings.ENV_ROOT / 'dev_tools'
# PyCharm 安装包下载缓存目录
PYCHARM_DOWNLOAD_DIR = TMP_DIR / 'pycharm_download'
# PyCharm 静默安装配置文件（installer /CONFIG 参数指向此文件）
PYCHARM_SILENT_CONFIG_FILE = APP_DIR / 'data' / 'silent.config'
# VSCode 安装包下载缓存目录
VSCODE_DOWNLOAD_DIR = TMP_DIR / 'vscode_download'
# 环境记录存储文件
ENVIRONMENTS_FILE = DATA_DIR / 'python_environments.json'
# 版本列表缓存文件
VERSION_CACHE_FILE = DATA_DIR / 'python_versions_cache.json'
# 自定义 PyPI 源存储文件
PYPI_CUSTOM_SOURCES_FILE = DATA_DIR / 'pypi_custom_sources.json'
# pycharm 的安装信息，避免每次都检查系统
PYCHARM_INSTALLED_SOURCES_FILE = DATA_DIR / 'pycharm_installed_sources.json'
PYCHARM_VERSION_CACHE_FILE = DATA_DIR / 'pycharm_versions_cache.json'
# vscode 的安装信息，避免每次都检查系统
VSCODE_INSTALLED_SOURCES_FILE = DATA_DIR / 'vscode_installed_sources.json'
# vscode 最新版下载信息缓存（user / system 两种 installer 各一份）
VSCODE_LATEST_CACHE_FILE = DATA_DIR / 'vscode_latest_cache.json'


for _d in [DATA_DIR, TMP_DIR, PYTHON_INSTALL_DIR, DEV_TOOLS_INSTALL_ROOT,
           PYCHARM_DOWNLOAD_DIR, VSCODE_DOWNLOAD_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# 从官方抓取的 pycharm 最低版本,2016已经是最低年版本，不推荐修改的更低
LIMIT_VERSION =2016