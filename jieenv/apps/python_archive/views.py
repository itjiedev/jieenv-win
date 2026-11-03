"""
python_archive 视图层

页面视图:
- IndexView           首页：展示已安装列表
- VersionListView     官网版本列表页（独立页面）
- InstallOptionsView  安装选项页：配置安装参数并执行安装
- DownloadSourceView  下载源设置页：选择/保存默认镜像
- SettingsView        应用设置页

JSON API:
- api_versions            获取版本列表
- api_install_start       创建后台安装任务（下载+解压），返回 task_id 供 JieTaskLog 轮询
- api_uninstall           卸载
- api_add_path            将安装目录加入用户 PATH
- api_rename              修改安装记录名称
- api_open_terminal       以解释器目录为工作目录打开 cmd
- api_uninstall_gui       创建后台卸载任务
- api_clear_cache         清空本应用 tmp 目录
- api_download_sources    列出可选下载源与当前默认
- api_set_download_source 保存默认下载源
- api_settings_save       保存应用配置
- api_del_record          删除安装记录
"""
import os
import re
import shutil
import logging
import pathlib

from django.urls import reverse
from django.http import JsonResponse
from django.views.generic.base import TemplateView
from django.views.decorators.http import require_POST, require_GET

from jiecore import tasks
from jiecore.op_log import log_operation
from apps.python_main.views import PythonMixin

from . import storage
from . import python_install
from . import python_download
from . import env_ops
from . import detect
from .python_versions import get_python_versions
from .config import APP_VERBOSE_NAME, TMP_DIR, DEFAULT_VENV_ROOT

logger = logging.getLogger(__name__)


class ArchiveMixin(PythonMixin):
    """python_archive 视图基类：继承 PythonMixin 自动获得导航上下文"""


# ── 首页 ─────────────────────────────

class IndexView(ArchiveMixin, TemplateView):
    """首页：展示已安装列表"""
    template_name = 'python_archive/index.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = APP_VERBOSE_NAME
        installed = env_ops.annotate_installed(storage.list_installed())
        for rec in installed:
            rec['venv_count'] = storage_venv.count_by_owner(rec.get('id', ''))
        cfg = storage.load_config()
        context['installed'] = installed
        context['default_install_dir'] = cfg.get('install_dir', '')
        context['strict_uninstall_check'] = cfg.get('strict_uninstall_check', False)
        return context


# ── 官网版本列表页 ─────────────────────

class VersionListView(ArchiveMixin, TemplateView):
    """官网版本列表页：独立页面，展示可安装的 Python 版本"""
    template_name = 'python_archive/version_list.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Python 版本列表'
        context['breadcrumb'] = [
            {'title': 'Python 独立版本模式', 'href': reverse('python_archive:index'), 'active': False},
            {'title': 'Python 版本', 'href': None, 'active': True},
        ]
        return context


# ── 安装选项页 ────────────────────────────

class InstallOptionsView(ArchiveMixin, TemplateView):
    """安装选项页：配置安装参数并执行安装"""
    template_name = 'python_archive/install_options.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        version_id = self.request.GET.get('version_id', '')
        entry = storage.get_version_by_id(version_id)
        if not entry:
            from django.shortcuts import redirect
            return redirect('python_archive:version_list')
        context['entry'] = entry
        context['page_title'] = f'安装 {entry.get("display_name", entry.get("version", ""))}'
        context['default_install_dir'] = python_install.get_default_install_dir(version_id)
        context['download_sources'] = python_download.list_sources()
        context['current_source'] = python_download.get_default_source()
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_archive:index'), 'active': False},
            {'title': 'Python可安装版本', 'href': reverse('python_archive:version_list'), 'active': False},
            {'title': f'安装 Python', 'href': None, 'active': True},
        ]
        return context


# ── 下载源设置页 ────────────────────────────

class DownloadSourceView(ArchiveMixin, TemplateView):
    """下载源设置页：选择并保存默认镜像"""
    template_name = 'python_archive/download_source.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = '设置下载源'
        context['download_sources'] = python_download.list_sources()
        context['current_source'] = python_download.get_default_source()
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_archive:index'), 'active': False},
            {'title': 'Python可安装版本', 'href': reverse('python_archive:version_list'), 'active': False},
            {'title': '设置下载源', 'href': None, 'active': True},
        ]
        return context


# ── 应用设置页 ────────────────────────────

class SettingsView(ArchiveMixin, TemplateView):
    """应用设置页：Python 默认安装目录等配置"""
    template_name = 'python_archive/settings.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = f'{APP_VERBOSE_NAME}-设置'
        context['config'] = storage.load_config()
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_archive:index'), 'active': False},
            {'title': '设置', 'href': None, 'active': True},
        ]
        return context

