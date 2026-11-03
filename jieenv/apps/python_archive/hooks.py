"""
python_archive provider 注册（runtime_item.python_archive）

职责：下载绿色版 Python 压缩包并解压安装到指定目录。
project_mgmt 一键创建项目时，如果模板依赖 runtime_item.python_archive，会自动调 install_python_archive。

安装方式与 python_installer（exe 静默安装）平级：
    python_installer → 官方安装程序静默安装
    python_archive   → 绿色版 zip/nupkg 解压安装
两者共享 python_main 聚合层的 list_versions 数据源地位。
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
        "provider_id": "python_archive",
        "app_name": "python_archive",
        "label": "Python 绿色版解压",
        "icon": "fa-solid fa-file-zipper",
        "lifecycle": {
            "create": {
                "service": ("python_archive", "install_python_archive"),
                "service_mode": "context_dict",
                "config_schema": [
                    {
                        "key": "version_id",
                        "label": "版本 ID",
                        "type": "select",
                        "options_from": {"capability": "list_available_versions"},
                        "required": True,
                        "hint": "从镜像站下载绿色版 zip/nupkg 解压安装",
                    },
                    {
                        "key": "version",
                        "label": "Python 版本号",
                        "type": "text",
                        "required": True,
                        "hint": "如 3.13.5，与版本 ID 对应的实际版本号",
                    },
                    {
                        "key": "name",
                        "label": "环境名称",
                        "type": "text",
                        "default": "",
                        "hint": "留空自动生成，如 Python-3.13.5",
                    },
                    {
                        "key": "install_dir",
                        "label": "安装目录",
                        "type": "text",
                        "default": "",
                        "ui_widget": "dir_with_base",
                        "hint": "留空自动装到默认目录（D:\\Python\\python_archive\\...）",
                    },
                    {
                        "key": "download_source",
                        "label": "下载源",
                        "type": "select",
                        "options_from": {"capability": "list_sources"},
                        "default": {"match": "auto"},
                        "hint": "国内镜像更快，自动探测会依次 HEAD 测试可用性",
                    },
                ],
            },
            "preflight": {
                "service": ("python_archive", "preflight_python_archive"),
                "service_mode": "context_dict",
                "config_schema": [
                    {"key": "version_id", "label": "版本 ID", "type": "text", "required": True},
                    {"key": "version", "label": "Python 版本号", "type": "text", "required": True},
                    {"key": "name", "label": "环境名称", "type": "text", "default": ""},
                    {"key": "install_dir", "label": "安装目录", "type": "text", "default": ""},
                    {"key": "download_source", "label": "下载源", "type": "text", "default": "auto"},
                ],
            },
            "delete": {
                "service": ("python_archive", "uninstall_python_archive"),
                "service_mode": "kwargs",
                "config_schema": [
                    {"key": "record_id", "label": "记录主键", "type": "text", "required": True},
                ],
            },
            "verify": {
                "service": ("python_archive", "verify_python_archive"),
                "service_mode": "kwargs",
                "config_schema": [
                    {"key": "record_id", "label": "记录主键", "type": "text", "required": True},
                ],
            },
            "health": {
                "service": ("python_archive", "provider_health_check"),
            },
        },
        "capabilities": {
            "list_available_versions": {
                "service": ("python_archive", "list_available_python_versions"),
            },
            "list_sources": {
                "service": ("python_archive", "list_download_sources"),
            },
            "scan_project_env": {
                "service": ("python_archive", "scan_project_python_env"),
                "service_mode": "context_dict",
                "config_schema": [
                    {"key": "project_dir", "label": "项目目录", "type": "text", "default_from": "project_dir"},
                ],
            },
        },
    })
    logger.info('provider 注册: %s (runtime_item.python_archive)', pk)


def on_uninstall(context):
    removed = unregister_providers_by_app("python_archive")
    logger.info('provider 注销: %s', removed)