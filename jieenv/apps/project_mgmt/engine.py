import importlib.util
import keyword
import logging
import os
import re
import shutil
from collections import deque, defaultdict

from jiecore.provider_catalog import (
    invoke_provider, get_provider, list_providers, ProviderResult,
)
from jiecore import tasks as core_tasks
from . import storage, templates as project_templates

logger = logging.getLogger(__name__)


_VERSION_RE = re.compile(r'(\d+)\.(\d+)(?:\.(\d+))?')


def _ver_tuple(v: str) -> tuple:
    m = _VERSION_RE.search(v or '')
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _ver_gte(a: str, b: str) -> bool:
    return _ver_tuple(a) >= _ver_tuple(b)


def _extract_ver(option: dict) -> str:
    if isinstance(option, dict):
        return str(option.get('version', '') or '')
    return ''


def _pick_default(options: list) -> dict | None:
    for o in options:
        if isinstance(o, dict) and o.get('default'):
            return o
    return options[0] if options else None


def _pick_by_version_tier(options: list, default_ver: str) -> dict | None:
    if not default_ver or not options:
        return None

    dv = _ver_tuple(default_ver)
    dv_major_minor = dv[:2]

    def _vt(opt):
        return _ver_tuple(_extract_ver(opt))

    tier1 = [o for o in options if _vt(o) == dv]
    if tier1:
        return _pick_default(tier1)

    tier2 = [o for o in options if _vt(o)[:2] == dv_major_minor]
    if tier2:
        return _pick_default(tier2)

    tier3 = [o for o in options if _vt(o) > dv]
    if tier3:
        return _pick_default(tier3)

    tier4 = [o for o in options if _vt(o) < dv]
    if tier4:
        return _pick_default(tier4)

    return None


def _resolve_select_pick(options: list, prefs: dict, field_key: str, field: dict) -> dict | None:
    picked = None

    default_ver = prefs.get('default_version')
    if default_ver:
        tier_picked = _pick_by_version_tier(options, default_ver)
        if tier_picked:
            return tier_picked

    key_pref = prefs.get(field_key)
    if isinstance(key_pref, str):
        picked = next((o for o in options if o.get('value') == key_pref), None)
    elif isinstance(key_pref, dict):
        if key_pref.get('value'):
            picked = next((o for o in options if o.get('value') == key_pref['value']), None)
        if not picked and key_pref.get('match'):
            m = key_pref['match']
            if m == 'default':
                picked = next((o for o in options if o.get('default')), None)
            else:
                picked = next((o for o in options if m in str(o.get('label', ''))), None)

    if not picked and isinstance(field.get('default'), dict):
        dm = field['default'].get('match', '')
        if dm == 'default':
            picked = next((o for o in options if o.get('default')), None)
        elif dm:
            picked = next((o for o in options if dm in str(o.get('label', ''))), None)

    if not picked:
        picked = _pick_default(options)

    return picked


