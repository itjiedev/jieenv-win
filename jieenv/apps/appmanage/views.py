from typing import Any
import logging

from django.urls import reverse
from django.views.generic.list import ListView
from django.views.generic.base import TemplateView
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.utils.decorators import method_decorator
from django.contrib import messages
from django.shortcuts import redirect

from jiecore.app_manager import (
    read_app_meta_from_disk, read_changelog_from_disk, AppManager,
    is_platform_fixed_app,
)
from jiecore.views import AppMixin

logger = logging.getLogger(__name__)


class AppManageMixin(AppMixin):
    parent_menu_id = 'system'
    menu_id = 'appmanage'


def _block_platform_fixed(request, app_name):
    """骨架应用访问拦截：flash 提示 + redirect 回应用列表"""
    messages.warning(request,
                     f'「{app_name}」属于平台骨架应用，请通过顶部"平台升级"统一更新。')
    return redirect('appmanage:list')


class AppListView(AppManageMixin, ListView):
    template_name = 'appmanage/list.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = '应用管理'
        return context

    def get_queryset(self):
        all_apps = {
            'jiecore': read_app_meta_from_disk('jiecore'),
        }
        all_apps.update(AppManager.get_all_apps())
        all_apps = {k: v for k, v in all_apps.items() if not is_platform_fixed_app(k)}
        return all_apps


class AppDetailView(AppManageMixin, TemplateView):
    template_name = 'appmanage/detail.html'

    def get(self, request, *args, **kwargs):
        app_name = kwargs.get('app_name', '')
        if is_platform_fixed_app(app_name):
            return _block_platform_fixed(request, app_name)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = '应用详情'
        context['breadcrumb'] = [
            {
                'title': '应用管理',
                'href': reverse('appmanage:list'),
                'active': False
            },
            {
                'title': '应用详情',
                'href': None,
                'active': True
            },
        ]
        app_name = kwargs['app_name']
        if app_name == 'jiecore':
            context['app'] = dict(read_app_meta_from_disk('jiecore'))
        else:
            context['app'] = AppManager.get_app(self.kwargs.get('app_name'))
        app = context['app']
        if not app.get('changelog'):
            app['changelog'] = read_changelog_from_disk(app_name)
        return context


@method_decorator(xframe_options_sameorigin, name='dispatch')
class AppInstallView(AppManageMixin, TemplateView):
    """安装应用页：表单由前端 AJAX 提交到 jiecore:api_app_install

    - 普通访问（无参数）: 渲染 appmanage/install.html（继承 layout_main，带完整框架）
    - ?embed=1: 渲染 appmanage/install_embed.html（继承 layout_embed，纯 iframe 壳，无侧栏/顶栏）
    """
    template_name = 'appmanage/install.html'

    def get_template_names(self):
        if self.request.GET.get('embed') == '1':
            return ['appmanage/install_embed.html']
        return ['appmanage/install.html']

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = '安装应用'
        context['embed_mode'] = self.request.GET.get('embed') == '1'
        context['app_name'] = self.request.GET.get('app_name', '')
        context['next'] = self.request.GET.get('next', '')
        return context


class AppUpgradeView(AppManageMixin, TemplateView):
    """升级应用页：表单由前端 AJAX 提交到 jiecore:api_app_action"""
    template_name = 'appmanage/upgrade.html'

    def get(self, request, *args, **kwargs):
        app_name = kwargs.get('app_name', '')
        if is_platform_fixed_app(app_name):
            return _block_platform_fixed(request, app_name)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['page_title'] = '升级应用'
        context['breadcrumb'] = [
            {
                'title': '应用管理',
                'href': reverse('appmanage:list'),
                'active': False
            },
            {
                'title': '应用详情',
                'href': reverse('appmanage:detail', kwargs={'app_name': self.kwargs.get('app_name')}),
                'active': False
            },
            {
                'title': '升级应用',
                'href': None,
                'active': True
            },
        ]
        app_name = self.kwargs.get('app_name')
        # 核心应用(jiecore)不在注册表中，需从磁盘读取 meta
        if app_name == 'jiecore':
            context['app'] = dict(read_app_meta_from_disk(app_name))
        else:
            context['app'] = AppManager.get_app(app_name)
        return context


class AppConfirmActionView(AppManageMixin, TemplateView):
    """操作确认页：确认后由前端 AJAX 提交到 jiecore:api_app_action
    通过 URL 的 action 参数区分操作类型（enable/disable/uninstall/delete）。
    """
    template_name = 'appmanage/confirm.html'

    # action -> (操作动词, 确认提示, 成功后跳转目标)
    CONFIRM_CONFIGS = {
        'enable': ('启用', '启用后应用将加入运行状态，菜单将显示，原数据可继续使用。', 'detail'),
        'disable': ('禁用', '禁用后不会显示应用的菜单，但是数据仍然保留，您可以再次启用。', 'detail'),
        'uninstall': (
            '卸载',
            '卸载后将回滚数据库迁移、删除数据，删除数据表，保留应用文件，可原地重新安装，但原数据将丢失。',
            'list',
        ),
        'delete': (
            '彻底删除',
            '此操作将回滚数据库迁移、完全删除数据，删除应用数据表、并完全删除应用文件，不可恢复！',
            'list',
        ),
    }

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        app_name = kwargs['app_name']
        action = kwargs['action']
        verb, message, redirect_to = self.CONFIRM_CONFIGS[action]
        get_app = AppManager.get_app(app_name)
        display_name = get_app.get('display_name', app_name) if get_app else app_name
        context['page_title'] = '操作确认'
        context['confirm_action'] = f'{verb} "{display_name}" 应用'
        context['confirm_message'] = message
        context['app_name'] = app_name
        context['api_action'] = action
        next_url = self.request.GET.get('next', '')
        context['redirect_url'] = (
            next_url
            if next_url else (
                reverse('appmanage:detail', kwargs={'app_name': app_name})
                if redirect_to == 'detail' else reverse('appmanage:list')
            )
        )
        context['breadcrumb'] = [
            {
                'title': '应用管理',
                'href': reverse('appmanage:list'),
                'active': False
            },
            {
                'title': '应用详情',
                'href': reverse('appmanage:detail', kwargs={'app_name': app_name}),
                'active': False
            },
            {
                'title': '操作确认',
                'href': None,
                'active': True
            },
        ]
        return context