import os
from django.apps import AppConfig


def _detect_project_deps(context):
    import os as _os
    project_dir = (context or {}).get('project_dir') or ''
    if not project_dir or not _os.path.isdir(project_dir):
        return {'success': False, 'message': '项目目录不存在'}

    project_dir = _os.path.abspath(project_dir)
    candidates = {
        'requirements.txt': 'pip',
        'requirements-dev.txt': 'pip',
        'pyproject.toml': 'pyproject',
        'Pipfile': 'pipfile',
        'poetry.lock': 'poetry',
        'setup.py': 'setuptools',
    }

    found = []
    for name, kind in candidates.items():
        fp = _os.path.join(project_dir, name)
        if _os.path.isfile(fp):
            found.append({
                'path': fp,
                'kind': kind,
                'language': 'python',
            })

    return {
        'success': True,
        'deps_files': found,
        'project_dir': project_dir,
        'language': 'python' if found else '',
    }


def _scan_python_env(context):
    import os as _os

    project_dir = (context or {}).get('project_dir') or ''
    if not project_dir or not _os.path.isdir(project_dir):
        return []

    project_dir = _os.path.abspath(project_dir)
    venv_candidates = ['.venv', 'venv', '.env', 'env']
    results = []

    for c in venv_candidates:
        full = _os.path.join(project_dir, c)
        if not _os.path.isdir(full):
            continue

        cfg_path = _os.path.join(full, 'pyvenv.cfg')
        conda_meta = _os.path.join(full, 'conda-meta')
        kind = ''
        home = ''

        if _os.path.isfile(cfg_path):
            kind = 'venv'
            try:
                with open(cfg_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('home') and '=' in line:
                            home = line.split('=', 1)[1].strip()
                            break
            except Exception:
                pass
        elif _os.path.isdir(conda_meta):
            kind = 'conda'
        elif (
            _os.path.isfile(_os.path.join(full, 'bin', 'python'))
            or _os.path.isfile(_os.path.join(full, 'Scripts', 'python.exe'))
        ):
            # 兼容旧版 virtualenv 等没有 pyvenv.cfg 的情况
            kind = 'venv-like'

        if kind:
            results.append({
                'path': full,
                'name': c,
                'is_venv': kind in ('venv', 'venv-like'),
                'kind': kind,
                'home': home,
                'language': 'python',
            })

    return results


def _create_venv_and_install(context):
    import os as _os

    from . import venv_utils

    ctx = context or {}
    use_venv = ctx.get('use_venv', True)
    python_exe = ctx.get('python_exe', '')
    venv_dir = ctx.get('venv_dir', '')
    system_site = ctx.get('system_site', False)
    on_output = ctx.get('on_output')

    if not use_venv:
        return {
            'success': True,
            'message': '未启用虚拟环境，跳过创建',
            'venv_python': '',
            'installed_packages': [],
        }

    if not python_exe:
        return {'success': False, 'message': '请选择 Python 版本'}
    if not venv_dir:
        return {'success': False, 'message': '虚拟环境目录不能为空'}

    create_result = venv_utils.create_venv(
        python_exe=python_exe,
        venv_dir=venv_dir,
        system_site=system_site,
        on_output=on_output,
    )
    if not create_result.get('success'):
        return create_result

    return {
        'success': True,
        'message': create_result.get('message', ''),
        'venv_python': create_result.get('venv_python', ''),
        'installed_packages': [],
    }


def _pip_install(context):
    import logging as _logging
    import os as _os
    import subprocess

    _log = _logging.getLogger(__name__)
    ctx = context or {}
    packages = (ctx.get('packages') or '').strip()
    target_dir = ctx.get('target_dir') or ''
    on_output = ctx.get('on_output')

    _log.warning('_pip_install called: target_dir=%r, packages=%r', target_dir, packages[:80])

    if not packages:
        return {'success': True, 'message': '包列表为空，跳过安装', 'installed_packages': []}

    venv_python = ''
    if target_dir:
        from . import venv_utils
        verify = venv_utils.verify_venv(target_dir)
        if verify.get('success'):
            venv_python = verify.get('venv_python', '')

    if not venv_python:
        venv_dirs = [
            _os.path.join(target_dir, '.venv'),
            _os.path.join(target_dir, 'venv'),
        ] if target_dir else []
        from . import venv_utils
        for vd in venv_dirs:
            if _os.path.isdir(vd):
                verify = venv_utils.verify_venv(vd)
                if verify.get('success'):
                    venv_python = verify.get('venv_python', '')
                    break

    if not venv_python:
        _log.warning('_pip_install: 未找到 venv_python，target_dir=%r', target_dir)
        return {'success': False, 'message': f'未找到可用的 venv_python (target_dir={target_dir})'}

    _log.warning('_pip_install: 使用 venv_python=%r', venv_python)

    package_list = [line.strip() for line in packages.splitlines() if line.strip()]
    if not package_list:
        return {'success': True, 'message': '包列表为空，跳过安装', 'installed_packages': []}

    creationflags = 0x08000000 if _os.name == 'nt' else 0
    env = _os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
        env.pop(key, None)
    env['PYTHONUTF8'] = '1'

    all_installed = []
    all_failed = []

    for pkg in package_list:
        try:
            proc = subprocess.Popen(
                [venv_python, '-m', 'pip', 'install', pkg],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=creationflags, env=env)
            for line in proc.stdout:
                line = (line or b'').decode('utf-8', errors='replace').rstrip()
                if line and on_output:
                    on_output(line)
            proc.wait()
            if proc.returncode != 0:
                all_failed.append({'pkg': pkg, 'code': proc.returncode})
            else:
                all_installed.append(pkg)
        except Exception as e:
            all_failed.append({'pkg': pkg, 'code': -1, 'error': str(e)})

    if all_failed:
        fail_msg = ', '.join(
            f"{f['pkg']}" + (f"(启动失败:{f['error']})" if f.get('code') == -1 else f"(退出码{f['code']})")
            for f in all_failed
        )
        return {
            'success': False,
            'message': f'部分包安装失败: {fail_msg}',
            'installed_packages': all_installed,
            'failed_packages': all_failed,
        }

    return {
        'success': True,
        'message': f'成功安装 {len(all_installed)} 个包',
        'installed_packages': all_installed,
        'failed_packages': [],
    }


def _run_python_module(context):
    import logging as _logging
    import os as _os
    import subprocess

    _log = _logging.getLogger(__name__)
    ctx = context or {}
    module_name = (ctx.get('module') or '').strip()
    args_str = (ctx.get('args') or '').strip()
    target_dir = ctx.get('target_dir') or ''
    project_name = (ctx.get('project_name') or '').strip()

    if not module_name or not target_dir:
        return {'success': False, 'error': '缺少 module 或 target_dir 参数'}

    venv_python = ''
    if target_dir:
        from . import venv_utils
        for vd_candidate in ['.venv', 'venv']:
            vd = _os.path.join(target_dir, vd_candidate)
            if _os.path.isdir(vd):
                verify = venv_utils.verify_venv(vd)
                if verify.get('success'):
                    venv_python = verify.get('venv_python', '')
                    break

    if not venv_python:
        _log.warning('_run_python_module: 未找到 venv_python，target_dir=%r', target_dir)
        return {'success': False, 'error': f'未找到可用的 venv_python (target_dir={target_dir})'}

    import shlex
    cmd = [venv_python, '-m', module_name] + shlex.split(args_str)
    _log.info('run_python_module: %s', cmd)

    creationflags = 0x08000000 if _os.name == 'nt' else 0
    env = _os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
        env.pop(key, None)
    env['PYTHONUTF8'] = '1'

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=target_dir, env=env,
            creationflags=creationflags,
        )
    except FileNotFoundError as e:
        return {'success': False, 'error': f'模块或 venv_python 执行失败: {e}'}
    except Exception as e:
        return {'success': False, 'error': f'执行异常: {e}'}

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or '').strip()
        return {'success': False, 'error': stderr[:500]}

    return {
        'success': True,
        'message': f'python -m {module_name} {args_str} 执行成功',
        'returncode': 0,
    }