# ── pip 包管理视图（在指定环境下）──────────────────

from apps.python_main.views import PipListViewMixin, PipInstallViewMixin

def _get_record_by_id(record_id):
    """按安装记录 id 取记录，未命中返回 None。"""
    return storage.get_by_id(record_id) if record_id else None

class PipListView(ArchiveMixin, PipListViewMixin):
    """pip 包管理页：管理指定 Installer 环境（按安装记录 id 定位）下的 Python 包。"""

    def get(self, request, *args, **kwargs):
        if not _get_record_by_id(kwargs.get('id', '')):
            from django.shortcuts import redirect
            return redirect('python_archive:index')
        return super().get(request, *args, **kwargs)

    def url_install(self):
        # “安装新包”跳转到完整页面（非弹窗），由本应用提供路由
        return reverse('python_archive:pip_install', args=[self.get_env_id()])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = _get_record_by_id(self.get_env_id()) or {}
        version = record.get('version', '')
        context['page_title'] = f'包管理 - {record.get('name', '')}'
        context['record'] = record
        context['version'] = version
        context['breadcrumb'] = [
            {'title': 'Python 独立版本模式', 'href': reverse('python_archive:index'), 'active': False},
            {'title': f'包管理', 'href': None, 'active': True},
        ]
        return context


class PipInstallView(ArchiveMixin, PipInstallViewMixin):
    """安装新包页（完整页面）：在指定独立版本环境下直接安装与 requirements.txt 导入安装新包。"""

    def get(self, request, *args, **kwargs):
        if not _get_record_by_id(kwargs.get('id', '')):
            from django.shortcuts import redirect
            return redirect('python_archive:index')
        return super().get(request, *args, **kwargs)

    def url_list(self):
        # 返回包列表页
        return reverse('python_archive:pip_list', args=[self.get_env_id()])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = _get_record_by_id(self.get_env_id()) or {}
        version = record.get('version', '')
        context['page_title'] = f'安装新包 - Python {version}'
        context['record'] = record
        context['version'] = version
        context['breadcrumb'] = [
            {'title': 'Python 环境管理', 'href': reverse('python_main:index'), 'active': False},
            {'title': 'Python 独立版本模式', 'href': reverse('python_archive:index'), 'active': False},
            {'title': f'包管理',
             'href': reverse('python_archive:pip_list', args=[self.get_env_id()]), 'active': False},
            {'title': '安装新包', 'href': None, 'active': True},
        ]
        return context



# ── JSON API ─────────────────────────────────────

@require_GET
def api_versions(request):
    """获取版本列表。"""
    refresh = request.GET.get('refresh') == '1'
    result = get_python_versions(refresh=refresh)

    if not result.get('success'):
        return JsonResponse(result)

    versions = result.get('versions', [])

    # ---- 解析筛选参数 ----
    # 1. 架构维度
    arch_param = request.GET.get('arch', '')
    # 处理前端传 arch=all 的情况
    if arch_param in ('all', 'none', ''):
        arches = None  # 不限制架构
    else:
        arches = {a.strip() for a in arch_param.split(',') if a.strip()}

    # 2. 特性维度（叠加条件）
    include_free_threaded = request.GET.get('free_threaded') == '1'
    include_with_tests = request.GET.get('with_tests') == '1'

    def match(v):
        is_standard = not v.get('free_threaded') and not v.get('with_tests')
        is_ft = v.get('free_threaded') is True
        is_wt = v.get('with_tests') is True
        v_arch = str(v.get('arch', '')).strip()

        # ---- 规则1：全部不勾选时，显示所有数据 ----
        if arches is None and not include_free_threaded and not include_with_tests:
            return True

        # ---- 规则2：架构过滤 ----
        # 如果勾选了架构，版本架构必须匹配
        if arches is not None:
            if v_arch not in arches:
                return False

        # ---- 规则3：标准版逻辑 ----
        # 如果是标准版，只有在勾选了架构时才显示（因为用户没勾选架构时只想看特性版）
        if is_standard:
            return arches is not None

        # ---- 规则4：特性叠加逻辑 ----
        # 勾选了 free-threaded 且版本符合
        if include_free_threaded and is_ft:
            return True
        # 勾选了 with tests 且版本符合
        if include_with_tests and is_wt:
            return True

        # 其余情况（如没勾选with tests但版本是wt=True）淘汰
        return False

    result['versions'] = [v for v in versions if match(v)]
    default_source = python_download.get_default_source()

    if default_source and result.get('versions'):
        for entry in result['versions']:
            entry['url'] = python_download.build_zip_url(default_source['url'], entry)

    return JsonResponse(result)


