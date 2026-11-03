"""
python_installer provider 注册（runtime_item.python_installer）

职责：下载并静默安装 Python 解释器到指定目录。
project_mgmt 一键创建项目时，如果模板依赖 runtime_item.python_installer，会自动调 install_python。
"""
import logging

from jiecore.provider_catalog import (
    register_provider,
    unregister_providers_by_app,
)

logger = logging.getLogger(__name__)


def on_enable(context):
    pk = register_provider({
        "category": "runtime_item",
        "provider_id": "python_installer",
        "app_name": "python_installer",
        "label": "Python 解释器安装",
        "icon": "fa-brands fa-python",
        "lifecycle": {
            "create": {
                "service": ("python_installer", "install_python"),
                "service_mode": "context_dict",
                "config_schema": [
                    {
                        "key": "version",
                        "label": "Python 版本",
                        "type": "select",
                        "options_from": {"capability": "list_available_versions"},
                        "required": True,
                        "hint": "将下载并静默安装该版本的 Python 解释器",
                    },
                    {
                        "key": "install_dir",
                        "label": "安装目录",
                        "type": "text",
                        "default": "",
                        "ui_widget": "dir_with_base",
                        "hint": "留空则自动装到默认目录（D:\\Python\\<version>）",
                    },
                    {
                        "key": "download_source",
                        "label": "下载源",
                        "type": "select",
                        "options_from": {"capability": "list_sources"},
                        "default": {"match": "auto"},
                        "hint": "国内镜像更快，自动探测会依次 HEAD 测试可用性",
                    },
                    {
                        "key": "add_to_path",
                        "label": "加入系统 PATH",
                        "type": "checkbox",
                        "default": False,
                    },
                    {
                        "key": "install_pip",
                        "label": "带 pip",
                        "type": "checkbox",
                        "default": True,
                    },
                    {
                        "key": "install_py",
                        "label": "带 py 启动器",
                        "type": "checkbox",
                        "default": True,
                    },
                ],
            },
            "preflight": {
                "service": ("python_installer", "preflight_python_install"),
                "service_mode": "context_dict",
                "config_schema": [
                    {"key": "version", "label": "Python 版本", "type": "text", "required": True},
                    {"key": "install_dir", "label": "安装目录", "type": "text", "default": ""},
                    {"key": "download_source", "label": "下载源", "type": "text", "default": "auto"},
                ],
            },
            "delete": {
                "service": ("python_installer", "uninstall_python"),
                "service_mode": "kwargs",
                "config_schema": [
                    {"key": "version", "label": "Python 版本", "type": "text", "required": True},
                ],
            },
            "verify": {
                "service": ("python_installer", "verify_python"),
                "service_mode": "kwargs",
                "config_schema": [
                    {"key": "version", "label": "Python 版本", "type": "text", "required": True},
                ],
            },
            "health": {
                "service": ("python_installer", "provider_health_check"),
            },
        },
        "capabilities": {
            "list_available_versions": {
                "service": ("python_installer", "list_available_python_versions"),
            },
            "list_sources": {
                "service": ("python_installer", "list_download_sources"),
            },
            "scan_project_env": {
                "service": ("python_installer", "scan_project_python_env"),
                "service_mode": "context_dict",
                "config_schema": [
                    {"key": "project_dir", "label": "项目目录", "type": "text", "default_from": "project_dir"},
                ],
            },
        },
    })
    logger.info('provider 注册: %s (runtime_item.python_installer)', pk)


def on_uninstall(context):
    removed = unregister_providers_by_app("python_installer")
    logger.info('provider 注销: %s', removed)