from typing import Any
import logging

from django.views.generic.base import TemplateView
from django.http import JsonResponse

from jiecore.views import AppMixin
from jiecore.ext_hook_registry import call_hooks
from jiecore.op_log import get_recent_operations
from jiecore.app_manager import read_app_meta_from_disk

logger = logging.getLogger(__name__)


def api_home_projects_stats(request):
    """首页项目概览 API：统计分布 + 最近创建项目（最多 5 个）。

    返回格式：
    {
        'total': 12,
        'ok': 9,
        'degraded': 2,
        'missing': 1,
        'recent': [
            {
                'id': 'abc123',
                'name': '电商平台',
                'template': 'python_main',
                'health': 'OK',
                'created_at': '2026-10-22 14:30:00',
            },
            ...
        ]
    }
    """
    try:
        from jieenv.apps.project_mgmt import storage as proj_storage
        from jieenv.apps.project_mgmt import binding_health as proj_health
    except ImportError:
        return JsonResponse({'total': 0, 'ok': 0, 'degraded': 0, 'missing': 0, 'recent': []})

    projects = proj_storage.list_projects()
    total = len(projects)
    ok = degraded = missing = 0
    for p in projects:
        h = proj_health.check_binding_health(p)
        state = h.get('overall', '')
        if state == proj_health.STATE_OK:
            ok += 1
        elif state == proj_health.STATE_DEGRADED:
            degraded += 1
        elif state == proj_health.STATE_MISSING:
            missing += 1

    recent_sorted = sorted(
        projects,
        key=lambda p: p.get('created_at', ''),
        reverse=True,
    )[:5]
    recent = []
    for p in recent_sorted:
        h = proj_health.check_binding_health(p)
        recent.append({
            'id': p.get('id', ''),
            'name': p.get('name', ''),
            'project_dir': p.get('project_dir', ''),
            'health': h.get('overall', proj_health.STATE_OK),
            'created_at': p.get('created_at', ''),
        })

    return JsonResponse({
        'total': total,
        'ok': ok,
        'degraded': degraded,
        'missing': missing,
        'recent': recent,
    })


class HomeMixin(AppMixin):
    menu_id = 'home'
    parent_menu_id = ''


class HomeView(HomeMixin, TemplateView):
    """首页：环境总览 + 最近操作"""
    template_name = 'home/home.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = '总览'
        # ── 最近操作 ──
        context['recent_operations'] = get_recent_operations(limit=5)
        return context


def api_home_environments(request):
    """异步加载首页环境总览 API。

    行数据契约：每个环境由钩子自行提供展示字段（fields 为 {label, value}
    字典列表，label 可选），前端只做通用渲染；未注册钩子的环境不出现，
    分类下无任何环境时整个分类不返回。

    返回格式：
    {
        'categories': [
            {
                'menu_id': 'runtimes',
                'name': '语言&运行时',
                'icon': 'fa-solid fa-code',
                'envs': [
                    {
                        'name': 'Python_installer',
                        'icon': 'fa-brands fa-python',
                        'icon_color': '#3776ab',   # 可选，图标颜色
                        'manage_url': '/python/',
                        'manage_label': '管理',    # 可选，默认"管理"
                        'fields': [
                            {'value': '3个版本'},
                            {'label': 'PATH默认', 'value': '3.12.10'},
                        ],
                    },
                ],
            },
        ]
    }
    """
    # 读取分类骨架
    meta = read_app_meta_from_disk('mainapp')
    menus = meta.get('menus', [])
    category_menus = [
        m for m in menus
        if not m.get('parent_id') and m.get('menu_id') != 'home'
    ]

    # 收集各应用注册的环境数据
    env_results = call_hooks('mainapp.home.environments')

    # 按 menu_id 分组
    env_by_menu: dict[str, list] = {}
    for env in env_results:
        if not env or not isinstance(env, dict):
            continue
        mid = env.get('menu_id', '')
        if mid:
            env_by_menu.setdefault(mid, []).append(env)

    # 构造分类列表（只返回有内容的分类）
    categories = []
    for menu in category_menus:
        mid = menu.get('menu_id', '')
        envs = env_by_menu.get(mid, [])
        if not envs:
            continue
        categories.append({
            'menu_id': mid,
            'name': menu.get('name', ''),
            'icon': menu.get('icon', ''),
            'envs': envs,
        })

    return JsonResponse({'categories': categories})