@require_POST
def api_install_start(request):
    """创建后台安装任务（下载+解压），返回 task_id 供 JieTaskLog 轮询。

    POST 参数：
        version_id       版本缓存中的 id
        version          版本号
        name             环境名称（必填）
        install_dir      安装目录
        download_source  本次临时指定的下载源名
    """
    version_id = request.POST.get('version_id', '').strip()
    version = request.POST.get('version', '').strip()
    name = request.POST.get('name', '').strip()
    install_dir = request.POST.get('install_dir', '').strip()
    download_source = request.POST.get('download_source', '').strip()

    if not version_id or not version:
        return JsonResponse({'success': False, 'message': '缺少版本信息'})
    if not name:
        return JsonResponse({'success': False, 'message': '请填写名称'})

    # 前置检查：安装目录是否已存在且有内容
    if install_dir:
        install_path = pathlib.Path(install_dir)
        if install_path.exists() and any(install_path.iterdir()):
            return JsonResponse({'success': False,
                                 'message': f'安装目录已存在且有内容: {install_dir}\n请先删除该目录或选择其他安装路径'})

    # 检查名称是否已存在，如果存在则自动生成唯一名称
    unique_name = storage.generate_unique_name(name)
    if unique_name != name:
        # 名称有变化，记录日志但不阻断
        logger.info('名称 %s 已存在，自动改为 %s', name, unique_name)

    options = {
        'version_id': version_id,
        'version': version,
        'name': unique_name,
        'install_dir': install_dir,
        'download_source': download_source,
    }

    try:
        version_entry = storage.get_version_by_id(version_id) or {}
        for meta_key in ('arch', 'free_threaded', 'with_tests', 'implements_pep'):
            if meta_key in version_entry:
                options[meta_key] = version_entry[meta_key]
    except Exception:
        pass

    task = tasks.create_task('python_archive.install', meta={'version': version, 'name': unique_name},
                             title=f'安装 Python {version}')
    tasks.run_in_thread(task, _install_worker, options)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _install_worker(task, options):
    """后台安装任务体：下载源解析 → 下载 → 解压 → 登记。"""
    version = options['version']

    def log(msg):
        tasks.append_log(task, msg)
        tasks.set_progress(task, stage=msg)

    def progress(pct, stage=None):
        tasks.set_progress(task, progress=pct, stage=stage)

    try:
        progress(5, f'准备安装 Python {version}')
        log(f'安装选项：目录={options.get("install_dir") or "(默认)"}, '
            f'下载源={options.get("download_source") or "默认/自动"}')

        progress(10, '解析下载源')
        result = python_install.run_install(options, on_progress=log)

        progress(100, '安装完成')
        tasks.set_result(task, {
            'success': True,
            'message': f'Python {version} 安装成功',
            'python_exe': result.get('python_exe', ''),
            'install_dir': result.get('install_dir', ''),
            'download_source': result.get('download_source', ''),
        })
        log_operation('python_archive', f'安装 Python {version}',
                      detail=f'安装目录 {result.get("install_dir", "")}，解释器 {result.get("python_exe", "")}')
    except python_install.InstallError as e:
        tasks.set_result(task, {'success': False, 'message': str(e)})
        log_operation('python_archive', f'安装 Python {version}', success=False, detail=str(e))
        raise
    except Exception as e:
        logger.exception('安装 Python %s 失败', version)
        tasks.set_result(task, {'success': False, 'message': f'安装失败: {e}'})
        log_operation('python_archive', f'安装 Python {version}', success=False, detail=str(e))
        raise


@require_POST
def api_uninstall(request):
    """卸载指定 ID 的环境"""
    record_id = request.POST.get('id', '').strip()
    if not record_id:
        return JsonResponse({'success': False, 'message': '缺少记录 ID'})

    record = storage.get_by_id(record_id)
    if not record:
        return JsonResponse({'success': False, 'message': '未找到该安装记录'})

    name = record.get('name', '')
    logs = []

    def on_progress(msg):
        logs.append(msg)

    try:
        result = python_install.run_uninstall(record_id, on_progress=on_progress)
        log_operation('python_archive', f'卸载 {name}',
                      success=result.get('success', False),
                      detail=f'记录 {record_id}：{result.get("message", "")}')
        return JsonResponse({**result, 'logs': logs})
    except Exception as e:
        logger.exception('卸载 %s 失败', name)
        log_operation('python_archive', f'卸载 {name}',
                      success=False, detail=f'记录 {record_id}：{e}')
        return JsonResponse({'success': False, 'message': f'卸载失败: {e}', 'logs': logs})


