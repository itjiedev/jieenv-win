import json
import logging
import os
import random
import re

from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView, View

from jiecore.views import AppMixin
from jiecore.system_ops import open_terminal
from jiecore.provider_catalog import (
    get_provider, invoke_provider, find_provider_app,
)
from jiecore import tasks as core_tasks
from apps.mainapp.config import WORKSPACE_ROOT
from . import storage, binding_health, templates as project_templates, snapshot, scanner
from . import action_registry
from .engine import execute_project_creation, execute_project_deletion, soft_delete_project, execute_permanent_deletion_async

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    if not name:
        return ''
    ascii_parts = re.findall(r'[A-Za-z0-9]+', name)
    if ascii_parts:
        slug = '_'.join(ascii_parts).lower()
    else:
        slug = 'project_' + ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=4))
    slug = re.sub(r'_+', '_', slug).strip('_')
    return slug


class ProjectMgmtMixin(AppMixin):
    menu_id = 'project_mgmt'
    parent_menu_id = ''


class IndexView(ProjectMgmtMixin, TemplateView):
    template_name = 'project_mgmt/index.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        keyword = (self.request.GET.get('q') or '').strip()

        ready = storage.list_ready_projects()

        if keyword:
            kw = keyword.lower()
            ready = [p for p in ready if kw in (p.get('name') or '').lower()]

        context.update({
            'page_title': '项目管理',
            'projects': ready,
            'q': keyword,
        })
        return context


class ImportLocalView(ProjectMgmtMixin, TemplateView):
    template_name = 'project_mgmt/import_local.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '导入本地目录'
        context['breadcrumb'] = [
            {'title': '项目管理', 'href': reverse('project_mgmt:index'), 'active': False},
            {'title': '导入本地目录', 'href': None, 'active': True},
        ]
        return context


class CreateWizardView(ProjectMgmtMixin, TemplateView):
    template_name = 'project_mgmt/create.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'page_title': '创建项目',
            'templates': project_templates.get_all_templates(),
            'categories': project_templates.get_template_categories(),
            'workspace_root': str(WORKSPACE_ROOT),
            'default_base_dir': str(WORKSPACE_ROOT),
        })
        return context

    def post(self, request):
        template_id = request.POST.get('template_id', '').strip()
        project_name = request.POST.get('project_name', '').strip()
        base_dir = request.POST.get('base_dir', '').strip()
        dir_slug = request.POST.get('dir_slug', '').strip()
        project_dir = request.POST.get('project_dir', '').strip()
        configs_raw = request.POST.get('configs', '{}')

        errors = {}

        if not template_id:
            errors['template_id'] = '请选择一个项目模板'
        elif project_templates.get_template(template_id) is None:
            errors['template_id'] = '模板不存在'

        if not project_name:
            errors['project_name'] = '请输入项目名称'
        elif len(project_name) > 100:
            errors['project_name'] = '项目名称过长（最大 100 字符）'

        if not base_dir:
            errors['base_dir'] = '请选择或输入基础目录'

        if not dir_slug:
            errors['dir_slug'] = '目录名不能为空'
        elif not re.match(r'^[A-Za-z0-9_]+$', dir_slug):
            errors['dir_slug'] = '目录名只能包含字母、数字和下划线'

        if not project_dir:
            project_dir = os.path.join(base_dir, dir_slug)

        if os.path.exists(project_dir):
            errors['project_dir'] = '目录已存在，请更换目录名或基础目录'

        try:
            configs = json.loads(configs_raw)
        except Exception:
            errors['configs'] = '组件配置数据格式错误'
            configs = {}

        if errors:
            ctx = self.get_context_data()
            ctx.update({
                'wizard': {
                    'template_id': template_id,
                    'project_name': project_name,
                    'base_dir': base_dir,
                    'dir_slug': dir_slug,
                    'project_dir': project_dir,
                    'configs': configs,
                },
                'errors': errors,
            })
            return render(request, self.template_name, ctx)

        result = execute_project_creation(
            project_name=project_name,
            template_id=template_id,
            project_dir=project_dir,
            configs=configs,
        )

        if result.get('success'):
            return HttpResponseRedirect(reverse('project_mgmt:index'))

        ctx = self.get_context_data()
        ctx.update({
            'wizard': {
                'template_id': template_id,
                'project_name': project_name,
                'base_dir': base_dir,
                'dir_slug': dir_slug,
                'project_dir': project_dir,
                'configs': configs,
            },
            'execution_failed': True,
            'error_logs': result.get('logs', []),
            'error_errors': result.get('errors', []),
        })
        return render(request, self.template_name, ctx)


@require_POST
def api_create_project(request):
    template_id = request.POST.get('template_id', '').strip()
    project_name = request.POST.get('project_name', '').strip()
    base_dir = request.POST.get('base_dir', '').strip()
    dir_slug = request.POST.get('dir_slug', '').strip()
    project_dir = request.POST.get('project_dir', '').strip()
    configs_raw = request.POST.get('configs', '{}')

    errors = {}

    if not template_id:
        errors['template_id'] = '请选择一个项目模板'
    elif project_templates.get_template(template_id) is None:
        errors['template_id'] = '模板不存在'

    if not project_name:
        errors['project_name'] = '请输入项目名称'
    elif len(project_name) > 100:
        errors['project_name'] = '项目名称过长（最大 100 字符）'

    if not base_dir:
        errors['base_dir'] = '请选择或输入基础目录'

    if not dir_slug:
        errors['dir_slug'] = '目录名不能为空'
    elif not re.match(r'^[A-Za-z0-9_]+$', dir_slug):
        errors['dir_slug'] = '目录名只能包含字母、数字和下划线'

    if not project_dir:
        project_dir = os.path.join(base_dir, dir_slug)

    if os.path.exists(project_dir):
        errors['project_dir'] = '目录已存在，请更换目录名或基础目录'

    try:
        configs = json.loads(configs_raw)
    except Exception:
        errors['configs'] = '组件配置数据格式错误'
        configs = {}

    if errors:
        return JsonResponse({'ok': False, 'form_errors': errors})

    task = core_tasks.create_task(
        'project_create',
        meta={'project_name': project_name, 'template_id': template_id},
        title='创建项目: %s' % project_name,
    )

    core_tasks.run_in_thread(
        task,
        _project_create_worker,
        project_name=project_name,
        template_id=template_id,
        project_dir=project_dir,
        configs=configs,
    )

    return JsonResponse({'ok': True, 'task_id': task['task_id']})


def _project_create_worker(task, project_name, template_id, project_dir, configs):
    result = execute_project_creation(
        project_name=project_name,
        template_id=template_id,
        project_dir=project_dir,
        configs=configs,
        task=task,
    )
    core_tasks.set_result(task, result)
    if not result.get('success'):
        core_tasks.set_progress(task, status=core_tasks.STATUS_ERROR)


