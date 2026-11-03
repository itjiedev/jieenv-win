from django.apps import AppConfig

SOURCE_LABEL = '独立版本模式'


def _list_installed_pythons():
    from apps.python_archive import storage, env_ops
    records = storage.list_installed()
    if hasattr(env_ops, 'annotate_installed') and records:
        records = env_ops.annotate_installed(records)
    for r in records or []:
        r['_source_label'] = SOURCE_LABEL
    return records


def _resolve_pip_env(env_id: str):
    from apps.python_archive import storage_venv
    venv = storage_venv.get_venv_by_id(env_id)
    if venv and venv.get('python_exe'):
        return {
            'env_id': env_id,
            'python_exe': venv['python_exe'],
            'label': f"{venv.get('name', '')}（虚拟环境）",
        }

    from apps.python_archive import storage
    record = storage.get_by_id(env_id)
    if record and record.get('python_exe'):
        return {
            'env_id': env_id,
            'python_exe': record.get('python_exe', ''),
            'label': record.get('name') or record.get('version', ''),
        }
    return None


class PythonArchiveConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.python_archive'
    verbose_name = 'python_archive'

    def ready(self):
        from jiecore.app_services import register_service
        from .provider_services import (
            install_python_archive,
            preflight_python_archive,
            uninstall_python_archive,
            verify_python_archive,
            health_check,
            provider_health_check,
            list_available_python_versions,
            list_download_sources,
            scan_project_python_env,
        )
        register_service(
            'python_archive', 'list_installed_pythons', _list_installed_pythons,
            'list installed python records [{name, version, python_exe, install_dir, is_path_default, exe_dir}]')
        register_service(
            'python_archive', 'resolve_pip_env', _resolve_pip_env,
            'resolve archive env_id -> {env_id, python_exe, label} or None')
        register_service(
            'python_archive', 'install_python_archive', install_python_archive,
            'context_dict: 下载绿色版 Python 并解压安装')
        register_service(
            'python_archive', 'preflight_python_archive', preflight_python_archive,
            'context_dict: 安装前校验（版本 ID / 名称重复 / 敏感目录 / 源可达性）')
        register_service(
            'python_archive', 'uninstall_python_archive', uninstall_python_archive,
            'kwargs: 卸载指定记录的 Python 绿色版')
        register_service(
            'python_archive', 'verify_python_archive', verify_python_archive,
            'kwargs: 验证指定记录的 Python 是否已安装且可执行')
        register_service(
            'python_archive', 'health_check', health_check,
            'kwargs: 后台健康检查')
        register_service(
            'python_archive', 'provider_health_check', provider_health_check,
            '无参数: provider 级别健康检查（遍历所有已安装记录）')
        register_service(
            'python_archive', 'list_available_python_versions', list_available_python_versions,
            '返回可下载 Python 绿色版选项 list[dict]，带 value/label/version/default')
        register_service(
            'python_archive', 'list_download_sources', list_download_sources,
            '返回下载源选项 list[dict]')
        register_service(
            'python_archive', 'scan_project_python_env', scan_project_python_env,
            'context_dict: 扫描项目目录是否已有 venv')