@require_POST
def api_uninstall_gui(request):
    """创建后台卸载任务，返回 task_id 供 JieTaskLog 轮询。"""
    record_id = request.POST.get('id', '').strip()
    if not record_id:
        return JsonResponse({'success': False, 'message': '缺少记录 ID'})

    record = storage.get_by_id(record_id)
    if not record:
        return JsonResponse({'success': False, 'message': '未找到该安装记录'})

    if record.get('locked'):
        return JsonResponse({
            'success': False, 'blocked': True,
            'message': '该解释器已锁定，请先解锁后再卸载',
        })

    venv_count = storage_venv.count_by_owner(record_id)
    strict = storage.load_config().get('strict_uninstall_check', False)
    if strict and venv_count > 0:
        return JsonResponse({
            'success': False,
            'blocked': True,
            'message': f'该解释器下还有 {venv_count} 个虚拟环境，请先全部删除后再卸载（或关闭「严格卸载检查」开关）',
            'venv_count': venv_count,
        })

    name = record.get('name', '')
    options = {'record_id': record_id}
    task = tasks.create_task('python_archive.uninstall', meta={'record_id': record_id, 'name': name},
                             title=f'卸载 {name}')
    tasks.run_in_thread(task, _uninstall_gui_worker, options)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _uninstall_gui_worker(task, options):
    """后台卸载任务体。"""
    record_id = options['record_id']

    # 获取记录信息用于日志
    record = storage.get_by_id(record_id)
    name = record.get('name', '') if record else record_id

    def log(msg):
        tasks.append_log(task, msg)
        tasks.set_progress(task, stage=msg)

    try:
        log(f'开始卸载 {name}')
        result = python_install.run_uninstall(record_id, on_progress=log)
        if result.get('success'):
            log(result.get('message', '卸载完成'))
            env_ops.refresh_process_path()
            tasks.set_result(task, result)
            log_operation('python_archive', f'卸载 {name}',
                          detail=f'记录 {record_id}（{name}）')
        else:
            log(result.get('message', '卸载失败'))
            tasks.set_result(task, result)
            log_operation('python_archive', f'卸载 {name}',
                          success=False, detail=f'记录 {record_id}（{name}）：{result.get("message", "")}')
    except Exception as e:
        logger.exception('卸载 %s 失败', name)
        msg = f'卸载失败: {e}'
        log(msg)
        tasks.set_result(task, {'success': False, 'message': msg})
        raise


@require_POST
def api_add_path(request):
    """将指定 ID 的安装目录加入用户 PATH"""
    record_id = request.POST.get('id', '').strip()
    if not record_id:
        return JsonResponse({'success': False, 'message': '缺少记录 ID'})
    record = storage.get_by_id(record_id)
    if not record:
        return JsonResponse({'success': False, 'message': '未找到该安装记录'})
    install_dir = record.get('install_dir', '')
    return JsonResponse(env_ops.add_to_path(install_dir))


@require_POST
def api_rename(request):
    """修改指定 ID 的安装记录名称"""
    record_id = request.POST.get('id', '').strip()
    new_name = request.POST.get('name', '').strip()
    if not record_id:
        return JsonResponse({'success': False, 'message': '缺少记录 ID'})
    if not new_name:
        return JsonResponse({'success': False, 'message': '名称不能为空'})
    record = storage.get_by_id(record_id)
    if not record:
        return JsonResponse({'success': False, 'message': '未找到该安装记录'})
    result = storage.rename_installed(record_id, new_name)
    if result is None:
        return JsonResponse({'success': False, 'message': f'名称 "{new_name}" 已存在'})
    log_operation('python_archive', f'重命名 {record.get("name")} -> {new_name}',
                  detail=f'记录 {record_id}')
    return JsonResponse({'success': True, 'message': '名称已修改'})


@require_POST
def api_open_terminal(request):
    """以指定目录为工作目录打开 cmd 命令行窗口，PATH 前置该目录。"""
    work_dir = request.POST.get('path', '').strip()
    return JsonResponse(env_ops.open_terminal(
        work_dir=work_dir,
        path_prepend=[work_dir] if work_dir else None,
    ))