def _render_action_body(request, action_binding: dict) -> str:
    type_id = action_binding.get('type', '')
    action_def = action_registry.get_action_def(type_id)
    if not action_def:
        logger.warning('action_registry: unknown action type %s, skip render', type_id)
        return ''

    params = action_binding.get('params', {}) or {}
    packages = params.get('packages', [])
    if isinstance(packages, str):
        packages = [p.strip() for p in packages.splitlines() if p.strip()]

    ctx = {
        'action_binding': action_binding,
        'action_def': action_def,
        'packages': packages,
        'packages_newline': '\n'.join(packages),
        'packages_comma': ','.join(packages),
        'label': action_binding.get('label', action_def.get('label', type_id)),
        'icon': action_def.get('icon', 'fa-solid fa-puzzle-piece'),
        'description': action_def.get('description', ''),
    }

    tmpl_name = action_def.get('render_template')
    if not tmpl_name:
        return ''

    try:
        return render(request, tmpl_name, ctx).content.decode('utf-8')
    except Exception as e:
        logger.warning('render action body %s failed: %s', type_id, e)
        return ''


class BindingFormApiView(ProjectMgmtMixin, View):

    def get(self, request):
        template_id = request.GET.get('template_id', '').strip()
        project_dir = request.GET.get('project_dir', '').strip()
        project_name = request.GET.get('project_name', '').strip()
        dir_slug = request.GET.get('dir_slug', '').strip()
        provider_key = request.GET.get('provider_key', '').strip()
        import_mode = request.GET.get('import_mode', '').strip()
        dir_path = request.GET.get('dir_path', '').strip()

        if import_mode:
            return self._get_import_bindings(request, import_mode, dir_path, project_dir, project_name, provider_key)

        tmpl = project_templates.get_template(template_id)
        if tmpl is None:
            return JsonResponse({'ok': False, 'error': '模板不存在: %s' % template_id})

        bindings = tmpl.get('bindings', []) or []

        return self._render_bindings(request, bindings, project_dir, project_name, dir_slug, provider_key)

    def _get_import_bindings(self, request, import_mode, dir_path, project_dir, project_name, provider_key):
        if not dir_path:
            return JsonResponse({'ok': False, 'error': '缺少 dir_path 参数'})

        scan = scanner.scan_directory(dir_path)
        if not scan.get('dir_exists'):
            return JsonResponse({'ok': False, 'error': '目录不存在: %s' % dir_path})

        snap = scan.get('snapshot')
        detected_runtimes = scan.get('detected_runtimes', [])

        use_snapshot = bool(snap) and import_mode != 'component_import'

        if use_snapshot:
            bindings, skipped = _build_bindings_from_snapshot(snap)
        elif detected_runtimes:
            bindings, skipped = _build_bindings_from_scan(detected_runtimes, dir_path)
        else:
            return JsonResponse({'ok': False, 'error': '未识别到可导入的组件'})

        effective_project_dir = os.path.abspath(dir_path)

        return self._render_bindings(request, bindings, effective_project_dir, project_name, '', provider_key,
                                     import_defaults=self._extract_import_defaults(bindings))

    def _extract_import_defaults(self, bindings):
        defaults = {}
        for b in bindings:
            if b.get('binding_type') == 'action':
                continue
            pk = b.get('actual_provider_key') or b.get('provider_key', '')
            if not pk:
                continue
            defaults[pk] = b.get('config', {}) or {}
        return defaults

    def _render_bindings(self, request, bindings, project_dir, project_name, dir_slug, provider_key,
                         import_defaults=None):
        from collections import defaultdict as _ddict
        import_mode = import_defaults is not None

        action_map = _ddict(list)
        for b in bindings:
            if b.get('type') and not b.get('provider_key'):
                target_pk = b.get('target_provider_key') or '__standalone__'
                action_map[target_pk].append(b)

        html_parts = []
        for b in bindings:
            binding_type = b.get('binding_type', 'provider')
            b_pk = b.get('provider_key', '')

            if import_mode:
                if binding_type != 'provider':
                    extra_actions_html = []
                    if b_pk:
                        for ab in action_map.get(b_pk, []):
                            ah = _render_action_body(request, ab)
                            if ah:
                                extra_actions_html.append(ah)
                    html_parts.append(self._render_action_binding_html(b, extra_actions_html))
                    continue
                pk = b_pk
            else:
                if not b.get('provider_key'):
                    continue
                pk = b['provider_key']

            if provider_key and pk != provider_key:
                continue

            provider = get_provider(pk)

            from jiecore.provider_catalog import find_provider_app
            from jiecore.app_manager import AppManager as _AM
            owner_app = find_provider_app(pk)
            app_all = _AM.get_all_apps()
            is_disabled = bool(owner_app and owner_app in app_all and provider is None)
            is_missing = (provider is None) and not is_disabled

            create_entry = {}
            if provider:
                lifecycle = provider.get('lifecycle') or {}
                create_entry = lifecycle.get('create') or {}

            config_fields = list(create_entry.get('config_schema', []) or [])

            prefs = {}
            if not import_mode:
                prefs = b.get('preferences', {}) or {}

            has_version_pref = bool(prefs.get('default_version'))

            import_cfg = (import_defaults or {}).get(pk, {}) if import_mode else {}

            fields_matched_default = False

            has_any_options = False

            for field in config_fields:
                fkey = field.get('key', '')
                ftype = field.get('type', '')
                uiw = field.get('ui_widget', '')

                if ftype == 'select' and 'options_from' in field:
                    cap = field['options_from'].get('capability', '')
                    if cap:
                        try:
                            rr = invoke_provider(pk, cap)
                            options = rr.data if rr.success and isinstance(rr.data, list) else []
                            if options:
                                has_any_options = True
                        except Exception:
                            options = []
                        field['options'] = options

                        if options:
                            from .engine import _extract_ver, _pick_by_version_tier

                            default_ver = prefs.get('default_version')
                            if default_ver:
                                tier_picked = _pick_by_version_tier(options, default_ver)
                                if tier_picked:
                                    field['default'] = tier_picked.get('value')
                                    actual_ver = _extract_ver(tier_picked)
                                    if actual_ver:
                                        field['default_version'] = actual_ver
                                    fields_matched_default = True
                                    continue

                            picked = None
                            key = field.get('key', '')
                            if key in prefs:
                                pref = prefs[key]
                                if isinstance(pref, str):
                                    picked = next((o for o in options if o.get('value') == pref), None)
                                elif isinstance(pref, dict):
                                    if pref.get('value'):
                                        picked = next((o for o in options if o.get('value') == pref['value']), None)
                                    if not picked and pref.get('match'):
                                        m = pref['match']
                                        if m == 'default':
                                            picked = next((o for o in options if o.get('default')), None)
                                        else:
                                            picked = next((o for o in options if m in str(o.get('label', ''))), None)
                            if not picked and isinstance(field.get('default'), dict) and field['default'].get('match') == 'default':
                                picked = next((o for o in options if o.get('default')), None)
                            if not picked:
                                picked = next((o for o in options if o.get('default')), None)
                            if picked:
                                field['default'] = picked.get('value')
                                ver = _extract_ver(picked)
                                if ver:
                                    field['default_version'] = ver

                if field.get('ui_widget') == 'dir_with_base':
                    full = field.get('default', '') or ''
                    if full and os.path.isabs(str(full)):
                        field['dir_base'] = os.path.dirname(str(full))
                        field['dir_sub'] = os.path.basename(str(full))
                    else:
                        field['dir_base'] = project_dir or ''
                        field['dir_sub'] = os.path.basename(str(full)) if full else '.venv'
                        if full:
                            field['default'] = os.path.join(project_dir, str(full)) if project_dir else str(full)

                default_from = field.get('default_from')
                if default_from and not field.get('default'):
                    if default_from == 'project_name':
                        field['default'] = project_name
                    elif default_from == 'dir_slug':
                        field['default'] = dir_slug
                    elif default_from == 'project_dir':
                        field['default'] = project_dir

                if import_cfg and fkey in import_cfg:
                    import_val = import_cfg[fkey]

                    if uiw == 'dir_with_base':
                        raw = str(import_val or '')
                        if raw and os.path.isabs(raw):
                            field['default'] = raw
                            field['dir_base'] = os.path.dirname(raw)
                            field['dir_sub'] = os.path.basename(raw)
                        else:
                            field['default'] = os.path.join(project_dir, raw) if raw else ''
                            field['dir_base'] = project_dir
                            field['dir_sub'] = raw if raw else '.venv'
                    elif ftype == 'checkbox':
                        field['default'] = bool(import_val)
                    elif ftype == 'select':
                        field['default'] = import_val
                        options = field.get('options') or []
                        if import_val and options:
                            found = any(str(o.get('value')) == str(import_val) for o in options)
                            if not found:
                                options.insert(0, {
                                    'value': import_val,
                                    'label': f'已检测: {import_val}',
                                    'default': True,
                                })
                                field['options'] = options
                                has_any_options = True
                    else:
                        field['default'] = import_val

            extra_actions_html = []
            for ab in action_map.get(pk, []):
                ah = _render_action_body(request, ab)
                if ah:
                    extra_actions_html.append(ah)

            binding_ctx = dict(b)
            if import_mode:
                provider_icon = provider.get('icon') if provider else None
                if provider_icon and not binding_ctx.get('icon'):
                    binding_ctx['icon'] = provider_icon
            else:
                if provider is not None and has_version_pref and fields_matched_default:
                    binding_ctx['template_default_hint'] = f"模板默认: {prefs.get('default_version')}"

            no_options = (
                provider is not None
                and config_fields
                and any(f.get('type') == 'select' and 'options_from' in f for f in config_fields)
                and not has_any_options
            )

            disabled_contributors = []
            missing_contributors = []
            degrade_error = ''

            prefix = (provider or {}).get('contributor_name_prefix') if provider else None
            if no_options and prefix:
                from jiecore.provider_catalog import _PROVIDER_APP_INDEX, _ensure_bootstrap
                from jiecore.app_manager import AppManager
                _ensure_bootstrap()

                all_apps = AppManager.get_all_apps()
                for prov_key, app_name in _PROVIDER_APP_INDEX.items():
                    prov_parts = prov_key.split('.')
                    prov_name = prov_parts[1] if len(prov_parts) >= 2 else prov_key
                    if not prov_name.startswith(prefix):
                        continue
                    if app_name == (owner_app or ''):
                        continue
                    info = all_apps.get(app_name)
                    label = (info.get('display_name') if isinstance(info, dict) else None) or app_name
                    entry = {'app_name': app_name, 'label': label}
                    if info is None:
                        missing_contributors.append(entry)
                    elif isinstance(info, dict) and not info.get('is_enabled', True):
                        disabled_contributors.append(entry)

                friendly = provider.get('label') or provider.get('provider_id') or pk
                if disabled_contributors and not missing_contributors:
                    degrade_error = f'没有启用的 {friendly}，请启用后重试'
                elif missing_contributors and not disabled_contributors:
                    degrade_error = f'需要先安装 {friendly} 扩展'
                elif disabled_contributors and missing_contributors:
                    degrade_error = f'需要先安装或启用 {friendly} 扩展'
                else:
                    degrade_error = f'没有可用的 {friendly} 环境'

            ctx = {
                'binding': binding_ctx,
                'installed': provider is not None,
                'is_disabled': is_disabled,
                'is_missing': is_missing,
                'no_options': no_options,
                'degrade_error': degrade_error,
                'disabled_contributors': disabled_contributors,
                'missing_contributors': missing_contributors,
                'owner_app': owner_app or '',
                'provider': provider,
                'config_fields': config_fields,
                'is_extra': False,
                'extra_actions_html': extra_actions_html,
            }

            if provider is not None:
                fe = provider.get('frontend_extras') or {}
                if fe:
                    ctx['extra_embed'] = fe.get('create_embed', '')
                    ctx['extra_css'] = fe.get('css', [])
                    ctx['extra_js'] = fe.get('js', [])
                    cs = fe.get('context_service')
                    if cs:
                        rr = invoke_provider(pk, '_frontend_context')
                        if rr.success and isinstance(rr.data, dict):
                            ctx.update(rr.data)

            html_parts.append(render(request, 'project_mgmt/create_binding_item.html', ctx).content.decode('utf-8'))

        if not html_parts:
            html_parts.append(
                '<div class="text-center text-muted py-3">'
                '<i class="fa-solid fa-circle-check"></i> 此项目无组件配置'
                '</div>'
            )

        return JsonResponse({'ok': True, 'html': '\n'.join(html_parts)})

    def _render_action_binding_html(self, b, extra_actions_html):
        from django.utils.html import escape
        action_type = b.get('action_type', b.get('type', ''))
        label = escape(b.get('label', action_type))
        provider_key = escape(b.get('provider_key', ''))
        state = b.get('state', 'ACTIVE')
        state_badge = '<span class="badge badge-success"><i class="fa-solid fa-check"></i> 已加载</span>' if state == 'ACTIVE' else '<span class="badge badge-danger">缺失</span>'
        return (
            '<div class="binding-form card mb-3" data-provider-key="%s">'
            '<div class="card-header py-2 d-flex align-items-center justify-content-between">'
            '<div class="d-flex align-items-center">'
            '<i class="fa-solid fa-code text-primary mr-2"></i>'
            '<span class="font-weight-bold">%s</span>'
            '<code class="ml-2 text-muted small">%s</code>'
            '</div>'
            '<div>%s</div>'
            '</div>'
            '<div class="card-body py-2">%s</div>'
            '</div>'
        ) % (provider_key, label, escape(action_type), state_badge, (extra_actions_html[0] if extra_actions_html else '<div class="text-muted small">关联动作（只读）</div>'))


