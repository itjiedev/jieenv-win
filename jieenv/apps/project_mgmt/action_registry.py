import logging

logger = logging.getLogger(__name__)


ACTION_REGISTRY = {
    'lang_packages': {
        'label': '语言包安装',
        'icon': 'fa-solid fa-box',
        'requires_target': True,
        'default_invoke_action': 'install_packages',
        'render_template': 'project_mgmt/create_lang_packages.html',
        'description': '在运行时环境中安装语言包依赖',
    },
}


def get_action_def(type_id: str) -> dict | None:
    return ACTION_REGISTRY.get(type_id)


def register_action(type_id: str, defn: dict):
    ACTION_REGISTRY[type_id] = defn
    logger.info('action_registry: registered %s', type_id)