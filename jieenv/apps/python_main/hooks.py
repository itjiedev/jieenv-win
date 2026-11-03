"""
python_main 生命周期钩子
on_enable → 注册 provider_catalog provider（runtime.python）
on_uninstall → 注销归属本应用的所有 provider
"""
from jiecore.provider_catalog import (
    register_provider, unregister_providers_by_app,
)
import logging

logger = logging.getLogger(__name__)


def on_enable(context):
    pk = register_provider({
        "category": "runtime",
        "provider_id": "python",
        "app_name": "python_main",
        "label": "Python 运行时",
        "icon": "fa-brands fa-python",
        "contributor_name_prefix": "python",
        "lifecycle": {
            "create": {
                "service": ("python_main", "create_venv_and_install"),
                "service_mode": "context_dict",
                "config_schema": [
                    {"key": "python_exe", "label": "Python 版本", "type": "select",
                     "options_from": {"capability": "list_versions"},
                     "default": {"match": "default"},
                     "hint": "系统已安装的 Python 版本"},
                    {"key": "use_venv", "label": "使用虚拟环境", "type": "checkbox",
                     "default": True},
                    {"key": "venv_dir", "label": "虚拟环境目录", "type": "text",
                     "required": True,
                     "default": ".venv",
                     "default_from": "project_dir", "ui_widget": "dir_with_base",
                     },
                    {"key": "system_site", "label": "包含全局 site-packages",
                     "type": "checkbox", "default": False},
                ],
            },
            "verify": {"service": ("python_main", "verify_venv"), "service_mode": "kwargs", "config_schema": [
                {"key": "use_venv", "label": "使用虚拟环境", "type": "checkbox", "default": True},
                {"key": "venv_dir", "label": "虚拟环境目录", "type": "text", "required": False},
            ]},
            "delete": {
                "service": ("python_main", "remove_venv"),
                "service_mode": "kwargs",
                "config_schema": [
                    {"key": "venv_dir", "label": "虚拟环境目录", "type": "text", "required": True},
                ],
            },
            "health": {"service": ("python_main", "provider_health_check")},
            "install_packages": {
                "service": ("python_main", "pip_install"),
                "service_mode": "context_dict",
                "config_schema": [
                    {"key": "packages", "label": "包列表", "type": "textarea", "required": True,
                     "hint": "每行一个包，支持版本号，如 django>=4.2"},
                    {"key": "target_dir", "label": "目标目录", "type": "text",
                     "default_from": "project_dir", "hint": "虚拟环境所在的项目目录"},
                ],
            },
            "run_python_module": {
                "service": ("python_main", "run_python_module"),
                "service_mode": "context_dict",
                "config_schema": [
                    {"key": "module", "label": "Python 模块", "type": "text", "required": True,
                     "hint": "如 django / flask / pip / black"},
                    {"key": "args", "label": "模块参数", "type": "text", "required": False,
                     "hint": "如 startproject / install requests"},
                    {"key": "target_dir", "label": "项目目录", "type": "text", "required": True,
                     "default_from": "project_dir", "hint": "在该目录的 venv 里执行模块命令"},
                    {"key": "project_name", "label": "项目名", "type": "text", "required": False,
                     "default_from": "project_name",
                     "hint": "某些模块（如 django startproject）会创建同名子目录，引擎会自动 merge"},
                ],
            },
        },
        "capabilities": {
            "list_versions": {"service": ("python_main", "list_python_versions")},
            "detect_project_deps": {"service": ("python_main", "detect_project_deps")},
            "scan_python_env": {"service": ("python_main", "scan_python_env")},
            "env_hints": {
                "service": ("python_main", "get_env_hints"),
                "service_mode": "context_dict",
                "config_schema": [
                    {"key": "python_exe", "label": "Python 可执行文件", "type": "text",
                     "required": False},
                    {"key": "use_venv", "label": "使用虚拟环境", "type": "checkbox",
                     "default": True},
                    {"key": "venv_dir", "label": "虚拟环境目录", "type": "text",
                     "required": False},
                    {"key": "project_dir", "label": "项目目录", "type": "text",
                     "required": False, "default_from": "project_dir"},
                ],
            },
        },
    })
    logger.info('provider 注册: %s (runtime.python)', pk)


def on_uninstall(context):
    removed = unregister_providers_by_app("python_main")
    logger.info('provider 注销: %s', removed)