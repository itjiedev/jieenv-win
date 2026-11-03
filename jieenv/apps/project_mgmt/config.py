import logging
from django.conf import settings

logger = logging.getLogger(__name__)

APP_LABEL = 'project_mgmt'
APP_VERBOSE_NAME = '项目管理'

DATA_DIR = settings.DATA_DIR / APP_LABEL
USER_TEMPLATES_DIR = DATA_DIR / 'user_templates'

for _d in [DATA_DIR, USER_TEMPLATES_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

PROJECTS_FILE = DATA_DIR / 'projects.json'
TRASH_FILE = DATA_DIR / 'deleted_projects.json'
LAST_CURSOR_FILE = DATA_DIR / 'last_cursor.json'