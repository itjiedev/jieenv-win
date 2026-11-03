import logging

from jiecore.app_manager import AppManager
from jiecore.provider_catalog import invoke_provider, ProviderResult, find_provider_app, get_provider_health

logger = logging.getLogger(__name__)

STATE_OK = 'OK'
STATE_DEGRADED = 'DEGRADED'
STATE_MISSING = 'MISSING'
STATE_DISABLED = 'DISABLED'


def check_binding_health(project: dict) -> dict:
    """检查单个项目的所有 bindings 健康状态。

    实时探测：用 binding.config 的实际参数调 provider 的 verify action。
    返回 {'overall': str, 'bindings': [{provider_key, state, ..., label, summary_html, depends_on, app_name}]}
    """
    from .views import _config_summary, _category_icon

    bindings = project.get('bindings') or []
    results = []
    has_disabled = False
    has_issue = False
    has_missing = False

    for b in bindings:
        pk = b.get('provider_key', '')
        config = b.get('config', {}) or {}
        bt = b.get('binding_type', 'provider')
        label = b.get('label', pk) or pk
        owner_app = ''

        if not pk:
            continue
        if not config:
            continue
        if config.get('_action_type'):
            continue

        rr = invoke_provider(pk, 'verify', config=config)
        if rr.success:
            state = STATE_OK
        elif rr.error_code == ProviderResult.E_APP_DISABLED:
            state = STATE_DISABLED
            has_disabled = True
        elif rr.error_code == ProviderResult.E_NOT_FOUND:
            state = STATE_MISSING
            has_missing = True
        elif rr.error_code == ProviderResult.E_UNHEALTHY:
            state = STATE_DEGRADED
            has_issue = True
        elif rr.error_code == ProviderResult.E_NO_CAPABILITY:
            state = STATE_OK
        else:
            state = STATE_DEGRADED
            has_issue = True

        if state == STATE_OK:
            cache_state = get_provider_health(pk)
            if cache_state == 'DEGRADED':
                state = STATE_DEGRADED
                has_issue = True

        if state == STATE_OK:
            contributor = config.get('_contributor_app')
            if contributor and contributor not in AppManager.get_enabled_apps():
                state = STATE_DISABLED
                has_disabled = True
                owner_app = contributor

        if state == STATE_DISABLED and not owner_app:
            owner_app = find_provider_app(pk)

        config_summary = _config_summary(config, pk, bt)
        summary_html = _render_summary_html(config_summary)

        error_msg = ''
        verify_code = ''
        verify_hint = ''
        remedy = None
        if state != STATE_OK:
            if state == STATE_DISABLED and owner_app:
                display_name = owner_app
                try:
                    app_info = AppManager.get_app(owner_app)
                    if app_info and app_info.get('display_name'):
                        display_name = app_info['display_name']
                except Exception:
                    pass
                error_msg = f'应用 {display_name}({owner_app}) 未启用'
            elif state == STATE_DISABLED:
                error_msg = '相关应用未启用'
            else:
                error_msg = rr.error_msg or ''
            if isinstance(rr.data, dict):
                verify_code = rr.data.get('verify_code', '')
                verify_hint = rr.data.get('verify_hint', '')
                remedy = rr.data.get('remedy')

        if state == STATE_MISSING:
            owner_app = find_provider_app(pk)
            remedy = {'type': 'install', 'label': '安装扩展', 'app_name': owner_app} if owner_app else None
        elif state == STATE_DISABLED:
            remedy = None

        results.append({
            'provider_key': pk,
            'label': label,
            'state': state,
            'depends_on': b.get('depends_on', []),
            'app_name': owner_app,
            'summary_html': summary_html,
            'error_msg': error_msg,
            'verify_code': verify_code,
            'verify_hint': verify_hint,
            'remedy': remedy,
            'config': config,
        })

    if has_missing:
        overall = STATE_MISSING
    elif has_issue or has_disabled:
        overall = STATE_DEGRADED
    else:
        overall = STATE_OK

    return {
        'overall': overall,
        'bindings': results,
    }


def _render_summary_html(summary_items: list) -> str:
    """把 _config_summary 的 [{label, value}] 渲染成详情页用的 HTML 片段。"""
    if not summary_items:
        return '<span class="text-muted">-</span>'
    parts = []
    for item in summary_items:
        lbl = item.get('label', '')
        val = item.get('value', '')
        parts.append(
            '<div><span class="text-muted font-weight-bold">%s:</span> '
            '<span class="text-monospace small">%s</span></div>' % (lbl, val)
        )
    return ''.join(parts)


def health_badge_class(state: str) -> str:
    return {
        STATE_OK: 'badge-success',
        STATE_DEGRADED: 'badge-warning',
        STATE_MISSING: 'badge-danger',
        STATE_DISABLED: 'badge-warning',
    }.get(state, 'badge-secondary')


def health_label(state: str) -> str:
    return {
        STATE_OK: '正常',
        STATE_DEGRADED: '异常',
        STATE_MISSING: '缺失',
        STATE_DISABLED: '异常',
    }.get(state, '探测中...')