class DeleteProjectView(ProjectMgmtMixin, View):

    def post(self, request, project_id):
        remove_dir = request.POST.get('remove_dir', '0') in ('1', 'true', 'on')
        result = execute_project_deletion(project_id, remove_dir=remove_dir)
        if result.get('success'):
            return JsonResponse({'ok': True, 'message': '删除成功'})
        return JsonResponse({'ok': False, 'error': result.get('error', '删除失败'),
                             'logs': result.get('logs', [])})


@require_POST
def api_provider_capability(request, provider_key, capability):
    """通用 provider capability 调用端点。

    POST body 参数作为 config 传给 invoke_provider，返回 ProviderResult。
    provider 可以是 lifecycle 或 capabilities 里声明的任意 capability。
    """
    from jiecore.provider_catalog import invoke_provider as _invoke

    config = {}
    for k, v in request.POST.items():
        config[k] = v

    rr = _invoke(provider_key, capability, config=config)
    if rr.success:
        return JsonResponse({'ok': True, 'data': rr.data})
    return JsonResponse({'ok': False, 'error_msg': rr.error_msg})


@require_POST
def api_check_project_dir(request):
    project_dir = request.POST.get('project_dir', '').strip()
    base_dir = request.POST.get('base_dir', '').strip()
    dir_slug = request.POST.get('dir_slug', '').strip()

    if not project_dir:
        if base_dir and dir_slug:
            project_dir = os.path.join(base_dir, dir_slug)
        else:
            return JsonResponse({'ok': True, 'exists': False, 'reason': 'empty'})

    exists = os.path.exists(project_dir)
    is_dir = os.path.isdir(project_dir) if exists else False

    return JsonResponse({
        'ok': True,
        'exists': exists,
        'is_dir': is_dir,
        'path': project_dir,
    })