@require_GET
def api_del_record(request):
    """删除安装记录（同时级联清除归属的虚拟环境管理记录）"""
    record_id = request.GET.get('id', '')
    if not record_id:
        return JsonResponse({'success': False, 'message': '缺少记录 ID'})
    record = storage.get_by_id(record_id)
    if not record:
        return JsonResponse({'success': False, 'message': '未找到该安装记录'})

    if record.get('locked'):
        return JsonResponse({
            'success': False, 'blocked': True,
            'message': '该解释器已锁定，请先解锁后再从列表移除',
        })

    venv_count = storage_venv.count_by_owner(record_id)
    strict = storage.load_config().get('strict_uninstall_check', False)
    if strict and venv_count > 0:
        return JsonResponse({
            'success': False,
            'blocked': True,
            'message': f'该解释器下还有 {venv_count} 个虚拟环境，请先全部删除后再从列表移除（或关闭「严格卸载检查」开关）',
            'venv_count': venv_count,
        })

    venv_cleaned = storage_venv.clear_by_owner(record_id)
    storage.remove_installed(record_id)
    msg = '安装记录已删除'
    if venv_cleaned > 0:
        msg += f'（同步清除 {venv_cleaned} 条虚拟环境管理记录）'
    return JsonResponse({'success': True, 'message': msg})


@require_POST
def api_import_installed(request):
    """将用户指定的文件夹中的 Python 环境导入安装记录"""
    folder_path = request.POST.get('folder_path', '').strip()
    if not folder_path:
        return JsonResponse({'success': False, 'message': '未提供文件夹路径'})
    try:
        result = detect.import_folder(folder_path)
    except Exception as e:
        logger.exception('导入 Python 环境失败')
        return JsonResponse({'success': False, 'message': f'导入失败: {e}'})
    return JsonResponse(result)


# ── 缓存清理 ────────────────────────────

def _path_size(path: pathlib.Path) -> int:
    """递归统计文件或目录占用字节数"""
    try:
        if path.is_file() or path.is_symlink():
            return path.stat().st_size
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _human_size(num: int) -> str:
    """字节数格式化为易读字符串"""
    size = float(num)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


@require_POST
def api_clear_cache(request):
    """清空 python_archive 应用的 tmp 目录下的全部内容。"""
    removed_count = 0
    freed = 0
    errors = []
    tmp_dir = pathlib.Path(TMP_DIR)
    if tmp_dir.exists():
        for entry in list(tmp_dir.iterdir()):
            try:
                freed += _path_size(entry)
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed_count += 1
            except OSError as e:
                errors.append(f'{entry.name}: {e}')
                logger.warning('清理缓存失败 %s: %s', entry, e)
    if errors:
        return JsonResponse({
            'success': False,
            'message': f'部分文件清理失败：{"；".join(errors)}',
        })
    if removed_count == 0:
        return JsonResponse({'success': True, 'message': '缓存目录已为空，无需清理'})
    return JsonResponse({
        'success': True,
        'message': f'已清空独立版本模式应用缓存目录，共删除 {removed_count} 项，释放 {_human_size(freed)}',
    })


# ── 下载源管理 ────────────────────────────────

@require_GET
def api_download_sources(request):
    """列出全部可选下载源与当前持久化默认。"""
    return JsonResponse({
        'success': True,
        'sources': python_download.list_sources(),
        'current': python_download.get_default_source(),
    })


@require_POST
def api_set_download_source(request):
    """保存默认下载源。POST 参数 name：源名或 'auto'（恢复自动探测）。"""
    name = request.POST.get('name', '').strip()
    try:
        saved = python_download.set_default_source(name)
    except ValueError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    except RuntimeError as e:
        return JsonResponse({'success': False, 'message': str(e)})
    return JsonResponse({
        'success': True,
        'message': f'默认下载源已设为：{saved["display_name"]}',
        'current': saved,
    })


# ── 应用设置 ────────────────────────────────

@require_POST
def api_settings_save(request):
    """保存应用配置"""
    config = storage.load_config()
    install_dir = request.POST.get('install_dir', '').strip()
    if install_dir:
        p = pathlib.Path(install_dir)
        if not p.exists():
            return JsonResponse({'success': False,
                                 'message': f'目录不存在: {install_dir}'})
        config['install_dir'] = install_dir

    strict_raw = request.POST.get('strict_uninstall_check', '')
    if strict_raw:
        config['strict_uninstall_check'] = strict_raw.lower() in ('on', 'true', '1')

    storage.save_config(config)
    return JsonResponse({'success': True, 'message': '设置已保存'})


# ═══════════════════════════════════════════════════════════════════════
# 虚拟环境（venv）管理
# ═══════════════════════════════════════════════════════════════════════

import subprocess as _sp
from datetime import datetime
from apps.python_main.views import PipListViewMixin, PipInstallViewMixin

from . import storage_venv
from . import venv_utils