def _preflight_check(bindings: list, configs: dict,
                     project_name: str = '', project_dir: str = '',
                     dir_slug: str = '') -> tuple[list, list, list]:
    errors = []
    warnings = []

    for b in bindings:
        if not b.get('provider_key'):
            continue
        pk = b['provider_key']
        provider = get_provider(pk)
        if not provider:
            continue

        prefs = b.get('preferences', {}) or {}
        schema = (provider.get('lifecycle') or {}).get('create', {}).get('config_schema', []) or []
        user_config = configs.get(pk, {}) or {}

        for field in schema:
            key = field.get('key', '')

            default_from = field.get('default_from')
            if default_from and not user_config.get(key):
                if default_from == 'project_name' and project_name:
                    user_config[key] = project_name
                elif default_from == 'dir_slug' and dir_slug:
                    user_config[key] = dir_slug
                elif default_from == 'project_dir' and project_dir:
                    user_config[key] = os.path.join(project_dir, str(field.get('default', ''))) if field.get('default') and not os.path.isabs(str(field.get('default', ''))) else project_dir

            if field.get('type') == 'select' and 'options_from' in field:
                cap = field['options_from'].get('capability', '')
                if not cap:
                    continue

                try:
                    rr = invoke_provider(pk, cap)
                    options = rr.data if rr.success and isinstance(rr.data, list) else []
                except Exception:
                    options = []

                if not options:
                    errors.append({
                        'provider_key': pk,
                        'message': f"未发现可用选项（{field.get('label', key)}），请先在对应应用中配置",
                    })
                    continue

                missing_ver_opts = [o for o in options if not _extract_ver(o)]
                if missing_ver_opts:
                    warnings.append({
                        'provider_key': pk,
                        'message': f"部分选项缺少 version 字段（{field.get('label', key)}），版本追踪可能不完整",
                    })

                if user_config.get(key):
                    picked_opt = next((o for o in options if o.get('value') == user_config[key]), None)
                    if picked_opt:
                        opt_ver = _extract_ver(picked_opt)
                        if opt_ver:
                            user_config[f'{key}_version'] = opt_ver
                        if picked_opt.get('_source_app_name'):
                            user_config['_contributor_app'] = picked_opt['_source_app_name']
                    continue

                picked = _resolve_select_pick(options, prefs, key, field)
                if picked is None:
                    errors.append({
                        'provider_key': pk,
                        'message': f"无法为 {field.get('label', key)} 匹配可用选项，请检查配置",
                    })
                    continue

                user_config[key] = picked.get('value')
                ver = _extract_ver(picked)
                if ver:
                    user_config[f'{key}_version'] = ver
                if picked.get('_source_app_name'):
                    user_config['_contributor_app'] = picked['_source_app_name']

        configs[pk] = user_config

    if project_dir and os.path.exists(project_dir):
        errors.append({'provider_key': '', 'message': f'项目目录已存在: {project_dir}'})

    for b in bindings:
        pk = b.get('provider_key')
        if not pk:
            continue
        provider = get_provider(pk)
        if not provider:
            continue
        lifecycle = provider.get('lifecycle') or {}
        preflight_entry = lifecycle.get('preflight')

        if preflight_entry:
            user_config = configs.get(pk, {}) or {}
            rr = invoke_provider(pk, 'preflight', config=user_config)
            if isinstance(rr.data, dict):
                for w in (rr.data.get('warnings') or []):
                    warnings.append({'provider_key': pk, 'message': w})
                for e in (rr.data.get('errors') or []):
                    errors.append({'provider_key': pk, 'message': e})
                if not rr.success and not (rr.data.get('errors') or rr.data.get('warnings')):
                    errors.append({
                        'provider_key': pk,
                        'message': rr.error_msg or rr.error_code or '预校验调用失败',
                    })
            else:
                errors.append({
                    'provider_key': pk,
                    'message': rr.error_msg or rr.error_code or '预校验调用失败',
                })

    return errors, warnings


def _topo_sort(bindings: list) -> list:
    """Kahn 算法拓扑排序，按 depends_on 排。"""
    graph = {}
    in_degree = {}
    binding_map = {}

    for b in bindings:
        pk = b['provider_key']
        binding_map[pk] = b
        graph[pk] = []
        in_degree[pk] = 0

    for b in bindings:
        pk = b['provider_key']
        for dep in b.get('depends_on', []) or []:
            if dep in in_degree:
                graph[dep].append(pk)
                in_degree[pk] += 1

    queue = deque([k for k, v in in_degree.items() if v == 0])
    result = []
    while queue:
        node = queue.popleft()
        result.append(binding_map[node])
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(bindings):
        raise ValueError('模板 bindings 存在循环依赖: %s' % [b['provider_key'] for b in bindings])

    return result


def _resolve_actual_provider_key(template_pk: str) -> str | None:
    """模板里写的 provider_key（如 runtime.python）可能和 hooks.py 注册的 provider_key 不完全一致，这里做一次归一化。"""
    provider = get_provider(template_pk)
    if provider is not None:
        return provider.get('provider_key')
    return None


def _extract_pkg_name(spec: str) -> str:
    """从 pip spec 提取包名（小写）。"""
    if not spec:
        return ''
    m = re.split(r'[<>=!~\[]', str(spec).strip(), 1)
    return m[0].strip().lower() if m else ''


def _log(logs_list, task, msg):
    logs_list.append(msg)
    if task is not None:
        core_tasks.append_log(task, msg)


def _err(errors_list, pk, msg):
    entry = {'provider_key': pk or '', 'message': msg}
    errors_list.append(entry)
    return entry


def _progress(task, **kwargs):
    if task is not None:
        core_tasks.set_progress(task, **kwargs)