@require_POST
def api_open_dir(request):
    path = request.POST.get('path', '').strip()
    if not path:
        return JsonResponse({'ok': False, 'error': '未提供路径'})
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return JsonResponse({'ok': False, 'error': '路径不存在: %s' % abs_path})
    try:
        os.startfile(abs_path)
        return JsonResponse({'ok': True})
    except Exception as e:
        logger.warning('open_dir failed: %s', e)
        return JsonResponse({'ok': False, 'error': str(e)})


@require_POST
def api_open_terminal(request):
    project_id = request.POST.get('project_id', '').strip()
    if not project_id:
        return JsonResponse({'ok': False, 'error': '未提供项目ID'})

    project = storage.get_project(project_id)
    if not project:
        return JsonResponse({'ok': False, 'error': '项目不存在'})

    project_dir = project.get('work_dir') or project.get('project_dir')
    project_name = project.get('name', '')
    if not project_dir or not os.path.isdir(project_dir):
        return JsonResponse({'ok': False, 'error': '项目目录不存在: %s' % project_dir})

    env_hints = _storage_collect_env_hints(project, project_dir, project_name)
    if not env_hints:
        env_hints = _snapshot_read_env_hints(project_dir)

    subprocess_env, activate_cmd = _merge_to_subprocess_env(env_hints)

    result = open_terminal(
        work_dir=project_dir,
        subprocess_env=subprocess_env,
        activate_cmd=activate_cmd or None,
    )
    if result.get('success'):
        return JsonResponse({'ok': True})
    return JsonResponse({'ok': False, 'error': result.get('message', '打开终端失败')})


def _storage_collect_env_hints(project, project_dir, project_name):
    collected = []
    for b in project.get('bindings', []):
        if b.get('config', {}).get('_action_type'):
            continue
        pk = b.get('actual_provider_key') or b.get('provider_key', '')
        if not pk:
            continue
        result = invoke_provider(
            pk, 'env_hints',
            config=b.get('config', {}),
            project_dir=project_dir,
            project_name=project_name,
        )
        if result.ok and result.data:
            collected.append(result.data)
    return collected


