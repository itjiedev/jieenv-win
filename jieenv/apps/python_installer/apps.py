from django.apps import AppConfig

SOURCE_LABEL = '安装程序模式'


def _list_installed_pythons():
    from apps.python_installer import storage, env_ops
    records = storage.list_installed()
    if hasattr(env_ops, 'annotate_installed') and records:
        py_default_tag = None
        if hasattr(env_ops, 'get_py_default_tag_fast'):
            try:
                py_default_tag = env_ops.get_py_default_tag_fast()
            except Exception:
                pass
        kwargs = {'py_default_tag': py_default_tag} if py_default_tag is not None else {}
        records = env_ops.annotate_installed(records, **kwargs)
    for r in records or []:
        r['_source_label'] = SOURCE_LABEL
    return records


def _resolve_pip_env(env_id: str):
    from apps.python_installer import storage_venv
    venv = storage_venv.get_venv_by_id(env_id)
    if venv and venv.get('python_exe'):
        return {
            'env_id': env_id,
            'python_exe': venv['python_exe'],
            'label': f"{venv.get('name', '')}（虚拟环境）",
        }

    from apps.python_installer import storage
    record = storage.get_by_id(env_id)
    if record and record.get('python_exe'):
        return {
            'env_id': env_id,
            'python_exe': record.get('python_exe', ''),
            'label': record.get('name') or f"Python {record.get('version', '')}",
        }
    return None


class PythonInstallerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.python_installer'
    verbose_name = 'python_installer'

    def ready(self):
        from jiecore.app_services import register_service
        from .provider_services import (
            install_python,
            preflight_python_install,
            uninstall_python,
            verify_python,
            provider_health_check,
            list_available_python_versions,
            list_download_sources,
            scan_project_python_env,
        )
        register_service(
            'python_installer', 'list_installed_pythons', _list_installed_pythons,
            'list installed python records [{name, version, python_exe, install_dir, is_path_default, is_py_default, exe_dir}]')
        register_service(
            'python_installer', 'resolve_pip_env', _resolve_pip_env,
            'resolve env_id -> {env_id, python_exe, label} or None')
        register_service(
            'python_installer', 'install_python', install_python,
            'context_dict: 安装指定版本 Python 解释器')
        register_service(
            'python_installer', 'preflight_python_install', preflight_python_install,
            'context_dict: 安装前校验（版本重复/敏感目录/源可达性）')
        register_service(
            'python_installer', 'uninstall_python', uninstall_python,
            'kwargs: 卸载指定版本 Python 解释器')
        register_service(
            'python_installer', 'verify_python', verify_python,
            'kwargs: 验证指定版本 Python 是否已安装且可执行')
        register_service(
            'python_installer', 'provider_health_check', provider_health_check,
            '无参数: provider 级别健康检查（遍历所有已安装版本）')
        register_service(
            'python_installer', 'list_available_python_versions', list_available_python_versions,
            '返回可下载 Python 版本选项 list[dict]，带 value/label/version/default')
        register_service(
            'python_installer', 'list_download_sources', list_download_sources,
            '返回下载源选项 list[dict]')
        register_service(
            'python_installer', 'scan_project_python_env', scan_project_python_env,
            'context_dict: 扫描项目目录是否已有 venv')