import logging
import pathlib

from django.conf import settings

logger = logging.getLogger(__name__)

APP_LABEL = 'devtools'
APP_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = settings.DATA_DIR / APP_LABEL
TMP_DIR = settings.TMP_DIR / APP_LABEL
DOWNLOAD_DIR = TMP_DIR / 'download'
DEV_TOOLS_INSTALL_ROOT = settings.ENV_ROOT / 'dev_tools'

for _d in [DATA_DIR, TMP_DIR, DEV_TOOLS_INSTALL_ROOT, DOWNLOAD_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

PAGE_TITLE = '开发工具'

CATEGORY_MAP = {
    'ide': 'IDE / 编辑器',
    'db': '数据库管理',
    'vcs': '版本控制',
}

DEFAULT_CATEGORY_KEY = 'ide'
DEFAULT_CATEGORY_LABEL = CATEGORY_MAP[DEFAULT_CATEGORY_KEY]

STATUS_INSTALLED_TEXT = '已安装'
STATUS_NOT_INSTALLED_TEXT = '未安装'

INSTALLED_CACHE_MISS_TIPS = '启动后首次检测，请稍候…'