class VenvListView(ArchiveMixin, TemplateView):
    template_name = 'python_archive/venv_list.html'

    def get(self, request, *args, **kwargs):
        record_id = kwargs.get('record_id', '')
        record = storage.get_by_id(record_id)
        if not record:
            from django.shortcuts import redirect
            return redirect('python_archive:index')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record_id = self.kwargs.get('record_id', '')
        record = storage.get_by_id(record_id) or {}

        venvs = storage_venv.list_venvs(owner_record_id=record_id)
        for v in venvs:
            l1 = venv_utils.verify_venv_l1(v.get('venv_path', ''))
            v['status'] = l1.get('status', v.get('status', 'ready'))
            v['status_reason'] = l1.get('reason', '')
        context['page_title'] = f'{record.get("name", "")} 的虚拟环境'
        context['record'] = record
        context['venvs'] = venvs
        context['default_venv_root'] = str(DEFAULT_VENV_ROOT).replace('\\', '/')
        context['breadcrumb'] = [
            {'title': 'Python 独立版本模式', 'href': reverse('python_archive:index'), 'active': False},
            {'title': f'{record.get("name", "")} 的虚拟环境', 'href': None, 'active': True},
        ]
        return context


class PipListVenvView(ArchiveMixin, PipListViewMixin):
    template_name = 'python_main/pip_list.html'

    def get_env_id(self) -> str:
        return self.kwargs.get('venv_id', '')

    def get(self, request, *args, **kwargs):
        vid = kwargs.get('venv_id', '')
        if not storage_venv.get_venv_by_id(vid):
            from django.shortcuts import redirect
            return redirect('python_archive:index')
        return super().get(request, *args, **kwargs)

    def url_install(self):
        return reverse('python_archive:pip_install_venv', args=[self.get_env_id()])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        vid = self.get_env_id()
        venv = storage_venv.get_venv_by_id(vid) or {}
        context['page_title'] = f'包管理 - {venv.get("name", "")}'
        context['record'] = venv
        context['version'] = venv.get('owner_version', '')
        context['breadcrumb'] = [
            {'title': 'Python 独立版本模式', 'href': reverse('python_archive:index'), 'active': False},
            {'title': '虚拟环境管理',
             'href': reverse('python_archive:venv_list', args=[venv.get('owner_record_id', '')]),
             'active': False},
            {'title': f'包管理', 'href': None, 'active': True},
        ]
        return context


class PipInstallVenvView(ArchiveMixin, PipInstallViewMixin):
    template_name = 'python_main/pip_install.html'

    def get_env_id(self) -> str:
        return self.kwargs.get('venv_id', '')

    def get(self, request, *args, **kwargs):
        vid = kwargs.get('venv_id', '')
        if not storage_venv.get_venv_by_id(vid):
            from django.shortcuts import redirect
            return redirect('python_archive:index')
        return super().get(request, *args, **kwargs)

    def url_list(self):
        return reverse('python_archive:pip_list_venv', args=[self.get_env_id()])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        vid = self.get_env_id()
        venv = storage_venv.get_venv_by_id(vid) or {}
        context['page_title'] = f'安装新包 - {venv.get("name", "")}'
        context['record'] = venv
        context['version'] = venv.get('owner_version', '')
        context['breadcrumb'] = [
            {'title': 'Python 独立版本模式', 'href': reverse('python_archive:index'), 'active': False},
            {'title': '虚拟环境管理',
             'href': reverse('python_archive:venv_list', args=[venv.get('owner_record_id', '')]),
             'active': False},
            {'title': f'包管理',
             'href': reverse('python_archive:pip_list_venv', args=[vid]), 'active': False},
            {'title': '安装新包', 'href': None, 'active': True},
        ]
        return context


# ── venv JSON API ───────────────────────────────────────────

def _terminal_cmd(venv_path: str) -> str:
    bat = os.path.join(venv_path, 'Scripts', 'activate.bat')
    if os.path.exists(bat):
        return f'cmd /k "{bat}"'
    return f'cmd /k cd /d "{venv_path}"'


@require_GET
def api_venv_list(request, record_id):
    venvs = storage_venv.list_venvs(owner_record_id=record_id)
    for v in venvs:
        l1 = venv_utils.verify_venv_l1(v.get('venv_path', ''))
        v['status'] = l1.get('status', v.get('status', 'ready'))
        v['status_reason'] = l1.get('reason', '')
    return JsonResponse({'success': True, 'venvs': venvs})


