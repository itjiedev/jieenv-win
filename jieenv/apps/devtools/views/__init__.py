import logging
import shutil

from django.http import JsonResponse
from django.urls import reverse, NoReverseMatch
from django.views.decorators.http import require_POST
from django.views.generic.base import TemplateView

from jiecore.views import AppMixin

from apps.devtools import config
from apps.devtools import tool_registry

logger = logging.getLogger(__name__)


class DevtoolsMixin(AppMixin):
    parent_menu_id = ''
    menu_id = 'devtools'


class IndexView(DevtoolsMixin, TemplateView):
    template_name = 'devtools/index.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        all_categories = self._build_categories()

        active_cat = self.request.GET.get('cat', '')
        if active_cat:
            display_tools = next(
                (c['tools'] for c in all_categories if c['key'] == active_cat), []
            )
        else:
            display_tools = []
            for c in all_categories:
                display_tools.extend(c['tools'])

        context['page_title'] = config.PAGE_TITLE
        context['categories'] = all_categories
        context['active_cat'] = active_cat
        context['display_tools'] = display_tools
        context['status_installed_text'] = config.STATUS_INSTALLED_TEXT
        context['status_not_installed_text'] = config.STATUS_NOT_INSTALLED_TEXT
        return context

    def _build_categories(self):
        groups = tool_registry.grouped_by_order(
            list(config.CATEGORY_MAP.keys()), config.DEFAULT_CATEGORY_KEY
        )
        categories = []
        for group in groups:
            key = group['key']
            tools = [self._build_tool(t) for t in group['tools']]
            tools = [t for t in tools if t]
            categories.append({
                'key': key,
                'label': config.CATEGORY_MAP.get(key, config.DEFAULT_CATEGORY_LABEL),
                'tools': tools,
                'has_tools': bool(tools),
            })
        return categories

    def _build_tool(self, info):
        tool_id = info.get('tool_id')
        if not tool_id:
            return None

        install_info = self._get_install_info(info)
        installed = bool(install_info)

        version_str = ''
        display_name = ''
        if install_info:
            display_name = install_info.get('DisplayName') or ''
            version_str = (
                install_info.get('version')
                or install_info.get('Version')
                or install_info.get('DisplayVersion')
                or install_info.get('productVersion')
                or ''
            )
        return {
            'tool_id': tool_id,
            'label': info.get('label', tool_id),
            'icon_static': info.get('icon_static', ''),
            'installed': installed,
            'version_str': version_str,
            'official_url': info.get('official_url', ''),
            'install_url': self._reverse(
                'install_viewname', info.get('install_viewname')
            ),
            'detail_url': self._reverse(
                'detail_viewname', info.get('detail_viewname')
            ),
            'start_url': self._reverse(
                'start_viewname', info.get('start_viewname')
            ),
        }

    @staticmethod
    def _get_install_info(info):
        check_fn = info.get('check_installed') or info.get('install_check_fn')
        if not callable(check_fn):
            return None
        try:
            result = check_fn()
            if result and isinstance(result, dict):
                return result
            return None
        except Exception as e:
            logger.warning(
                'devtools 检测工具安装状态失败 [%s]: %s', info.get('tool_id'), e
            )
            return None

    @staticmethod
    def _reverse(viewname_field, viewname):
        if not viewname:
            return ''
        try:
            return reverse(viewname)
        except NoReverseMatch as e:
            logger.warning(
                'devtools 反向解析失败 [%s=%s]: %s', viewname_field, viewname, e
            )
            return ''


@require_POST
def api_cleanup_downloads(request):
    dl_dir = config.DOWNLOAD_DIR
    files_count = 0
    total_bytes = 0
    if dl_dir.exists():
        for p in dl_dir.rglob('*'):
            if p.is_file():
                try:
                    total_bytes += p.stat().st_size
                    files_count += 1
                except OSError:
                    pass
        try:
            shutil.rmtree(dl_dir)
        except OSError as e:
            return JsonResponse({'success': False, 'msg': f'删除失败: {e}'})
    dl_dir.mkdir(parents=True, exist_ok=True)
    size_mb = round(total_bytes / 1024 / 1024, 2)
    return JsonResponse({
        'success': True,
        'files': files_count,
        'size_mb': size_mb,
    })