def _list_python_versions(context=None):
    import os as _os

    from jiecore.app_services import (
        list_services, call_service,
        ServiceNotFoundError, ServiceCallError,
    )

    all_options = []
    seen_paths = set()

    def _collect_from(records, app_name):
        if not records:
            return
        for r in records:
            exe = (r.get('python_exe') or '').strip()
            if not exe or not _os.path.isfile(exe):
                continue
            norm = _os.path.normcase(exe)
            if norm in seen_paths:
                continue
            seen_paths.add(norm)
            name = r.get('name') or _os.path.basename(
                r.get('install_dir') or _os.path.dirname(exe)) or 'Python'
            version = r.get('version', '?')
            source_label = r.get('_source_label') or app_name
            suffix = source_label
            if r.get('is_path_default'):
                suffix += '(PATH 默认)'
            elif r.get('is_py_default'):
                suffix += '(py 默认)'
            label = f"{name}-{version}-{suffix}"
            is_default = bool(r.get('is_path_default')) or bool(r.get('is_py_default'))
            all_options.append({
                'value': exe,
                'label': label,
                'default': is_default,
                'version': version,
                '_source_app_name': app_name,
            })

    services = list_services()
    for app_name, svcs in services.items():
        if 'list_installed_pythons' not in svcs:
            continue
        try:
            records = call_service(app_name, 'list_installed_pythons')
        except (ServiceNotFoundError, ServiceCallError):
            continue
        _collect_from(records, app_name)

    if all_options and not any(o.get('default') for o in all_options):
        all_options[0]['default'] = True

    return all_options