def _snapshot_read_env_hints(project_dir):
    snapshot_path = os.path.join(project_dir, '.jieenv', 'project.json')
    if not os.path.isfile(snapshot_path):
        return None
    try:
        with open(snapshot_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        hints = data.get('env_hints')
        if hints and isinstance(hints, dict):
            return [hints]
    except Exception as e:
        logger.debug('snapshot env_hints read failed: %s', e)
    return None


def _merge_to_subprocess_env(env_hints_list):
    merged_env = {}
    merged_path_prepend = []
    activate_cmd = ''

    for hints in (env_hints_list or []):
        if not isinstance(hints, dict):
            continue
        for k, v in (hints.get('env') or {}).items():
            if k != 'PATH' and v is not None:
                merged_env[k] = str(v)
        for p in (hints.get('PATH_prepend') or []):
            if p and p not in merged_path_prepend:
                merged_path_prepend.append(p)
        if not activate_cmd and hints.get('activate_cmd'):
            activate_cmd = hints['activate_cmd']

    base_env = os.environ.copy()
    for k, v in merged_env.items():
        base_env[k] = v

    if merged_path_prepend:
        existing = base_env.get('PATH', '')
        prepend_str = os.pathsep.join(merged_path_prepend)
        base_env['PATH'] = prepend_str + os.pathsep + existing if existing else prepend_str

    return base_env, activate_cmd


class CreateResultView(ProjectMgmtMixin, TemplateView):
    template_name = 'project_mgmt/create_result.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        task_id = self.kwargs.get('task_id', '')
        task = core_tasks.get_task(task_id)

        if not task:
            ctx['task_found'] = False
            return ctx

        ctx['task_found'] = True
        ctx['task_id'] = task_id
        ctx['task_status'] = task.get('status', '')
        ctx['is_running'] = ctx['task_status'] in ('pending', 'running')

        if ctx['is_running']:
            return ctx

        result = task.get('result', {}) or {}
        ctx['success'] = bool(result.get('success'))
        ctx['project_name'] = result.get('project_name', '')
        ctx['project_dir'] = result.get('project_dir', '')
        ctx['project_id'] = result.get('project_id', '')
        ctx['errors'] = result.get('errors', []) or []
        ctx['created_items'] = result.get('created_items', []) or []
        ctx['action_results'] = result.get('action_results', []) or []

        for ar in ctx['action_results']:
            if '_pkg_status' in ar:
                ar['pkg_status'] = ar.get('_pkg_status')

        has_warnings = False
        for ar in ctx['action_results']:
            pkg_status = ar.get('pkg_status', {}) or {}
            if pkg_status.get('skipped_packages') or pkg_status.get('fallback_used'):
                has_warnings = True
                break
        ctx['has_warnings'] = has_warnings

        item_status = {}
        for item in ctx['created_items']:
            ok = bool(item.get('provider_result')) and item.get('provider_result', {}).get('success', True) is not False
            item_status[item.get('template_pk', '')] = ok
        ctx['item_status_map'] = item_status
        ctx['page_title'] = '项目创建结果'
        return ctx


class DeleteConfirmView(ProjectMgmtMixin, TemplateView):
    template_name = 'project_mgmt/delete_confirm.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        project_id = self.kwargs.get('project_id', '')
        project = storage.get_project(project_id)
        if not project:
            ctx['project_found'] = False
            ctx['page_title'] = '删除项目'
            return ctx

        bindings = project.get('bindings', []) or []
        provider_bindings = [b for b in bindings if b.get('provider_key') and not (b.get('config') or {}).get('_action_type')]
        provider_bindings.sort(key=lambda b: len(b.get('depends_on', [])), reverse=True)

        preview_items = []
        for b in provider_bindings:
            pk = b.get('actual_provider_key') or b.get('provider_key')
            provider = get_provider(pk)
            icon = 'fa-solid fa-puzzle-piece'
            label = b.get('label') or pk
            if provider:
                cat = provider.get('category', '')
                if cat == 'runtime':
                    icon = 'fa-solid fa-code'
                elif cat == 'db':
                    icon = 'fa-solid fa-database'
                elif cat == 'cache':
                    icon = 'fa-solid fa-memory'
            preview_items.append({
                'label': label,
                'provider_key': pk,
                'icon': icon,
                'category': provider.get('category', '') if provider else '',
            })

        full_dir = (project.get('project_dir') or '').strip()
        ref_name = os.path.basename(full_dir) if full_dir else project.get('name', '')
        ref_type = '项目目录名' if full_dir else '项目名称'

        ctx.update({
            'project_found': True,
            'project': project,
            'preview_items': preview_items,
            'total_components': len(preview_items),
            'page_title': f'删除项目: {project.get("name", "")}',
            'ref_name': ref_name,
            'ref_type': ref_type,
        })
        return ctx


class PermanentDeleteConfirmView(ProjectMgmtMixin, TemplateView):
    template_name = 'project_mgmt/permanent_delete_confirm.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        project_id = self.kwargs.get('project_id', '')
        project = storage._trash_store.get(project_id)
        if not project:
            ctx['project_found'] = False
            ctx['page_title'] = '彻底删除项目'
            return ctx

        bindings = project.get('bindings', []) or []
        provider_bindings = [b for b in bindings if b.get('provider_key') and not (b.get('config') or {}).get('_action_type')]
        provider_bindings.sort(key=lambda b: len(b.get('depends_on', [])), reverse=True)

        preview_items = []
        for b in provider_bindings:
            pk = b.get('actual_provider_key') or b.get('provider_key')
            provider = get_provider(pk)
            icon = 'fa-solid fa-puzzle-piece'
            label = b.get('label') or pk
            if provider:
                cat = provider.get('category', '')
                if cat == 'runtime':
                    icon = 'fa-solid fa-code'
                elif cat == 'db':
                    icon = 'fa-solid fa-database'
                elif cat == 'cache':
                    icon = 'fa-solid fa-memory'
            preview_items.append({
                'label': label,
                'provider_key': pk,
                'icon': icon,
                'category': provider.get('category', '') if provider else '',
            })

        full_dir = (project.get('project_dir') or '').strip()
        ref_name = os.path.basename(full_dir) if full_dir else project.get('name', '')
        ref_type = '项目目录名' if full_dir else '项目名称'

        ctx.update({
            'project_found': True,
            'project': project,
            'preview_items': preview_items,
            'total_components': len(preview_items),
            'page_title': f'彻底删除: {project.get("name", "")}',
            'ref_name': ref_name,
            'ref_type': ref_type,
        })
        return ctx


@require_POST
def api_delete_execute(request):
    project_id = request.POST.get('project_id', '').strip()

    project = storage.get_project(project_id)
    if not project:
        return JsonResponse({'ok': False, 'error': '项目不存在'})

    result = soft_delete_project(project_id)
    return JsonResponse(result)


def api_delete_status(request):
    task_id = request.GET.get('task_id', '').strip()
    after = request.GET.get('after', '0')
    if not task_id:
        return JsonResponse({'success': False, 'error': '缺少 task_id'})
    task = core_tasks.get_task(task_id)
    if not task:
        return JsonResponse({'success': False, 'error': '任务不存在'})
    return JsonResponse(core_tasks.task_payload(task, after=after))


class TrashIndexView(ProjectMgmtMixin, TemplateView):
    template_name = 'project_mgmt/trash.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        trash_items = storage.list_trash()
        for item in trash_items:
            full_dir = (item.get('project_dir') or '').strip()
            item['ref_name'] = os.path.basename(full_dir) if full_dir else item.get('name', '')
            item['ref_type'] = '项目目录名' if full_dir else '项目名称'
        ctx.update({
            'page_title': '回收站',
            'trash_items': trash_items,
        })
        return ctx


@require_POST
def api_trash_restore(request):
    project_id = request.POST.get('project_id', '').strip()
    if storage.restore_from_trash(project_id):
        return JsonResponse({'ok': True})
    return JsonResponse({'ok': False, 'error': '恢复失败'})


@require_POST
def api_trash_delete_permanent(request):
    project_id = request.POST.get('project_id', '').strip()
    confirmed_ref = request.POST.get('confirmed_ref', '').strip()
    remove_dir = request.POST.get('remove_dir', '0') in ('1', 'true', 'on')

    project = storage._trash_store.get(project_id)
    if not project:
        return JsonResponse({'ok': False, 'error': '回收站中未找到此项目'})

    full_dir = (project.get('project_dir') or '').strip()
    expected = os.path.basename(full_dir) if full_dir else project.get('name', '')
    if confirmed_ref and confirmed_ref != expected:
        return JsonResponse({'ok': False, 'error': '目录名/项目名称不匹配，请重新输入确认'})

    result = execute_permanent_deletion_async(
        project_id=project_id,
        remove_dir=remove_dir,
    )
    return JsonResponse(result)


@require_POST
def api_trash_remove_record(request):
    project_id = request.POST.get('project_id', '').strip()
    if not project_id:
        return JsonResponse({'ok': False, 'error': '缺少 project_id'})

    project = storage._trash_store.get(project_id)
    if not project:
        return JsonResponse({'ok': False, 'error': '回收站中未找到此项目'})

    ok = storage.delete_permanent(project_id)
    if ok:
        return JsonResponse({'ok': True, 'project_name': project.get('name', '')})
    return JsonResponse({'ok': False, 'error': '删除记录失败'})


def api_health_batch(request):
    ids_raw = request.POST.get('project_ids', '[]')
    try:
        import json as _json
        ids = _json.loads(ids_raw)
    except Exception:
        ids = [x.strip() for x in ids_raw.split(',') if x.strip()]

    result = {}
    for pid in ids:
        p = storage.get_project(pid)
        if not p:
            continue
        result[pid] = binding_health.check_binding_health(p)

    return JsonResponse({'ok': True, 'data': result})


class ProjectDetailView(ProjectMgmtMixin, TemplateView):
    template_name = 'project_mgmt/project_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        project_id = self.kwargs.get('project_id', '')
        project = storage.get_project(project_id)

        if not project:
            ctx.update({
                'project_found': False,
                'page_title': '项目不存在',
            })
            return ctx

        project_dir = (project.get('project_dir') or '').strip()

        snap = None
        if project_dir:
            snap = snapshot.read_snapshot(project_dir)

        bindings_render = []
        if snap and snap.get('bindings'):
            snap_bindings = snap['bindings']

            for b in snap_bindings:
                pk = b.get('provider_key', '')
                bt = b.get('binding_type', 'provider')
                label = b.get('label') or pk
                cfg = b.get('config_snapshot', {}) or {}

                provider_def = get_provider(pk)
                owner_app = find_provider_app(pk) if not provider_def else ''
                cat = provider_def.get('category', '') if provider_def else ''
                icon = _category_icon(cat, bt)

                summary = _config_summary(cfg, pk, bt)

                bindings_render.append({
                    'binding_type': bt,
                    'provider_key': pk,
                    'label': label,
                    'icon': icon,
                    'category': cat,
                    'action_type': b.get('action_type', ''),
                    'invoke_action': b.get('invoke_action', ''),
                    'state': None,
                    'state_label': binding_health.health_label('DETECTING') if bt == 'provider' else '',
                    'state_class': 'badge-secondary' if bt == 'provider' else '',
                    'summary': summary,
                    'config': cfg,
                    'depends_on': b.get('depends_on', []),
                    'missing_keys': b.get('config_missing_keys', []),
                    'is_disabled': bool(owner_app),
                    'owner_app': owner_app,
                })
        else:
            for b in project.get('bindings') or []:
                pk = b.get('provider_key') or b.get('actual_provider_key', '')
                bt = b.get('binding_type', 'provider')
                label = b.get('label') or pk
                cfg = b.get('config', {}) or {}

                provider_def = get_provider(pk)
                owner_app = find_provider_app(pk) if not provider_def else ''
                cat = provider_def.get('category', '') if provider_def else ''
                icon = _category_icon(cat, bt)

                bindings_render.append({
                    'binding_type': bt,
                    'provider_key': pk,
                    'label': label,
                    'icon': icon,
                    'category': cat,
                    'action_type': b.get('action_type', ''),
                    'invoke_action': b.get('invoke_action', ''),
                    'state': None,
                    'state_label': binding_health.health_label('DETECTING') if bt == 'provider' else '',
                    'state_class': 'badge-secondary' if bt == 'provider' else '',
                    'summary': _config_summary(cfg, pk, bt),
                    'config': cfg,
                    'depends_on': b.get('depends_on', []),
                    'missing_keys': [],
                    'is_disabled': bool(owner_app),
                    'owner_app': owner_app,
                })

        provider_bindings = [b for b in bindings_render if b['binding_type'] == 'provider']
        action_bindings = [b for b in bindings_render if b['binding_type'] == 'action']

        # ── 拓扑分组：按 provider category 值硬编码分组 ──
        # 贡献应用在 hooks.py register_provider 时声明的 category 值即分组 id
        # 想合并就写同值，想细分就写不同值，程序不做映射
        TOPO_GROUP_INFO = {
            'runtime': {'label': '运行时',   'icon': 'fa-solid fa-code',         'sort': 100},
            'runtime_item': {'label': '运行时辅助', 'icon': 'fa-solid fa-wrench',       'sort': 200},
            'db':      {'label': '数据库',   'icon': 'fa-solid fa-database',     'sort': 300},
            'cache':   {'label': '缓存',     'icon': 'fa-solid fa-memory',       'sort': 350},
            'mq':      {'label': '消息队列', 'icon': 'fa-solid fa-bullhorn',     'sort': 360},
            'other':   {'label': '其他',     'icon': 'fa-solid fa-puzzle-piece', 'sort': 999},
        }

        _grouped = {}
        for b in provider_bindings:
            gid = b.get('category') or 'other'
            if gid not in TOPO_GROUP_INFO:
                gid = 'other'
            _grouped.setdefault(gid, []).append(b)

        grouped_bindings = []
        for gid in sorted(_grouped.keys(), key=lambda g: TOPO_GROUP_INFO.get(g, {}).get('sort', 999)):
            info = TOPO_GROUP_INFO.get(gid, {})
            grouped_bindings.append({
                'group_id': gid,
                'label': info.get('label', gid),
                'icon': info.get('icon', 'fa-solid fa-folder'),
                'bindings': _grouped[gid],
            })

        env_hints = (snap or {}).get('env_hints', {}) or {}

        dir_entries = []
        if project_dir and os.path.isdir(project_dir):
            try:
                all_entries = sorted(os.listdir(project_dir))
                for entry in all_entries[:50]:
                    full = os.path.join(project_dir, entry)
                    is_dir = os.path.isdir(full)
                    dir_entries.append({'name': entry, 'is_dir': is_dir})
            except OSError:
                pass

        snap_exists = bool(snap)
        snap_meta = {}
        if snap:
            snap_meta = {
                'schema_version': snap.get('schema_version', '?'),
                'snapshot_version': snap.get('snapshot_version', '?'),
                'created_at': snap.get('created_at', ''),
                'has_missing_keys': any(b.get('config_missing_keys') for b in snap.get('bindings', [])),
            }

        template_label = project.get('template_name', '')
        if not template_label:
            tid = project.get('template_id', '')
            if tid:
                tmpl = project_templates.get_template(tid)
                if tmpl:
                    template_label = tmpl.get('name', tmpl.get('label', ''))

        overall = 'UNKNOWN'
        overall_class = 'badge-secondary'
        overall_label = '探测中...'

        ctx.update({
            'project_found': True,
            'project': project,
            'project_dir': project_dir,
            'template_label': template_label,
            'overall_state': overall,
            'overall_class': overall_class,
            'overall_label': overall_label,
            'provider_bindings': provider_bindings,
            'action_bindings': action_bindings,
            'grouped_bindings': grouped_bindings,
            'topo_group_info': TOPO_GROUP_INFO,
            'env_hints': env_hints,
            'dir_entries': dir_entries,
            'snap_exists': snap_exists,
            'snap_meta': snap_meta,
            'snap_raw': json.dumps(snap, indent=2, ensure_ascii=False) if snap else '',
            'page_title': f'项目详情: {project.get("name", "")}',
        })
        return ctx


def _category_icon(category, binding_type='provider'):
    if binding_type == 'action':
        return 'fa-solid fa-bolt'
    return {
        'runtime': 'fa-solid fa-code',
        'db': 'fa-solid fa-database',
        'cache': 'fa-solid fa-memory',
        'web': 'fa-solid fa-globe',
        'queue': 'fa-solid fa-list',
    }.get(category, 'fa-solid fa-puzzle-piece')


def _config_summary_from_cfg(cfg):
    """provider 不可用时的兜底：直接从已保存的 config 里读键值对。"""
    SKIP_SUFFIXES = ('_version', '_contributor_app')
    SENSITIVE_KEYS = {'password', 'passwd', 'pwd', 'secret', 'token', 'api_key'}
    result = []
    for key, val in cfg.items():
        if key.startswith('_'):
            continue
        if any(key.endswith(s) for s in SKIP_SUFFIXES):
            continue
        if val is None or val == '':
            continue
        if isinstance(val, bool):
            val = '✅ 开启' if val else '❌ 关闭'
        elif isinstance(val, (dict, list)):
            val = str(val)
        display = '••••••' if key.lower() in SENSITIVE_KEYS else str(val)
        result.append({'label': key, 'value': display})
    return result


def _config_summary(cfg, provider_key, binding_type='provider'):
    """通用配置摘要：基于 provider 的 lifecycle.create.config_schema 动态渲染。
    返回 [{label, value}] 结构化列表，供模板渲染带标签的配置摘要。
    """
    if not cfg:
        return []

    if binding_type == 'action':
        atype = cfg.get('_action_type', '')
        result = []
        if atype == 'lang_packages':
            pkgs = cfg.get('packages', '')
            if isinstance(pkgs, list):
                pkgs = ', '.join(pkgs)
            if pkgs:
                result.append({'label': 'pip', 'value': pkgs})
        elif atype == 'python_module':
            module = cfg.get('module', '')
            args = cfg.get('args', '')
            if module:
                result.append({'label': '模块', 'value': f'python -m {module}'})
            if args:
                result.append({'label': '参数', 'value': args})
        if not result and atype:
            result.append({'label': '动作', 'value': atype})
        return result

    from jiecore.provider_catalog import get_provider as _gp

    p = _gp(provider_key)
    if not p:
        return _config_summary_from_cfg(cfg)

    lifecycle = p.get('lifecycle') or {}
    create_entry = lifecycle.get('create') or {}
    schema = create_entry.get('config_schema', []) or []

    if not schema:
        return _config_summary_from_cfg(cfg)

    result = []
    raw_fields = set()

    for field in schema:
        key = field.get('key', '')
        label = field.get('label', key)
        ftype = field.get('type', 'text')
        sensitive = field.get('sensitive', False)
        uiw = field.get('ui_widget', '')

        raw = cfg.get(key)
        if raw is None or raw == '' or raw is False:
            continue

        raw_fields.add(key)

        if sensitive or ftype == 'password':
            value = '••••••'
        elif ftype == 'checkbox':
            value = '✅ 开启' if raw else '❌ 关闭'
        elif ftype == 'select':
            options_from = field.get('options_from', {})
            cap = (options_from or {}).get('capability', '') if isinstance(options_from, dict) else ''
            picked_label = None
            if cap:
                try:
                    rr = invoke_provider(provider_key, cap)
                    opts = rr.data if rr.success and isinstance(rr.data, list) else []
                    for opt in opts:
                        opt_val = opt.get('value', opt) if isinstance(opt, dict) else opt
                        opt_label = opt.get('label', opt_val) if isinstance(opt, dict) else opt_val
                        opt_version = opt.get('version', '') if isinstance(opt, dict) else ''
                        if str(opt_val) == str(raw):
                            if opt_version:
                                picked_label = opt_version
                            else:
                                picked_label = opt_label
                            break
                except Exception:
                    pass
            value = picked_label if picked_label else str(raw)
        elif uiw == 'dir_with_base':
            value = str(raw)
        else:
            value = str(raw)

        result.append({'label': label, 'value': value})

    return result


def api_snapshot_json(request, project_id):
    project = storage.get_project(project_id)
    if not project:
        return JsonResponse({'ok': False, 'error': '项目不存在'})
    project_dir = (project.get('project_dir') or '').strip()
    if not project_dir:
        return JsonResponse({'ok': False, 'error': '项目无目录'})
    snap = snapshot.read_snapshot(project_dir)
    if not snap:
        return JsonResponse({'ok': False, 'error': '快照不存在'})
    return JsonResponse(snap, safe=False)


def api_health_check(request):
    project_id = request.POST.get('project_id', '').strip()
    if not project_id:
        return JsonResponse({'ok': False, 'error': '缺少 project_id'})
    project = storage.get_project(project_id)
    if not project:
        return JsonResponse({'ok': False, 'error': '项目不存在'})
    health = binding_health.check_binding_health(project)
    return JsonResponse({'ok': True, 'data': health})


def api_edit_binding(request):
    """编辑项目单个 binding 的 config。

    POST JSON: {project_id: str, provider_key: str, config: dict}
    只更新对应 binding 的 config 字段，不改动其他 binding。
    """
    import json as _json
    try:
        body = _json.loads(request.body.decode('utf-8'))
    except Exception:
        body = request.POST
    project_id = (body.get('project_id') or '').strip()
    provider_key = (body.get('provider_key') or '').strip()
    new_config = body.get('config') or {}
    if not project_id or not provider_key:
        return JsonResponse({'ok': False, 'error': '缺少 project_id 或 provider_key'})
    project = storage.get_project(project_id)
    if not project:
        return JsonResponse({'ok': False, 'error': '项目不存在'})
    bindings = project.get('bindings') or []
    target = None
    for b in bindings:
        if b.get('provider_key') == provider_key:
            target = b
            break
    if target is None:
        return JsonResponse({'ok': False, 'error': f'未找到 binding: {provider_key}'})
    target['config'] = new_config
    storage.update_project(project_id, bindings=bindings)
    health = binding_health.check_binding_health(project)
    return JsonResponse({'ok': True, 'data': health})


@require_POST
def api_scan_dir(request):
    dir_path = request.POST.get('dir_path', '').strip()

    if not dir_path:
        return JsonResponse({'ok': False, 'error': '请输入目录路径'})

    scan = scanner.scan_directory(dir_path)

    if not scan.get('dir_exists'):
        return JsonResponse({'ok': False, 'error': '目录不存在: %s' % dir_path})

    return JsonResponse({'ok': True, 'scan': scan})


@require_POST
def api_import_local(request):
    dir_path = request.POST.get('dir_path', '').strip()
    project_name = request.POST.get('project_name', '').strip()
    mode = request.POST.get('mode', 'auto').strip()
    configs_raw = request.POST.get('configs', '{}').strip()

    if not dir_path:
        return JsonResponse({'ok': False, 'error': '请输入目录路径'})

    scan = scanner.scan_directory(dir_path)

    if not scan.get('dir_exists'):
        return JsonResponse({'ok': False, 'error': '目录不存在: %s' % dir_path})

    if not scan.get('snapshot') and not scan.get('detected_runtimes'):
        return JsonResponse({
            'ok': False,
            'error': '该目录无快照也未识别到可导入的运行时组件',
            'recommended_action': scan.get('recommended_action'),
        })

    try:
        user_configs = json.loads(configs_raw) if configs_raw else {}
        if not isinstance(user_configs, dict):
            user_configs = {}
    except Exception:
        user_configs = {}

    if not project_name:
        project_name = os.path.basename(os.path.abspath(dir_path))

    bindings = []
    skipped_providers = []
    env_hints = {}

    snap = scan.get('snapshot')
    detected_runtimes = scan.get('detected_runtimes', [])

    use_snapshot = bool(snap) and mode != 'component_import'
    use_components = (not use_snapshot) and bool(detected_runtimes)

    if use_snapshot:
        bindings, skipped_providers = _build_bindings_from_snapshot(snap)
    elif use_components:
        bindings, skipped_providers = _build_bindings_from_scan(detected_runtimes, dir_path)
    else:
        return JsonResponse({'ok': False, 'error': '无法确定导入模式'})

    if user_configs:
        _merge_user_configs_into_bindings(bindings, user_configs)

    _env_hints_from_bindings(bindings, dir_path, env_hints)

    if not bindings and not skipped_providers:
        return JsonResponse({'ok': False, 'error': '导入构建失败：未能组装任何 binding'})

    record = {
        'name': project_name,
        'template_id': 'imported',
        'template_name': '导入本地目录',
        'project_dir': os.path.abspath(dir_path),
        'status': 'imported',
        'bindings': bindings,
    }

    saved = storage.add_project(record)

    try:
        snapshot.write_snapshot(dir_path, record, env_hints=env_hints)
    except Exception as _e:
        logger.warning('import: snapshot write failed: %s', _e)

    return JsonResponse({
        'ok': True,
        'project_id': saved.get('id', ''),
        'project_name': project_name,
        'project_dir': os.path.abspath(dir_path),
        'import_mode': 'snapshot_restore' if use_snapshot else 'component_import',
        'imported_count': sum(1 for b in bindings if b.get('state') == 'ACTIVE'),
        'missing_count': sum(1 for b in bindings if b.get('state') == 'MISSING'),
        'skipped_providers': skipped_providers,
        'recommended_action': scan.get('recommended_action'),
    })


def _merge_user_configs_into_bindings(bindings, user_configs):
    """用户 configs JSON 覆盖 binding 的默认 config。"""
    for b in bindings:
        if b.get('binding_type') == 'action':
            continue
        pk = b.get('actual_provider_key') or b.get('provider_key', '')
        if not pk or pk not in user_configs:
            continue
        raw = user_configs[pk]
        if not isinstance(raw, dict):
            continue
        cur = b.setdefault('config', {})
        for k, v in raw.items():
            if k == '_action_overrides':
                continue
            cur[k] = v


def _build_bindings_from_snapshot(snap: dict) -> tuple:
    bindings = []
    skipped_providers = []

    for b in snap.get('bindings', []):
        pk = b.get('provider_key', '')
        binding_type = b.get('binding_type', 'provider')
        label = b.get('label', '') or pk

        provider_def = get_provider(pk)

        config = b.get('config_snapshot', {}) or {}

        if provider_def:
            bindings.append({
                'provider_key': pk,
                'actual_provider_key': pk,
                'label': label,
                'binding_type': binding_type,
                'action_type': b.get('action_type', '') if binding_type == 'action' else '',
                'invoke_action': b.get('invoke_action', '') if binding_type == 'action' else '',
                'config': config,
                'state': 'ACTIVE',
                'depends_on': [],
            })
        else:
            skipped_providers.append(pk)
            bindings.append({
                'provider_key': pk,
                'actual_provider_key': pk,
                'label': label,
                'binding_type': binding_type,
                'action_type': b.get('action_type', '') if binding_type == 'action' else '',
                'invoke_action': b.get('invoke_action', '') if binding_type == 'action' else '',
                'config': config,
                'state': 'MISSING',
                'depends_on': [],
            })

    return bindings, skipped_providers


def _build_bindings_from_scan(detected_runtimes: list, work_dir: str) -> tuple:
    bindings = []
    skipped_providers = []

    for rt in detected_runtimes:
        pk = rt.get('provider_key', '')
        if not pk:
            continue

        provider_def = get_provider(pk)
        label = _provider_label(pk, rt)

        config = _build_provider_config_from_scan(pk, rt, work_dir)

        if provider_def:
            bindings.append({
                'provider_key': pk,
                'actual_provider_key': pk,
                'label': label,
                'binding_type': 'provider',
                'config': config,
                'state': 'ACTIVE',
                'depends_on': [],
            })

            if rt.get('lang') == 'python' and config.get('use_venv'):
                deps_files = rt.get('deps_files', [])
                if 'requirements.txt' in deps_files:
                    bindings.append({
                        'provider_key': pk,
                        'actual_provider_key': pk,
                        'label': '语言包管理',
                        'binding_type': 'action',
                        'action_type': 'lang_packages',
                        'invoke_action': 'pip_install',
                        'config': {
                            'packages': 'requirements.txt',
                            '_action_type': 'lang_packages',
                        },
                        'state': 'ACTIVE',
                        'depends_on': [],
                    })
        else:
            skipped_providers.append(pk)
            bindings.append({
                'provider_key': pk,
                'actual_provider_key': pk,
                'label': label,
                'binding_type': 'provider',
                'config': config,
                'state': 'MISSING',
                'depends_on': [],
            })

    return bindings, skipped_providers


def _provider_label(pk: str, rt: dict) -> str:
    lang = rt.get('lang', '')
    labels = {
        'python': 'Python 运行时',
        'node': 'Node.js 运行时',
        'go': 'Go 运行时',
        'rust': 'Rust 运行时',
        'java': 'Java 运行时',
    }
    return labels.get(lang, pk)


def _build_provider_config_from_scan(pk: str, rt: dict, work_dir: str) -> dict:
    if pk == 'runtime.python':
        config = {}
        venv_valid = rt.get('venv_valid', False)
        venv_path = rt.get('venv_path')

        if rt.get('venv_found') and venv_valid:
            config['use_venv'] = True
            config['venv_dir'] = venv_path or '.venv'
            base_py = rt.get('base_python', '')
            if base_py and os.path.isfile(base_py):
                config['python_exe'] = base_py
        elif rt.get('needs_action') == 'create_venv':
            config['use_venv'] = True
            config['venv_dir'] = '.venv'
        else:
            config['use_venv'] = False

        return config

    return {}


def _env_hints_from_bindings(bindings: list, project_dir: str, env_hints: dict) -> None:
    for b in bindings:
        if b.get('binding_type') == 'action' or b.get('state') != 'ACTIVE':
            continue
        pk = b.get('actual_provider_key') or b.get('provider_key')
        if not pk:
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
            logger.debug('import: env_hints failed %s: %s', pk, _e)