def execute_project_creation(
    project_name: str,
    template_id: str,
    project_dir: str,
    configs: dict,
    task: dict | None = None,
) -> dict:
    """执行项目创建。

    Args:
        project_name: 用户填的项目名
        template_id: 所选模板 ID
        project_dir: 项目目录路径
        configs: { provider_key: {...组件配置字典...}, ... }
        task: 可选的 jiecore.tasks 任务 dict；传入时日志/进度会同步到任务注册表，
              供前端 JieTaskLog 弹窗轮询。不传时保持原行为（仅内存列表）。

    Returns:
        {
            'success': bool,
            'project_id': str,
            'errors': [{'provider_key': str, 'message': str}, ...],
            'logs': [str, ...],
        }
    """
    logs = []
    errors = []

    _progress(task, progress=5, stage='初始化')

    tmpl = project_templates.get_template(template_id)
    if tmpl is None:
        _err(errors, '', '模板不存在: %s' % template_id)
        _log(logs, task, '[fail] 模板不存在: %s' % template_id)
        return {
            'success': False,
            'project_id': '',
            'errors': errors,
            'logs': logs,
        }

    bindings = tmpl.get('bindings', []) or []

    has_python_module_fix = any(
        b.get('project_dir_fix') == 'python_module_name' for b in bindings
    )
    if has_python_module_fix and project_dir:
        _sub = os.path.basename(project_dir)
        _needs_fix = False
        _reason = ''
        if not _sub.isidentifier():
            _needs_fix = True
            _reason = '不是合法 Python 标识符'
        elif keyword.iskeyword(_sub):
            _needs_fix = True
            _reason = '是 Python 保留字'
        elif importlib.util.find_spec(_sub) is not None:
            _needs_fix = True
            _reason = '与已安装模块冲突'
        if _needs_fix:
            import secrets
            _old_dir = project_dir
            _parent = os.path.dirname(project_dir)
            while True:
                _suffix = secrets.token_hex(2)
                _new_sub = '%s_%s' % (_sub, _suffix)
                _new_dir = os.path.join(_parent, _new_sub)
                if not os.path.exists(_new_dir):
                    break
            _log(logs, task, '[warn] 项目目录名 "%s" %s，自动调整为 "%s"' % (_sub, _reason, _new_sub))
            project_dir = _new_dir

            def _replace_in(obj):
                if isinstance(obj, str):
                    return obj.replace(_old_dir, _new_dir).replace(_old_dir.replace('\\', '/'), _new_dir.replace('\\', '/'))
                if isinstance(obj, dict):
                    return {k: _replace_in(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_replace_in(v) for v in obj]
                return obj

            for _pk in list(configs.keys()):
                configs[_pk] = _replace_in(configs[_pk])

    provider_bindings = [b for b in bindings if b.get('provider_key')]
    ordered_bindings = _topo_sort(provider_bindings)

    preflight_errors, preflight_warnings = _preflight_check(
        provider_bindings, configs,
        project_name=project_name, project_dir=project_dir,
        dir_slug=os.path.basename(project_dir) if project_dir else '',
    )

    if preflight_warnings:
        for w in preflight_warnings:
            msg = w.get('message') if isinstance(w, dict) else str(w)
            _log(logs, task, '[preflight-warn] %s' % msg)
            errors.append(w if isinstance(w, dict) else {'provider_key': '', 'message': str(w)})

    if preflight_errors:
        for e in preflight_errors:
            msg = e.get('message') if isinstance(e, dict) else str(e)
            _log(logs, task, '[preflight-fail] %s' % msg)
            errors.append(e if isinstance(e, dict) else {'provider_key': '', 'message': str(e)})
        return {
            'success': False,
            'project_name': project_name,
            'project_dir': project_dir,
            'project_id': '',
            'errors': errors,
            'logs': logs,
            'created_items': [],
            'action_results': [],
        }

    successfully_created = []

    _log(logs, task, '[precheck] 准备创建项目 "%s"，共 %d 个组件' % (project_name, len(ordered_bindings)))

    total_phases = len(ordered_bindings) + 1
    phase_idx = 0

    for binding in ordered_bindings:
        template_pk = binding['provider_key']
        actual_pk = _resolve_actual_provider_key(template_pk)
        phase_idx += 1
        pct = 5 + int(phase_idx / total_phases * 85)

        if actual_pk is None:
            from jiecore.provider_catalog import find_provider_app
            owner_app = find_provider_app(template_pk)
            if owner_app:
                from jiecore.app_manager import AppManager
                if owner_app in AppManager.get_all_apps():
                    msg = f'provider 已禁用: {template_pk}（应用 {owner_app} 未启用，请前往应用管理启用）'
                else:
                    msg = f'provider 未安装或不存在: {template_pk}'
            else:
                msg = f'provider 未安装或不存在: {template_pk}'
            _err(errors, template_pk, msg)
            _log(logs, task, '[fail] %s' % msg)
            _progress(task, progress=pct, stage='组件创建失败')
            break

        label = binding.get('label', actual_pk)
        binding_config = configs.get(template_pk, configs.get(actual_pk, {})) or {}

        _progress(task, progress=pct, stage='创建 %s' % label)
        _log(logs, task, '[create] 正在创建 %s (%s)...' % (label, actual_pk))

        try:
            result = invoke_provider(
                actual_pk, 'create',
                project_name=project_name,
                project_dir=project_dir,
                config=binding_config,
            )
            if not result.success:
                msg = '创建 %s 失败: %s' % (label, result.error_msg or 'unknown')
                _err(errors, template_pk, msg)
                _log(logs, task, '[fail] %s' % msg)
                _progress(task, progress=pct, stage='组件创建失败')
                break

            _log(logs, task, '[ok] %s 创建成功' % label)
            successfully_created.append({
                'template_pk': template_pk,
                'actual_pk': actual_pk,
                'label': label,
                'binding': binding,
                'binding_config': binding_config,
                'provider_result': result.data,
            })
        except Exception as e:
            msg = '创建 %s 异常: %s' % (label, e)
            _err(errors, template_pk, msg)
            _log(logs, task, '[fail] %s' % msg)
            _progress(task, progress=pct, stage='组件创建异常')
            logger.exception(msg)
            break

    if errors:
        if successfully_created:
            _log(logs, task, '[rollback] 开始回滚 %d 个已创建组件...' % len(successfully_created))
            for item in reversed(successfully_created):
                pk = item['actual_pk']
                label = item['label']
                rollback_config = item.get('binding_config', {}) or configs.get(item['template_pk'], {})
                _log(logs, task, '[rollback] 正在回滚 %s...' % label)
                try:
                    _rr = invoke_provider(
                        pk, 'delete',
                        config=rollback_config,
                        project_name=project_name,
                        project_dir=project_dir,
                    )
                    if _rr.success or _rr.error_code in (ProviderResult.E_NOT_FOUND, ProviderResult.E_APP_DISABLED):
                        _log(logs, task, '[rollback] %s 已回滚' % label)
                    else:
                        _log(logs, task, '[rollback-warn] %s 回滚警告: %s' % (label, _rr.error_msg or _rr.error_code))
                except Exception as e:
                    logger.warning('回滚 %s 失败: %s', label, e)
                    _log(logs, task, '[rollback-warn] %s 回滚失败: %s' % (label, e))
                    _err(errors, item['template_pk'], '%s 回滚失败: %s' % (label, e))

            if project_dir and os.path.isdir(project_dir):
                _log(logs, task, '[rollback] 清理项目目录 %s...' % project_dir)
                try:
                    shutil.rmtree(project_dir)
                    _log(logs, task, '[rollback] 项目目录已清理')
                except Exception as e:
                    _log(logs, task, '[rollback-warn] 项目目录清理失败: %s（请手动检查）' % e)

        _progress(task, progress=95, stage='执行失败')
        return {
            'success': False,
            'project_name': project_name,
            'project_dir': project_dir,
            'project_id': '',
            'errors': errors,
            'logs': logs,
            'created_items': successfully_created,
            'action_results': [],
        }

    _log(logs, task, '[phase2] 开始执行后置动作...')
    _progress(task, progress=90, stage='后置动作')

    from . import action_registry

    action_groups = defaultdict(list)
    for b in bindings:
        if not b.get('type') or b.get('provider_key'):
            continue
        target_pk = b.get('target_provider_key')
        action_groups[target_pk].append(b)

    action_results = []

    for target_pk, action_bindings in action_groups.items():
        for action_binding in action_bindings:
            type_id = action_binding.get('type', '')
            action_def = action_registry.get_action_def(type_id)
            label = action_binding.get('label', action_def.get('label', type_id) if action_def else type_id)

            invoke_action = action_binding.get('invoke_action')
            if not invoke_action and action_def:
                invoke_action = action_def.get('default_invoke_action')

            if action_def and action_def.get('requires_target') and not target_pk:
                _log(logs, task, '[skip] %s: requires target_provider_key but none provided' % label)
                continue

            if not invoke_action:
                _log(logs, task, '[skip] %s: no invoke_action defined' % label)
                continue

            if not target_pk:
                _log(logs, task, '[skip] %s: standalone actions not yet supported' % label)
                continue

            actual_pk = _resolve_actual_provider_key(target_pk)
            if actual_pk is None:
                from jiecore.provider_catalog import find_provider_app
                owner_app = find_provider_app(target_pk)
                if owner_app:
                    from jiecore.app_manager import AppManager
                    if owner_app in AppManager.get_all_apps():
                        reason = f'provider 已禁用（应用 {owner_app} 未启用）'
                    else:
                        reason = 'provider 未安装'
                else:
                    reason = 'provider 未安装'
                _log(logs, task, '[warn] %s: %s %s，跳过（项目主体已建）' % (label, reason, target_pk))
                continue

            provider_def = get_provider(actual_pk) or {}
            lifecycle = provider_def.get('lifecycle') or {}
            action_entry = lifecycle.get(invoke_action) or {}
            if not action_entry:
                _log(logs, task, '[skip] %s: %s 无 %s 动作' % (label, actual_pk, invoke_action))
                continue

            is_lang_packages = (type_id == 'lang_packages')

            if is_lang_packages:
                template_pkg_specs = {}
                for tb in bindings:
                    if tb.get('type') == 'lang_packages' and not tb.get('provider_key'):
                        tp = (tb.get('params') or {}).get('packages', [])
                        if isinstance(tp, str):
                            tp = [p.strip() for p in tp.splitlines() if p.strip()]
                        for spec in tp:
                            nm = _extract_pkg_name(spec)
                            if nm:
                                template_pkg_specs[nm] = spec

                user_pkgs = None
                ao = configs.get('_action_overrides') if isinstance(configs, dict) else None
                if ao and isinstance(ao, dict):
                    action_id = 'lang_packages:' + (target_pk or '')
                    if action_id in ao and isinstance(ao[action_id], dict):
                        user_pkgs = ao[action_id].get('packages')

                params = dict(action_binding.get('params', {}) or {})
                final_pkgs = user_pkgs if user_pkgs is not None else params.get('packages', [])
                if isinstance(final_pkgs, str):
                    final_pkgs = [p.strip() for p in final_pkgs.splitlines() if p.strip()]

                if not final_pkgs:
                    _log(logs, task, '[skip] %s: 包列表为空' % label)
                    continue

                params['packages'] = '\n'.join(final_pkgs)

                _log(logs, task, '[action] %s → %s.%s (%d 个包)...' % (label, actual_pk, invoke_action, len(final_pkgs)))

                batch_result = invoke_provider(
                    actual_pk, invoke_action,
                    project_name=project_name,
                    project_dir=project_dir,
                    config=params,
                )

                if batch_result.success:
                    _log(logs, task, '[ok] %s 完成' % label)
                    action_results.append({
                        'type_id': type_id,
                        'template_pk': target_pk,
                        'actual_pk': actual_pk,
                        'label': label,
                        'binding': action_binding,
                        'provider_result': batch_result.data if isinstance(batch_result.data, dict) else {},
                        '_pkg_status': {'installed_ok': final_pkgs, 'skipped_packages': [], 'fallback_used': []},
                        '_executed_config': dict(params),
                    })
                    continue

                _log(logs, task, '[batch-partial] 批量部分失败: %s' % (batch_result.error_msg or '未知错误'))
                logger.warning('lang_packages batch partial: %s', batch_result.error_msg, exc_info=True)

                batch_data = batch_result.data if isinstance(batch_result.data, dict) else {}
                batch_installed = batch_data.get('installed_packages', []) or []
                batch_failed = batch_data.get('failed_packages', []) or []

                if not batch_failed:
                    failed_specs = []
                    for m in re.finditer(r'[:，,]\s*([^,，(]+?)\(', batch_result.error_msg or ''):
                        spec = m.group(1).strip()
                        if spec:
                            failed_specs.append(spec)
                    specs_to_handle = failed_specs
                    batch_installed = [p for p in final_pkgs if p not in failed_specs]
                else:
                    specs_to_handle = [f.get('pkg', '') for f in batch_failed if f.get('pkg')]

                ok_pkgs = list(batch_installed)
                skipped_pkgs = []
                fallback_used = []

                if not specs_to_handle:
                    _log(logs, task, '[done] %s: 成功 %d 个，跳过 0 个' % (label, len(ok_pkgs)))
                else:
                    _log(logs, task, '[retry] 批量成功 %d 个，失败 %d 个，开始逐个处理失败的...' % (len(ok_pkgs), len(specs_to_handle)))

                    for spec in specs_to_handle:
                        if not spec:
                            continue
                        nm = _extract_pkg_name(spec)
                        if not nm:
                            skipped_pkgs.append(spec)
                            _log(logs, task, '  ⚠ 包名无法解析: %s' % spec)
                            continue

                        is_template_pkg = nm in template_pkg_specs
                        user_modified_template = is_template_pkg and template_pkg_specs[nm] != spec

                        if user_modified_template:
                            retry_spec = template_pkg_specs[nm]
                            retry_params = dict(params)
                            retry_params['packages'] = retry_spec
                            retry_rr = invoke_provider(
                                actual_pk, invoke_action,
                                project_name=project_name,
                                project_dir=project_dir,
                                config=retry_params,
                            )
                            if retry_rr.success:
                                ok_pkgs.append(retry_spec)
                                fallback_used.append({'requested': spec, 'fallback': retry_spec})
                                _log(logs, task, '  ⚠ 模板包 %s 未能按 %s 安装，已回退到模板版本 %s' % (nm, spec, retry_spec))
                            else:
                                skipped_pkgs.append(spec)
                                _log(logs, task, '  ⚠ 模板包 %s 回退 %s 也失败: %s' % (nm, retry_spec, retry_rr.error_msg or '未知错误'))
                        else:
                            single_params = dict(params)
                            single_params['packages'] = spec
                            single_rr = invoke_provider(
                                actual_pk, invoke_action,
                                project_name=project_name,
                                project_dir=project_dir,
                                config=single_params,
                            )
                            if single_rr.success:
                                ok_pkgs.append(spec)
                                _log(logs, task, '  ✓ 包 %s 单独安装成功' % spec)
                            else:
                                skipped_pkgs.append(spec)
                                _log(logs, task, '  ⚠ 包 %s 安装失败: %s' % (spec, single_rr.error_msg or '未知错误'))

                    _log(logs, task, '[done] %s: 成功 %d 个，跳过 %d 个' % (label, len(ok_pkgs), len(skipped_pkgs)))

                action_results.append({
                    'type_id': type_id,
                    'template_pk': target_pk,
                    'actual_pk': actual_pk,
                    'label': label,
                    'binding': action_binding,
                    'provider_result': {},
                    '_pkg_status': {
                        'installed_ok': ok_pkgs,
                        'skipped_packages': skipped_pkgs,
                        'fallback_used': fallback_used,
                    },
                    '_executed_config': dict(params),
                })
                continue

            params = dict(action_binding.get('params', {}) or {})
            for _k, _v in list(params.items()):
                if isinstance(_v, str):
                    params[_k] = _v.replace('{PROJECT_NAME}', project_name).replace('{PROJECT_DIR}', project_dir).replace('{PROJECT_SUB_DIR}', os.path.basename(project_dir)).replace('{PROJECT_PARENT_DIR}', os.path.dirname(project_dir))
            _log(logs, task, '[action] %s → %s.%s ...' % (label, actual_pk, invoke_action))

            try:
                result = invoke_provider(
                    actual_pk, invoke_action,
                    project_name=project_name,
                    project_dir=project_dir,
                    config=params,
                )
                if not result.success:
                    _log(logs, task, '[warn] %s 失败: %s（项目主体已建，继续）' % (label, result.error_msg or 'unknown'))
                    continue

                _log(logs, task, '[ok] %s 完成' % label)
                action_results.append({
                    'type_id': type_id,
                    'template_pk': target_pk,
                    'actual_pk': actual_pk,
                    'label': label,
                    'binding': action_binding,
                    'provider_result': result.data if isinstance(result.data, dict) else {},
                    '_executed_config': dict(params),
                })
            except Exception as e:
                logger.warning('%s 异常: %s', label, e)
                _log(logs, task, '[warn] %s 异常: %s（项目主体已建，继续）' % (label, e))

    binding_records = []
    for item in successfully_created:
        binding_records.append({
            'provider_key': item['template_pk'],
            'actual_provider_key': item['actual_pk'],
            'label': item['label'],
            'binding_type': 'provider',
            'config': item.get('binding_config') or configs.get(item['template_pk'], {}),
            'state': 'ACTIVE',
            'depends_on': item['binding'].get('depends_on', []),
            'created_result': item['provider_result'],
        })
    for item in action_results:
        action_binding = item.get('binding', {})
        executed_config = item.get('_executed_config')
        if executed_config:
            config_store = dict(executed_config)
        else:
            config_store = dict(action_binding.get('params', {}) or {})
        config_store['_action_type'] = item.get('type_id', '')
        pkg_status = item.get('_pkg_status')
        if pkg_status:
            config_store['packages'] = '\n'.join(pkg_status.get('installed_ok', []))
            config_store['_skipped_packages'] = pkg_status.get('skipped_packages', [])
            config_store['_fallback_used'] = pkg_status.get('fallback_used', [])
        binding_records.append({
            'provider_key': item['template_pk'],
            'actual_provider_key': item['actual_pk'],
            'label': item['label'],
            'binding_type': 'action',
            'action_type': item.get('type_id', ''),
            'invoke_action': action_binding.get('invoke_action', ''),
            'config': config_store,
            'state': 'ACTIVE',
            'depends_on': [],
            'created_result': item['provider_result'],
        })

    record = {
        'name': project_name,
        'template_id': template_id,
        'template_name': tmpl.get('name', tmpl.get('label', template_id)),
        'project_dir': project_dir,
        'status': 'active',
        'bindings': binding_records,
    }
    saved = storage.add_project(record)

    env_hints = {}
    for b in binding_records:
        pk = b.get('actual_provider_key') or b.get('provider_key')
        if not pk or b.get('binding_type') == 'action':
            continue
        try:
            result = invoke_provider(
                pk, 'env_hints',
                config=b.get('config', {}),
                project_dir=project_dir,
            )
            if result and result.success and result.data:
                env_hints[pk] = result.data
        except Exception as _e:
            logger.debug('env_hints 获取失败 %s: %s', pk, _e)

    try:
        from . import snapshot
        snapshot.write_snapshot(project_dir, record, env_hints=env_hints)
    except Exception as _e:
        logger.warning('快照写入失败: %s', _e)

    _progress(task, progress=100, stage='完成')
    _log(logs, task, '[done] 项目 "%s" 创建成功 (id=%s)' % (project_name, saved.get('id')))

    return {
        'success': True,
        'project_name': project_name,
        'project_dir': project_dir,
        'project_id': saved.get('id', ''),
        'errors': errors,
        'logs': logs,
        'created_items': successfully_created,
        'action_results': action_results,
    }


def execute_project_deletion(project_id: str, remove_dir: bool = False) -> dict:
    project = storage.get_project(project_id)
    if not project:
        return {'success': False, 'error': '项目不存在: %s' % project_id, 'logs': []}

    logs = []
    errors = []
    bindings = project.get('bindings', []) or []
    project_name = project.get('name', '')
    project_dir = project.get('project_dir', '')

    provider_bindings = [b for b in bindings if b.get('provider_key') and b.get('binding_type') != 'action']
    provider_bindings.sort(key=lambda b: len(b.get('depends_on', [])), reverse=True)

    for b in provider_bindings:
        pk = b.get('actual_provider_key') or b.get('provider_key')
        label = b.get('label') or pk
        try:
            invoke_provider(
                pk, 'delete',
                config=b.get('config', {}),
                project_name=project_name,
                project_dir=project_dir,
            )
            logs.append('[delete] %s 已删除' % label)
        except Exception as e:
            logger.warning('删除 %s 失败: %s', label, e)
            logs.append('[delete-warn] %s 删除失败: %s' % (label, e))
            errors.append('%s: %s' % (label, e))

    if remove_dir and project_dir:
        import os as _os
        import shutil as _shutil
        if _os.path.isdir(project_dir):
            try:
                _shutil.rmtree(project_dir)
                logs.append('[delete] 项目目录已删除: %s' % project_dir)
            except Exception as e:
                logs.append('[delete-warn] 项目目录删除失败: %s' % e)
                errors.append('项目目录: %s' % e)

    storage.remove_project(project_id)
    logs.append('[done] 项目 "%s" 已从管理中移除' % project_name)

    return {
        'success': True,
        'errors': errors,
        'logs': logs,
    }


def execute_project_deletion(project_id: str, remove_dir: bool = False) -> dict:
    project = storage.get_project(project_id)
    if not project:
        return {'success': False, 'error': '项目不存在: %s' % project_id, 'logs': []}

    logs = []
    errors = []
    bindings = project.get('bindings', []) or []
    project_name = project.get('name', '')
    project_dir = project.get('project_dir', '')

    provider_bindings = [b for b in bindings if b.get('provider_key') and b.get('binding_type') != 'action']
    provider_bindings.sort(key=lambda b: len(b.get('depends_on', [])), reverse=True)

    for b in provider_bindings:
        pk = b.get('actual_provider_key') or b.get('provider_key')
        label = b.get('label') or pk
        try:
            invoke_provider(
                pk, 'delete',
                config=b.get('config', {}),
                project_name=project_name,
                project_dir=project_dir,
            )
            logs.append('[delete] %s 已删除' % label)
        except Exception as e:
            logger.warning('删除 %s 失败: %s', label, e)
            logs.append('[delete-warn] %s 删除失败: %s' % (label, e))
            errors.append('%s: %s' % (label, e))

    if remove_dir and project_dir:
        import os as _os
        import shutil as _shutil
        if _os.path.isdir(project_dir):
            try:
                _shutil.rmtree(project_dir)
                logs.append('[delete] 项目目录已删除: %s' % project_dir)
            except Exception as e:
                logs.append('[delete-warn] 项目目录删除失败: %s' % e)
                errors.append('项目目录: %s' % e)

    storage.remove_project(project_id)
    logs.append('[done] 项目 "%s" 已从管理中移除' % project_name)

    return {
        'success': True,
        'errors': errors,
        'logs': logs,
    }


def soft_delete_project(project_id: str) -> dict:
    project = storage.get_project(project_id)
    if not project:
        return {'ok': False, 'error': '项目不存在: %s' % project_id}
    ok = storage.move_to_trash(project_id)
    if ok:
        return {'ok': True, 'project_name': project.get('name', '')}
    return {'ok': False, 'error': '移入回收站失败'}


def _permanent_delete_worker(task, project_id, remove_dir):
    project = storage._trash_store.get(project_id)
    if not project:
        core_tasks.append_log(task, f'❌ 回收站中未找到项目: {project_id}')
        core_tasks.set_progress(task, status=core_tasks.STATUS_ERROR)
        return

    bindings = project.get('bindings', []) or []
    project_name = project.get('name', '')
    project_dir = project.get('project_dir', '')

    provider_bindings = [b for b in bindings if b.get('provider_key') and b.get('binding_type') != 'action']
    provider_bindings.sort(key=lambda b: len(b.get('depends_on', [])), reverse=True)

    total = len(provider_bindings)
    errors = []
    item_results = []
    stopped = False

    for idx, b in enumerate(provider_bindings):
        if stopped:
            break
        pk = b.get('actual_provider_key') or b.get('provider_key')
        label = b.get('label') or pk
        core_tasks.set_progress(task, progress=int((idx / max(total, 1)) * 75),
                                stage=f'正在清理 {label}...')
        core_tasks.append_log(task, f'>> 正在清理 {label}...')

        rr = invoke_provider(
            pk, 'delete',
            config=b.get('config', {}),
            project_name=project_name,
            project_dir=project_dir,
        )

        is_skipped = (rr.success and isinstance(rr.data, dict) and rr.data.get('skipped'))

        if is_skipped:
            core_tasks.append_log(task, f'  {label} 已清理（幂等跳过）')
            item_results.append({'label': label, 'provider_key': pk, 'success': True, 'skipped': True, 'detail': ''})
            continue

        if rr.success:
            core_tasks.append_log(task, f'  {label} 清理完成')
            item_results.append({'label': label, 'provider_key': pk, 'success': True, 'skipped': False, 'detail': ''})
            continue

        if rr.error_code in (ProviderResult.E_NOT_FOUND, ProviderResult.E_APP_DISABLED):
            core_tasks.append_log(task, f'  {label} {rr.error_msg}，跳过')
            item_results.append({'label': label, 'provider_key': pk, 'success': True, 'skipped': True, 'detail': rr.error_msg or 'provider not found'})
            continue

        msg = rr.error_msg or rr.error_code or '未知错误'
        core_tasks.append_log(task, f'  ❌ {label} 清理失败: {msg}')
        errors.append(f'{label}: {msg}')
        item_results.append({'label': label, 'provider_key': pk, 'success': False, 'skipped': False, 'detail': msg})
        core_tasks.append_log(task, f'  ⛔ 遇错即停，终止后续清理')
        stopped = True

    if not stopped:
        core_tasks.set_progress(task, progress=80, stage='处理项目目录...')
        if remove_dir and project_dir:
            if os.path.isdir(project_dir):
                core_tasks.append_log(task, f'>> 正在删除项目目录 {project_dir}...')
                try:
                    shutil.rmtree(project_dir)
                    core_tasks.append_log(task, f'  项目目录已删除')
                    item_results.append({'label': '项目目录', 'provider_key': '', 'success': True, 'skipped': False, 'detail': ''})
                except Exception as e:
                    msg = str(e)
                    errors.append(f'项目目录: {msg}')
                    core_tasks.append_log(task, f'  ❌ 项目目录删除失败: {msg}')
                    item_results.append({'label': '项目目录', 'provider_key': '', 'success': False, 'skipped': False, 'detail': msg})
                    core_tasks.append_log(task, f'  ⛔ 遇错即停，终止后续清理')
                    stopped = True
            else:
                core_tasks.append_log(task, f'  项目目录不存在，跳过')
                item_results.append({'label': '项目目录', 'provider_key': '', 'success': True, 'skipped': True, 'detail': ''})
        else:
            core_tasks.append_log(task, '  保留项目目录')

    if not errors:
        core_tasks.set_progress(task, progress=95, stage='清理回收站备份...')
        storage.delete_permanent(project_id)
        core_tasks.append_log(task, '>> 回收站备份已移除')
        core_tasks.append_log(task, f'✅ 项目 "{project_name}" 已彻底删除')
        core_tasks.set_progress(task, progress=100, status=core_tasks.STATUS_SUCCESS)
    else:
        core_tasks.append_log(task, f'⚠ 存在 {len(errors)} 个错误，回收站备份已保留，可修正后重试')
        core_tasks.set_progress(task, progress=100, status=core_tasks.STATUS_ERROR)

    result = {
        'success': len(errors) == 0,
        'has_errors': len(errors) > 0,
        'errors': errors,
        'items': item_results,
        'project_name': project_name,
        'project_dir': project_dir,
        'total_components': total,
        'stopped_at': len(item_results) if stopped else 0,
    }
    core_tasks.set_result(task, result)


def execute_permanent_deletion_async(project_id: str, remove_dir: bool = False) -> dict:
    project = storage._trash_store.get(project_id)
    if not project:
        return {'ok': False, 'error': '回收站中未找到此项目'}

    task = core_tasks.create_task(
        'project_permanent_delete',
        meta={
            'project_id': project_id,
            'project_name': project.get('name', ''),
            'remove_dir': remove_dir,
        },
        title='彻底删除项目: %s' % project.get('name', ''),
    )

    core_tasks.run_in_thread(
        task,
        _permanent_delete_worker,
        project_id=project_id,
        remove_dir=remove_dir,
    )

    return {'ok': True, 'task_id': task['task_id']}