from django.conf import settings

# ── 项目级目录配置─────────
DB_INSTALL_ROOT = settings.ENV_ROOT / 'databases'
LANG_INSTALL_ROOT = settings.ENV_ROOT / 'lang_envs'
WORKSPACE_ROOT = settings.ENV_ROOT / 'workspace'

for _d in [DB_INSTALL_ROOT, LANG_INSTALL_ROOT, WORKSPACE_ROOT]:
    _d.mkdir(parents=True, exist_ok=True)