def _provider_health_check() -> dict:
    """runtime.python provider 级别健康检测（无参数，健康缓存能直接调）。
    
    检查下游贡献应用是否至少有一个活跃（注册了 list_installed_pythons service）。
    不关心贡献者是禁用还是未安装——那是 project_mgmt 降级检测层的事。
    """
    from jiecore.app_services import list_services
    services = list_services()
    for app_name, svcs in services.items():
        if app_name == 'python_main':
            continue
        if 'list_installed_pythons' in svcs:
            return {'success': True}
    return {'success': False, 'error': '没有活跃的 Python 运行时数据源'}


def get_env_hints(context):
    """
    返回 runtime.python provider 的环境变量提示。
    
    context 由 provider_catalog 从 env_hints.config_schema 解析后传入:
      {python_exe, use_venv, venv_dir, project_dir}
    
    返回格式 (Windows):
      {
        "env": {"VIRTUAL_ENV": "...", "PATH": "..."},
        "PATH_prepend": ["D:\\Python313"],
        "activate_cmd": "call .venv\\Scripts\\activate.bat",
      }
    
    三种路径:
      - use_venv=True  → venv 激活信息（含 activate_cmd）
      - use_venv=False 但有 python_exe → PATH_prepend 指定解释器所在目录
      - 都没有 → {}
    """
    import os as _os

    ctx = context or {}
    python_exe = ctx.get('python_exe', '')
    use_venv = ctx.get('use_venv')

    if use_venv:
        pass
    elif python_exe:
        python_dir = _os.path.dirname(python_exe)
        return {
            'env': {},
            'PATH_prepend': [python_dir],
            'activate_cmd': '',
        }
    else:
        return {}

    venv_dir = ctx.get('venv_dir', '')
    if not venv_dir:
        return {}

    project_dir = ctx.get('project_dir', '')

    if not _os.path.isabs(venv_dir) and project_dir:
        venv_dir = _os.path.join(project_dir, venv_dir)

    venv_dir = _os.path.normpath(venv_dir)

    if not _os.path.isdir(venv_dir):
        return {}

    activate_bat = _os.path.join(venv_dir, 'Scripts', 'activate.bat')
    if not _os.path.isfile(activate_bat):
        return {}

    return {
        'env': {
            'VIRTUAL_ENV': venv_dir.replace('\\', '/'),
        },
        'PATH_prepend': [_os.path.join(venv_dir, 'Scripts')],
        'activate_cmd': f'call .venv{_os.sep}Scripts{_os.sep}activate.bat',
        'shell_hint': f'cmd /k "call "{activate_bat}""',
    }


class PythonConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.python_main'
    verbose_name = 'python'

    def ready(self):
        from jiecore.app_services import register_service
        from . import venv_utils

        register_service(
            'python_main', 'create_venv',
            venv_utils.create_venv,
            '创建虚拟环境 (python_exe, venv_dir, system_site, on_output)')

        register_service(
            'python_main', 'verify_venv',
            venv_utils.verify_venv,
            '验证虚拟环境有效性，返回 venv_python + python_version')

        register_service(
            'python_main', 'remove_venv',
            venv_utils.remove_venv,
            '彻底删除虚拟环境目录（仅文件系统）')

        register_service(
            'python_main', 'detect_project_deps',
            _detect_project_deps,
            '扫描项目目录识别依赖文件 (context={project_dir})')

        register_service(
            'python_main', 'scan_python_env',
            _scan_python_env,
            '扫描项目目录发现已有 venv (context={project_dir})')

        register_service(
            'python_main', 'create_venv_and_install',
            _create_venv_and_install,
            '组合服务：创建 venv + 可选安装 requirements (context=...)')

        register_service(
            'python_main', 'pip_install',
            _pip_install,
            '在已有 venv 里按包名列表安装 (context={packages, target_dir})')

        register_service(
            'python_main', 'list_python_versions',
            _list_python_versions,
            '列出系统所有可用 Python 版本，返回 [{value, label, default}]')

        register_service(
            'python_main', 'get_env_hints',
            get_env_hints,
            '返回 venv 激活环境变量提示 (context={python_exe, use_venv, venv_dir})')

        register_service(
            'python_main', 'run_python_module',
            _run_python_module,
            '在项目 venv 里执行 python -m {module} {args} (context={module, args, target_dir, project_name})')

        register_service(
            'python_main', 'provider_health_check',
            _provider_health_check,
            'runtime.python provider 级别健康检测（无参数，健康缓存可直接调）')

        try:
            from . import hooks
            hooks.on_enable({})
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).warning('python_main provider 自动注册失败: %s', e)