@require_POST
def api_venv_create(request):
    record_id = request.POST.get('record_id', '').strip()
    venv_path = request.POST.get('venv_path', '').strip()
    venv_name = request.POST.get('name', '').strip()
    include_system_site = request.POST.get('include_system_site') == '1'

    record = storage.get_by_id(record_id)
    if not record:
        return JsonResponse({'success': False, 'message': '归属的 Python 实例不存在'})

    if not venv_path:
        return JsonResponse({'success': False, 'message': '请选择虚拟环境保存目录'})

    path_obj = pathlib.Path(venv_path)
    name_part = path_obj.name or 'venv'
    import re as _re
    if _re.search(r'[^\w\-]', name_part, _re.UNICODE):
        return JsonResponse({'success': False, 'message':
                            '虚拟环境名称只能包含字母、数字、下划线和短横线（不能有中文或空格）'})

    if path_obj.exists():
        if not path_obj.is_dir():
            return JsonResponse({'success': False, 'message': f'目标路径是一个文件: {venv_path}'})
        try:
            if any(path_obj.iterdir()):
                return JsonResponse({'success': False, 'message':
                                    f'目标目录已存在且非空，请选择不存在的路径或先清空: {venv_path}'})
        except PermissionError:
            return JsonResponse({'success': False, 'message':
                                f'目标目录已存在且无法访问，请选择其他路径: {venv_path}'})

    existing = storage_venv.get_venv_by_path(venv_path)
    if existing:
        return JsonResponse({'success': False, 'message':
                            f'该路径已是虚拟环境记录，请改用导入功能'})

    if not venv_name:
        venv_name = path_obj.name or 'venv'

    task = tasks.create_task('venv.create',
                             meta={'record_id': record_id, 'venv_path': venv_path},
                             title=f'创建虚拟环境 {venv_name}')

    options = {
        'record_id': record_id,
        'venv_name': venv_name,
        'venv_path': venv_path,
        'include_system_site': include_system_site,
        'install_dir': record.get('install_dir', ''),
        'record_name': record.get('name', ''),
    }
    tasks.run_in_thread(task, _venv_create_worker, options)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _venv_create_worker(task, options):
    def log(msg):
        tasks.append_log(task, msg)

    log(f'开始创建虚拟环境 {options["venv_name"]}')
    log(f'保存路径: {options["venv_path"]}')
    if options['include_system_site']:
        log('已开启 继承系统 site-packages')

    def on_output(line):
        tasks.append_log(task, line)

    result = venv_utils.create_venv(
        options['install_dir'],
        target_path=options['venv_path'],
        include_system_site=options['include_system_site'],
        on_output=on_output)

    if not result.get('success'):
        tasks.fail_task(task, result.get('message', '创建失败'))
        return

    verify = result.get('verify', {})
    log(f'验证通过，pip_count={verify.get("pip_count", 0)}')

    storage_venv.add_venv({
        'name': options['venv_name'],
        'owner_record_id': options['record_id'],
        'owner_install_dir': options['install_dir'],
        'owner_version': '',
        'venv_path': result.get('venv_path', ''),
        'python_exe': verify.get('python_exe', ''),
        'pip_count': verify.get('pip_count', 0),
        'include_system_site': options['include_system_site'],
        'created_source': 'created',
        'status': verify.get('status', 'ready'),
        'last_verified_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })
    log('已登记到虚拟环境管理列表')
    log_operation('python_archive', f'创建虚拟环境 {options["venv_name"]}',
                  detail=f'归属 {options["record_name"]} -> {result.get("venv_path")}')
    tasks.set_result(task, {'success': True, 'message': result.get('message', '')})


@require_POST
def api_venv_import(request):
    import pathlib
    venv_path = request.POST.get('venv_path', '').strip()
    force = request.POST.get('force') == '1'

    result = venv_utils.import_venv(venv_path, force=force)
    if not result.get('success'):
        return JsonResponse({'success': False, 'message': result.get('message', '')})

    owner = result.get('owner_record')
    if not owner:
        return JsonResponse({'success': False, 'message':
                            '归属的 Python 未登记，请先安装或导入该 Python，'
                            '或使用强制导入（将标记为归属丢失）'})

    existing = storage_venv.get_venv_by_path(venv_path)
    if existing:
        return JsonResponse({'success': False, 'message': '该虚拟环境已在管理中'})

    verify = result.get('verify', {})
    name = os.path.basename(venv_path.rstrip(os.sep))
    storage_venv.add_venv({
        'name': name,
        'owner_record_id': owner.get('id', ''),
        'owner_install_dir': owner.get('install_dir', ''),
        'owner_version': owner.get('version', ''),
        'venv_path': venv_path,
        'python_exe': result.get('python_exe', ''),
        'pip_count': verify.get('pip_count', 0),
        'created_source': 'imported',
        'status': verify.get('status', 'ready'),
        'last_verified_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })
    log_operation('python_archive', f'导入虚拟环境 {name}', detail=venv_path)
    return JsonResponse({'success': True, 'message': '导入成功'})


@require_POST
def api_toggle_lock(request):
    """通用锁切换 API：type=owner|venv, id=记录ID"""
    obj_type = request.POST.get('type', '').strip()
    obj_id = request.POST.get('id', '').strip()
    if obj_type not in ('owner', 'venv') or not obj_id:
        return JsonResponse({'success': False, 'message': '参数错误'})

    if obj_type == 'owner':
        result = storage.toggle_lock(obj_id)
        if not result:
            return JsonResponse({'success': False, 'message': '记录不存在'})
        label = result.get('name', '')
    else:
        result = storage_venv.toggle_venv_lock(obj_id)
        if not result:
            return JsonResponse({'success': False, 'message': '虚拟环境不存在'})
        label = result.get('name', '')

    locked = result.get('locked', False)
    log_operation('python_archive', f"{'锁定' if locked else '解锁'} {obj_type}: {label}")
    return JsonResponse({
        'success': True,
        'locked': locked,
        'message': f"已{'锁定' if locked else '解锁'} {label}",
    })


@require_POST
def api_venv_delete(request):
    venv_id = request.POST.get('venv_id', '').strip()
    delete_dir = request.POST.get('delete_dir', '1') == '1'

    record = storage_venv.get_venv_by_id(venv_id)
    if not record:
        return JsonResponse({'success': False, 'message': '虚拟环境不存在'})

    if record.get('locked'):
        return JsonResponse({
            'success': False, 'blocked': True,
            'message': '该虚拟环境已锁定，请先解锁后再删除',
        })

    result = venv_utils.delete_venv(record.get('venv_path', ''), delete_dir=delete_dir)
    if not result.get('success'):
        return JsonResponse(result)

    storage_venv.remove_venv(venv_id)
    log_operation('python_archive', f'删除虚拟环境 {record.get("name")}',
                  detail=record.get('venv_path'))
    return JsonResponse({'success': True, 'message': '删除成功'})


@require_POST
def api_venv_rename(request):
    venv_id = request.POST.get('venv_id', '').strip()
    new_name = request.POST.get('new_name', '').strip()

    record = storage_venv.get_venv_by_id(venv_id)
    if not record:
        return JsonResponse({'success': False, 'message': '虚拟环境不存在'})

    storage_venv.update_venv(venv_id, name=new_name)
    return JsonResponse({'success': True, 'message': '重命名成功'})


@require_POST
def api_venv_open_terminal(request):
    venv_id = request.POST.get('venv_id', '').strip()
    record = storage_venv.get_venv_by_id(venv_id)
    if not record:
        return JsonResponse({'success': False, 'message': '虚拟环境不存在'})

    venv_path = record.get('venv_path', '')
    venv_scripts = os.path.join(venv_path, 'Scripts')
    bat = os.path.join(venv_scripts, 'activate.bat')
    activate_cmd = f'"{bat}"' if os.path.exists(bat) else None

    return JsonResponse(env_ops.open_terminal(
        work_dir=venv_path,
        path_prepend=[venv_scripts] if venv_path else None,
        activate_cmd=activate_cmd,
    ))


@require_GET
def api_venv_verify_l1(request, venv_id):
    record = storage_venv.get_venv_by_id(venv_id)
    if not record:
        return JsonResponse({'success': False, 'message': '虚拟环境不存在'})

    l1 = venv_utils.verify_venv_l1(record.get('venv_path', ''))
    storage_venv.update_venv(venv_id, status=l1.get('status', 'ready'),
                             last_verified_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    return JsonResponse({'success': True, 'status': l1.get('status', 'ready'),
                         'reason': l1.get('reason', '')})


@require_GET
def api_venv_verify(request, venv_id):
    record = storage_venv.get_venv_by_id(venv_id)
    if not record:
        return JsonResponse({'success': False, 'message': '虚拟环境不存在'})

    l2 = venv_utils.verify_venv_l2(record.get('venv_path', ''))
    storage_venv.update_venv(venv_id, status=l2.get('status', 'ready'),
                             pip_count=l2.get('pip_count', 0),
                             last_verified_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    return JsonResponse({
        'success': True,
        'status': l2.get('status', 'ready'),
        'reason': l2.get('reason', ''),
        'pip_count': l2.get('pip_count', 0),
        'pip_version': l2.get('pip_version', ''),
    })