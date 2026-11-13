"""
conda 视图层 — 页面路由 + JSON API。

页面视图:
  IndexView       — 智能跳转（空→发行版管理，1个→环境管理，多个→发行版管理）
  DistroListView  — 发行版管理列表页
  EnvManageView   — 环境管理（左环境 + 右包）
  CondaInstallView — 下载安装页

JSON API 全部在 api/ 路径下，保留不变。
"""
import json
import os
import subprocess
import logging
import time
from pathlib import Path

from django.views.generic.base import TemplateView, RedirectView
from django.views.decorators.http import require_GET, require_POST
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from jiecore.views import AppMixin
from jiecore import tasks
from jiecore.op_log import log_operation
from . import detect, storage, conda_ops, download_sources, config_manager, cache, config
from .config import CONDA_INSTALL_ROOT, CORE_DEPENDENCIES, PACKAGE_CATEGORIES, PACKAGE_BUNDLES

_log = logging.getLogger(__name__)

_VERSIONS_CACHE = {}

def _get_condarc_hash() -> str:
    condarc = config_manager.get_condarc_path()
    if not condarc.exists():
        return ''
    return cache._file_hash(condarc)

def _cached_versions_get(cache_key):
    entry = _VERSIONS_CACHE.get(cache_key)
    if entry and (time.time() - entry['ts']) < 120:
        return entry['data']
    return None

def _cached_versions_set(cache_key, data):
    _VERSIONS_CACHE[cache_key] = {'ts': time.time(), 'data': data}
    if len(_VERSIONS_CACHE) > 50:
        now = time.time()
        for k in list(_VERSIONS_CACHE.keys()):
            if now - _VERSIONS_CACHE[k]['ts'] > 300:
                _VERSIONS_CACHE.pop(k, None)

def _invalidate_memory_versions(record_id: str):
    prefix = f"{record_id}:"
    for k in list(_VERSIONS_CACHE.keys()):
        if k.startswith(prefix):
            _VERSIONS_CACHE.pop(k, None)

logger = logging.getLogger(__name__)


class CondaMixin(AppMixin):
    """菜单高亮 Mixin，设置当前应用的菜单ID"""
    parent_menu_id = 'runtimes'
    menu_id = 'conda'


# ── 页面视图 ─────────────────────────────────────────

class IndexView(CondaMixin, TemplateView):
    """首页 — 统一转到发行版管理（DistroListView 会先 scan_registry + sync_with_detected，
    确保 storage 数据永远是实时探测到的，不会出现用户在 jieEnv 外卸载后仍显示旧 distro_id 的坏链）"""

    def get(self, request, *args, **kwargs):
        return redirect('conda:distro_list')


class DistroListView(CondaMixin, TemplateView):
    """发行版管理列表页"""
    template_name = 'conda/distro_list.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Conda 管理 - 发行版列表'

        # 每次进入都扫描注册表（耗时 ~0.18s），同步 storage 新增/更新/清理
        try:
            scan_results = detect.scan_registry()
            storage.sync_with_detected(scan_results)
        except Exception as e:
            logger.warning('DistroListView auto scan_registry failed: %s', e)

        context['installations'] = storage.list_installations()
        context['breadcrumb'] = [
            {'title': '环境管理', 'href': reverse('conda:index'), 'active': False},
            {'title': '发行版列表', 'href': None, 'active': True},
        ]
        return context


class EnvManageView(CondaMixin, TemplateView):
    """环境管理页（左环境列表 + 右包列表）"""
    template_name = 'conda/env_manage.html'

    def get_context_data(self, distro_id=None, **kwargs):
        context = super().get_context_data(**kwargs)
        installation = storage.get_installation(record_id=distro_id)
        all_installs = storage.list_installations()
        context['page_title'] = '环境管理'
        context['installation'] = installation
        context['distro_id'] = distro_id
        context['total_installations'] = len(all_installs)
        initial_env_name = self.request.GET.get('env', '').strip()
        if initial_env_name and installation:
            try:
                envs = conda_ops.list_envs(installation['conda_bat'])
                matched = next((e for e in envs if (e.get('name') or '').strip() == initial_env_name), None)
                if matched:
                    context['initial_env_prefix'] = matched['prefix']
                    context['initial_env_name'] = matched.get('name', '')
            except Exception:
                pass
        return context


class CondaInstallView(CondaMixin, TemplateView):
    """独立下载安装页面（配置表单 + 内嵌进度/日志区，不弹窗）。"""
    template_name = 'conda/install.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        installers = download_sources.list_installers()
        context['page_title'] = '下载安装 Conda'
        context['installers'] = installers
        context['installers_json'] = json.dumps(installers, ensure_ascii=False)
        default_subdirs = {inst['key']: inst['default_subdir'] for inst in installers}
        context['default_subdirs_json'] = json.dumps(default_subdirs, ensure_ascii=False)
        context['default_install_root'] = str(CONDA_INSTALL_ROOT).replace('\\', '/')
        default_dir = ''
        if installers:
            default_dir = str(CONDA_INSTALL_ROOT / installers[0]['default_subdir']).replace('\\', '/')
        context['default_install_dir'] = default_dir
        context['breadcrumb'] = [
            {'title': '环境管理', 'href': reverse('conda:index'), 'active': False},
            {'title': '下载安装', 'href': None, 'active': True},
        ]
        return context


class CondaUninstallView(CondaMixin, TemplateView):
    """独立卸载页面 — 选项配置 + 启动 + 等待用户手动确认完成。"""
    template_name = 'conda/uninstall.html'

    def get_context_data(self, distro_id=None, **kwargs):
        context = super().get_context_data(**kwargs)
        context['distro_id'] = distro_id
        installation = storage.get_installation(record_id=distro_id)
        if not installation:
            context['installation'] = None
            context['page_title'] = '卸载 Conda'
            return context
        display_name = installation.get('display_name', installation.get('install_dir', ''))
        context['page_title'] = f'卸载 {display_name}'
        context['installation'] = installation
        context['install_dir_display'] = str(installation.get('install_dir', '')).replace('\\', '/')
        context['uninstall_cmd'] = installation.get('uninstall_string', '')
        context['breadcrumb'] = [
            {'title': '环境管理', 'href': reverse('conda:index'), 'active': False},
            {'title': '发行版管理', 'href': reverse('conda:distro_list'), 'active': False},
            {'title': '卸载', 'href': None, 'active': True},
        ]
        return context


# ── JSON API ─────────────────────────────────────────

@require_GET
def api_scan_registry(request):
    """扫描注册表，返回已发现的发行版。"""
    results = detect.scan_registry()
    return JsonResponse({'success': True, 'data': results})


@require_POST
def api_import_registry(request):
    """扫描注册表并直接把完整探测结果登记到 storage（后端一步完成，
    不走前端 POST 数据，避免重复探测和字段丢失）。
    同时清理探测不到的旧记录（用户可能在 jieEnv 外卸载了）。"""
    results = detect.scan_registry()
    stats = storage.sync_with_detected(results)

    return JsonResponse({
        'success': True,
        'message': f'新增 {stats["added"]} 个，更新 {stats["updated"]} 个，清理 {stats["removed"]} 个',
        'stats': stats,
    })


@require_GET
def api_installations(request):
    """返回已登记的所有发行版（仅读 JSON，秒回）。
    envs 列表等用户点击发行版时再按需调 api_envs 加载。"""
    items = storage.list_installations()
    return JsonResponse({'success': True, 'data': items})


@require_GET
def api_envs(request):
    """列出指定发行版的所有环境（同时自动清理孤儿快照）。"""
    install_dir = request.GET.get('install_dir', '').strip()
    record_id = request.GET.get('id', '').strip()
    force_refresh = request.GET.get('refresh', '') == '1'
    installation = storage.get_installation(install_dir=install_dir, record_id=record_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '发行版未登记'}, status=404)

    distro_id = installation.get('id', '')
    envs = None
    if not force_refresh and distro_id:
        envs = cache.get_envs_cache(distro_id)

    if envs is None:
        envs = conda_ops.list_envs(installation['conda_bat'])
        if distro_id:
            cache.set_envs_cache(distro_id, envs)

    if distro_id:
        current_prefixes = [e['prefix'] for e in envs]
        conda_ops.clean_orphan_pkg_snapshots(distro_id, current_prefixes)

    return JsonResponse({'success': True, 'data': envs, 'cached': envs is not None and not force_refresh})


@require_GET
def api_packages(request):
    """列出指定环境的已安装包（默认读快照，refresh=1 强制跑 conda list）。"""
    install_dir = request.GET.get('install_dir', '').strip()
    env_prefix = request.GET.get('env_prefix', '').strip()
    record_id = request.GET.get('id', '').strip()
    force_refresh = request.GET.get('refresh', '') == '1'
    installation = storage.get_installation(install_dir=install_dir, record_id=record_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '发行版未登记'}, status=404)

    packages = conda_ops.list_packages(
        installation['conda_bat'], env_prefix,
        distro_id=installation.get('id', ''),
        force_refresh=force_refresh,
    )
    for pkg in packages:
        pkg['is_core'] = pkg.get('name', '').lower() in CORE_DEPENDENCIES
    return JsonResponse({'success': True, 'data': packages})


@require_GET
def api_search_versions(request):
    """动态查询 conda 可用版本列表（按大版本去重取最高小版本）。

    三级缓存: 内存(120s) → JSON 文件(7天) → conda search 兜底。
    缓存 key 带 condarc hash，镜像/频道切换自动失效。
    """
    record_id = request.GET.get('id', '').strip() or request.GET.get('distro_id', '').strip()
    package = request.GET.get('package', '').strip()
    if not package:
        return JsonResponse({'success': False, 'message': '缺少 package 参数'}, status=400)

    installation = storage.get_installation(record_id=record_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '发行版未登记'}, status=404)

    install_dir = installation.get('install_dir', '')
    condarc_hash = _get_condarc_hash()
    cache_key = f"{record_id}:{condarc_hash}:{package}"

    in_mem = _cached_versions_get(cache_key)
    if in_mem is not None:
        return JsonResponse({'success': True, 'data': in_mem, 'cached': True})

    on_disk = cache.get_version_cache(record_id, condarc_hash, package)
    if on_disk is not None:
        _cached_versions_set(cache_key, on_disk)
        return JsonResponse({'success': True, 'data': on_disk, 'cached': True, 'source': 'disk'})

    versions = conda_ops.search_package_versions(installation['conda_bat'], package)
    _cached_versions_set(cache_key, versions)
    cache.set_version_cache(record_id, condarc_hash, package, versions)
    return JsonResponse({'success': True, 'data': versions, 'cached': False})


@require_POST
def api_create_env(request):
    """创建 conda 环境（后台任务）。"""
    install_dir = request.POST.get('install_dir', '').strip()
    name = request.POST.get('name', '').strip()
    python_version = request.POST.get('python_version', '').strip()
    r_version = request.POST.get('r_version', '').strip()
    record_id = request.POST.get('id', '').strip()

    installation = storage.get_installation(install_dir=install_dir, record_id=record_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '发行版未登记'}, status=404)
    if not name:
        return JsonResponse({'success': False, 'message': '请提供环境名'}, status=400)

    conda_bat = installation['conda_bat']
    distro_id = installation.get('id', '')
    existing = None
    if distro_id:
        existing = cache.get_envs_cache(distro_id)
    if existing is None:
        existing = conda_ops.list_envs(conda_bat)
        if distro_id:
            cache.set_envs_cache(distro_id, existing)
    for e in existing:
        if e.get('name') == name:
            return JsonResponse({'success': False, 'message': f'环境 {name} 已存在，请换个名字'})

    task = tasks.create_task('conda.create_env', meta={
        'name': name, 'python_version': python_version, 'r_version': r_version,
        'record_id': record_id, 'installation': installation.get('display_name', ''),
    }, title=f'创建环境 {name}')

    tasks.append_log(task, f'开始创建环境 {name}...')
    tasks.run_in_thread(task, _create_env_worker, conda_bat, name,
                        python_version, r_version, installation)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _create_env_worker(task: dict, conda_bat: str, name: str,
                       python_version: str, r_version: str, installation: dict):
    """后台线程：流式创建 conda 环境，实时吐日志。"""

    def log_cb(line):
        tasks.append_log(task, line)

    tasks.set_progress(task, stage=f'创建环境 {name}...')
    result = conda_ops.create_env_streaming(
        conda_bat, name,
        python_version=python_version, r_version=r_version,
        log_cb=log_cb,
    )

    parts = []
    if python_version:
        parts.append(f'python={python_version}')
    if r_version:
        parts.append(f'r={r_version}')
    detail = f'{installation.get("display_name", "")}: {" ".join(parts) or "空环境"}'

    distro_id = installation.get('id', '')
    if result.get('success'):
        if distro_id:
            cache.invalidate_envs_cache(distro_id)
        tasks.set_progress(task, progress=100, stage='完成')
        tasks.set_result(task, {'success': True, 'message': f'环境 {name} 创建成功'})
        log_operation('conda', f'创建环境 {name}', success=True, detail=detail)
    else:
        tasks.set_result(task, {'success': False, 'message': result.get('message', '创建失败')})
        log_operation('conda', f'创建环境 {name}', success=False,
                      detail=result.get('message', ''))
        raise RuntimeError(result.get('message', '创建失败'))


@require_POST
def api_remove_env(request):
    """删除 conda 环境（后台任务）。"""
    install_dir = request.POST.get('install_dir', '').strip()
    env_prefix = request.POST.get('env_prefix', '').strip()
    record_id = request.POST.get('id', '').strip()

    installation = storage.get_installation(install_dir=install_dir, record_id=record_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '发行版未登记'}, status=404)
    if not env_prefix:
        return JsonResponse({'success': False, 'message': '请提供环境路径'}, status=400)

    env_name = os.path.basename(env_prefix.rstrip('\\/'))
    task = tasks.create_task('conda.remove_env', meta={
        'env_prefix': env_prefix, 'env_name': env_name,
        'record_id': record_id, 'installation': installation.get('display_name', ''),
    }, title=f'删除环境 {env_name}')

    tasks.append_log(task, f'开始删除环境 {env_name} ({env_prefix})...')
    tasks.run_in_thread(task, _remove_env_worker, installation, env_prefix)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _remove_env_worker(task: dict, installation: dict, env_prefix: str):
    """后台线程：流式删除 conda 环境，实时吐日志。"""

    def log_cb(line):
        tasks.append_log(task, line)

    env_name = os.path.basename(env_prefix.rstrip('\\/'))
    tasks.set_progress(task, stage=f'删除环境 {env_name}...')
    result = conda_ops.remove_env_streaming(installation['conda_bat'], env_prefix, log_cb=log_cb)

    distro_id = installation.get('id', '')
    if result.get('success'):
        if distro_id:
            cache.invalidate_envs_cache(distro_id)
        conda_ops.remove_pkg_snapshot(distro_id, env_prefix)
        tasks.set_progress(task, progress=100, stage='完成')
        tasks.set_result(task, {'success': True, 'message': f'环境 {env_name} 已删除'})
        log_operation('conda', f'删除环境 {env_name}', success=True, detail=env_prefix)
    else:
        tasks.set_result(task, {'success': False, 'message': result.get('message', '删除失败')})
        log_operation('conda', f'删除环境 {env_name}', success=False,
                      detail=result.get('message', ''))
        raise RuntimeError(result.get('message', '删除失败'))


@require_POST
def api_remove_installation(request):
    """从 storage 移除一个发行版登记（不卸载实际文件），同时清理其所有包快照。"""
    install_dir = request.POST.get('install_dir', '').strip()
    record_id = request.POST.get('id', '').strip()

    target = storage.get_installation(install_dir=install_dir, record_id=record_id)
    distro_id = target.get('id', '') if target else ''
    display_name = target.get('display_name', target.get('install_dir', '')) if target else ''

    ok = storage.remove_installation(install_dir=install_dir, record_id=record_id)
    if ok and distro_id:
        conda_ops.clean_pkg_snapshots_for_distro(distro_id)
        cache.invalidate_envs_cache(distro_id)
        cache.invalidate_version_cache(distro_id)
    log_operation('conda', f'移除登记 {display_name}', success=ok)
    return JsonResponse({'success': ok, 'message': '已移除登记' if ok else '未找到该记录'})


# ── 卸载发行版（简化版：启动后不监视，用户手动确认）─────────

import shlex


def _split_uninstall_command(cmd: str) -> tuple:
    """把注册表 UninstallString 拆成 (exe, args)。"""
    cmd = (cmd or '').strip()
    if not cmd:
        return '', ''
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end != -1:
            return cmd[1:end], cmd[end + 1:].strip()
        return cmd.strip('"'), ''
    idx = cmd.lower().find('.exe')
    if idx != -1:
        cut = idx + len('.exe')
        return cmd[:cut], cmd[cut:].strip()
    parts = cmd.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else '')


def _precheck_collect_env_dirs(install_dir_norm: str) -> list:
    """三源收集 env 路径（不跑 conda，毫秒级）。"""
    found = []

    envs_root = os.path.join(install_dir_norm, 'envs')
    if os.path.isdir(envs_root):
        try:
            for name in os.listdir(envs_root):
                p = os.path.join(envs_root, name)
                if os.path.isdir(p):
                    found.append(os.path.normpath(p))
        except OSError:
            pass

    userprofile = os.environ.get('USERPROFILE', '')
    if userprofile:
        env_txt = os.path.join(userprofile, '.conda', 'environments.txt')
        if os.path.isfile(env_txt):
            try:
                with open(env_txt, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if line and os.path.isdir(line):
                            found.append(os.path.normpath(line))
            except OSError:
                pass

    seen = set()
    unique = []
    for p in found:
        low = p.lower()
        if low in seen:
            continue
        seen.add(low)
        unique.append(p)
    return unique


@require_GET
def api_uninstall_precheck(request):
    """预检：扫描安装目录内是否有非 base 环境（文件系统扫描，毫秒级）。"""
    record_id = request.GET.get('id', '').strip()
    installation = storage.get_installation(record_id=record_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '发行版未登记'})

    install_dir_raw = (installation.get('install_dir') or '').rstrip('\\/')
    install_dir_norm = os.path.normpath(install_dir_raw) if install_dir_raw else ''
    install_dir_lower = install_dir_norm.lower()

    if not install_dir_lower or not os.path.isdir(install_dir_norm):
        return JsonResponse({'success': True, 'envs': [], 'note': '安装目录不存在'})

    all_env_dirs = _precheck_collect_env_dirs(install_dir_norm)

    inside_envs = []
    for prefix in all_env_dirs:
        p_lower = prefix.lower()
        if p_lower == install_dir_lower:
            continue
        if p_lower.startswith(install_dir_lower + os.sep):
            inside_envs.append({
                'name': os.path.basename(prefix),
                'prefix': prefix,
            })

    return JsonResponse({'success': True, 'envs': inside_envs})


@require_POST
def api_uninstall_installation(request):
    """启动 conda 卸载（GUI / 静默模式，启动后不等待）。"""
    record_id = request.POST.get('id', '').strip()
    silent = request.POST.get('silent', '1') == '1'

    installation = storage.get_installation(record_id=record_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '发行版未登记'})

    display_name = installation.get('display_name', installation.get('install_dir', ''))
    install_dir = installation.get('install_dir', '')
    uninstall_cmd = (installation.get('uninstall_string') or '').strip()

    if not uninstall_cmd:
        return JsonResponse({'success': False, 'message': f'{display_name} 缺少卸载命令（UninstallString）'})

    exe, _ = _split_uninstall_command(uninstall_cmd)
    if not exe or not os.path.exists(exe):
        return JsonResponse({'success': False, 'message': f'卸载程序不存在: {exe or uninstall_cmd}'})

    cmd_args = ['/S'] if silent else []

    uninstall_env = os.environ.copy()
    uninstall_env.pop('CONDA_PKGS_DIRS', None)
    for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
        uninstall_env.pop(key, None)

    try:
        subprocess.Popen(
            [exe] + cmd_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            env=uninstall_env,
        )
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'启动卸载失败: {e}'})

    log_operation('conda', f'启动卸载 {display_name}', success=True,
                  detail=f'{"静默" if silent else "GUI"}模式 {install_dir}')
    return JsonResponse({'success': True, 'message': '卸载已启动'})


@require_POST
def api_uninstall_confirm(request):
    """用户点"我已卸载完成"后：扫目录 + 强制清残留 + 更新登记。"""
    import shutil

    record_id = request.POST.get('id', '').strip()
    force_cleanup = request.POST.get('force_cleanup', '0') == '1'

    installation = storage.get_installation(record_id=record_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '发行版未登记'})

    display_name = installation.get('display_name', installation.get('install_dir', ''))
    install_dir = installation.get('install_dir', '')

    dir_exists = os.path.isdir(install_dir)
    cleaned = False
    cleanup_msg = ''

    if dir_exists and force_cleanup:
        try:
            shutil.rmtree(install_dir, ignore_errors=True)
            cleaned = True
            cleanup_msg = f'已强制清理残留目录: {install_dir}'
        except Exception as e:
            cleanup_msg = f'清理残留目录失败: {e}'

    ok = storage.remove_installation(record_id=record_id)
    if ok:
        conda_ops.clean_pkg_snapshots_for_distro(record_id)
        cache.invalidate_envs_cache(record_id)
        cache.invalidate_version_cache(record_id)

    log_operation('conda', f'卸载 {display_name}', success=True,
                  detail=f'目录残留={"已清理" if cleaned else ("仍存在" if dir_exists else "已删除")}')

    return JsonResponse({
        'success': True,
        'message': f'{display_name} 卸载完成',
        'install_dir_exists': os.path.isdir(install_dir),
        'cleanup_msg': cleanup_msg,
        'storage_removed': ok,
    })


# ── 下载安装发行版（后台任务）───────────────────────────

@require_GET
def api_list_download_sources(request):
    """返回可下载的发行版列表及其镜像源。"""
    return JsonResponse({'success': True, 'data': download_sources.list_installers()})


@require_POST
def api_download_install_conda(request):
    """下载 conda 安装包并静默安装，完成后自动探测登记。"""
    installer_key = (request.POST.get('installer') or '').strip()
    mirror_id = (request.POST.get('mirror_id') or '').strip()
    install_dir = (request.POST.get('install_dir') or '').strip()

    installer = download_sources.get_installer(installer_key)
    if not installer:
        return JsonResponse({'success': False, 'message': f'未知的发行版: {installer_key}'})
    if not install_dir:
        return JsonResponse({'success': False, 'message': '请指定安装目录'})

    selected_mirror = None
    for m in installer.get('mirrors', []):
        if m['id'] == mirror_id:
            selected_mirror = m
            break
    if not selected_mirror:
        return JsonResponse({'success': False, 'message': '未找到可用下载源'})

    if storage.get_installation(install_dir=install_dir):
        return JsonResponse({
            'success': False,
            'message': f'该目录已有登记: {install_dir}（如需重装请先卸载）',
        })

    install_path = Path(install_dir)
    if install_path.exists() and any(install_path.iterdir()):
        return JsonResponse({
            'success': False,
            'message': f'安装目录已存在且非空: {install_dir}',
        })

    task = tasks.create_task('conda.download_install', meta={
        'installer': installer_key,
        'mirror': selected_mirror['id'],
        'install_dir': install_dir,
    }, title=f'下载安装 {installer["name"]}')

    tasks.append_log(task, f'安装任务已创建: {installer["name"]} -> {install_dir}')
    tasks.append_log(task, f'下载源: {selected_mirror["label"]}')
    tasks.run_in_thread(task, _download_install_worker,
                        installer['name'], installer['filename'],
                        selected_mirror['url'], install_dir)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _fmt_mb(n):
    return f'{n / (1024 * 1024):.1f} MB'


def _download_install_worker(task: dict, display_name: str, filename: str,
                              url: str, install_dir: str):
    """后台线程：下载 → 静默安装 → 探测登记。"""

    def on_step(msg):
        tasks.append_log(task, msg)
        tasks.set_progress(task, stage=msg)

    on_step(f'开始下载 {display_name}...')

    def progress_cb(received, total, _speed):
        if total:
            pct = received * 100 // total
            tasks.set_progress(task, progress=pct,
                               stage=f'下载中 {pct}% ({_fmt_mb(received)}/{_fmt_mb(total)})')
            on_step(f'下载中 {pct}% ({_fmt_mb(received)}/{_fmt_mb(total)})')
        else:
            on_step(f'下载中... ({_fmt_mb(received)})')

    try:
        local_path = conda_ops.download_installer(url, filename, progress_cb)
    except Exception as e:
        on_step(f'下载失败: {e}')
        tasks.set_result(task, {'success': False, 'message': f'下载失败: {e}'})
        log_operation('conda', f'下载安装 {display_name}', success=False, detail=f'下载失败: {e}')
        raise

    size_mb = local_path.stat().st_size / (1024 * 1024)
    on_step(f'下载完成 ({size_mb:.1f} MB)，开始静默安装...')
    tasks.set_progress(task, progress=60, stage='静默安装中...')

    install_dir_path = Path(install_dir)
    install_dir_path.mkdir(parents=True, exist_ok=True)

    last_hb = {'fc': -1}

    def install_heartbeat(file_count, total_size):
        if file_count == last_hb['fc']:
            return
        last_hb['fc'] = file_count
        mb = total_size / (1024 * 1024)
        msg = f'静默安装中... 已安装 {file_count} 文件 / {mb:.1f} MB'
        tasks.append_log(task, msg)

    result = conda_ops.run_silent_install(
        str(local_path), install_dir, progress_cb=install_heartbeat,
    )
    if result['returncode'] != 0:
        on_step(f'安装失败，退出码: {result["returncode"]}')
        if result['stderr']:
            on_step(f'stderr: {result["stderr"][-300:]}')
        tasks.set_result(task, {'success': False, 'message': result['stderr'][:200] or '安装失败'})
        log_operation('conda', f'下载安装 {display_name}', success=False,
                      detail=f'安装退出码 {result["returncode"]}')
        raise RuntimeError(f'安装失败: {result["stderr"][:200]}')

    on_step(f'安装程序退出码: {result["returncode"]}（0=成功）')
    tasks.set_progress(task, progress=90, stage='探测安装信息...')
    on_step('探测安装信息...')

    detected = detect.detect_custom_dir(install_dir)
    if not detected.get('success'):
        on_step(f'探测失败: {detected.get("message", "未知")}')
        tasks.set_result(task, {'success': False, 'message': f'探测失败: {detected.get("message")}'})
        log_operation('conda', f'下载安装 {display_name}', success=False,
                      detail=f'探测失败: {detected.get("message")}')
        raise RuntimeError(f'探测失败: {detected.get("message")}')

    record = {k: v for k, v in detected.items() if k != 'success'}
    record['is_external'] = False
    storage.upsert_installation(record)

    on_step(f'已登记发行版 {record.get("display_name", display_name)} '
            f'{record.get("version", "")}')

    on_step('安装完成')
    tasks.set_progress(task, progress=100, stage='完成')
    tasks.set_result(task, {'success': True, 'message': f'{display_name} 安装完成',
                             'distro_id': record.get('id', '')})
    log_operation('conda', f'下载安装 {display_name}', success=True, detail=install_dir)
# ====================================================================
# 配置编辑器
# ====================================================================

def _get_installation_or_404(distro_id):
    inst = storage.get_installation(record_id=distro_id)
    if not inst:
        from django.http import Http404
        raise Http404(f'未找到发行版: {distro_id}')
    return inst


class DistroConfigView(CondaMixin, TemplateView):
    """发行版配置编辑器页面"""
    template_name = 'conda/distro_config.html'

    def get_context_data(self, distro_id=None, **kwargs):
        context = super().get_context_data(**kwargs)
        installation = _get_installation_or_404(distro_id)
        install_dir = installation.get('install_dir', '')
        context['page_title'] = '发行版配置'
        context['installation'] = installation
        context['distro_id'] = distro_id
        context['condarc_path'] = str(config_manager.get_condarc_path())
        context['breadcrumb'] = [
            {'title': '环境管理', 'href': reverse('conda:index'), 'active': False},
            {'title': '配置', 'href': None, 'active': True},
        ]
        return context


@require_GET
def api_config_get(request):
    """读取 .condarc 配置 + 合规检测"""
    distro_id = request.GET.get('distro_id', '')
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    install_dir = installation.get('install_dir', '')
    cfg = config_manager.read_config()
    compliance = config_manager.detect_compliance(cfg)
    raw_yaml = config_manager.get_raw_yaml()
    backups = config_manager.list_backups()
    return JsonResponse({
        'success': True,
        'config': cfg,
        'raw_yaml': raw_yaml,
        'condarc_path': str(config_manager.get_condarc_path()),
        'compliance': compliance,
        'backups': backups,
        'mirrors': config_manager.get_mirror_list(),
        'channels': config_manager.get_channel_list(),
    })


@require_POST
def api_config_save(request):
    """保存 YAML 原文"""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)
    distro_id = data.get('distro_id', '')
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    install_dir = installation.get('install_dir', '')
    raw_yaml = data.get('raw_yaml', '')
    result = config_manager.save_raw_yaml(raw_yaml)
    if result.get('success'):
        cache.invalidate_version_cache(distro_id)
        cache.invalidate_envs_cache(distro_id)
        _invalidate_memory_versions(distro_id)
        log_operation('conda', f'修改配置 {distro_id}', success=True, detail=install_dir)
    return JsonResponse(result)


@require_POST
def api_config_mirror(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)
    distro_id = data.get('distro_id', '')
    mirror_id = data.get('mirror_id', '')
    channel_id = data.get('channel_id', '')
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    install_dir = installation.get('install_dir', '')
    if mirror_id and channel_id:
        result = config_manager.apply_full_config(mirror_id, channel_id)
        op_detail = f'镜像 {mirror_id} + 频道 {channel_id} -> {distro_id}'
    elif mirror_id:
        result = config_manager.switch_mirror(mirror_id)
        op_detail = f'切换镜像源 {mirror_id} -> {distro_id}'
    else:
        return JsonResponse({'success': False, 'message': '缺少 mirror_id 或 channel_id'}, status=400)
    if result.get('success'):
        cache.invalidate_version_cache(distro_id)
        cache.invalidate_envs_cache(distro_id)
        _invalidate_memory_versions(distro_id)
        log_operation('conda', op_detail, success=True)
    return JsonResponse(result)


@require_POST
def api_config_backup(request):
    """手动备份"""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)
    distro_id = data.get('distro_id', '')
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    install_dir = installation.get('install_dir', '')
    result = config_manager.backup()
    return JsonResponse(result)


@require_GET
def api_config_backups_list(request):
    """列出备份"""
    distro_id = request.GET.get('distro_id', '')
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    backups = config_manager.list_backups()
    return JsonResponse({'success': True, 'backups': backups})


@require_POST
def api_config_restore(request):
    """从备份恢复"""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)
    distro_id = data.get('distro_id', '')
    backup_path = data.get('backup_path', '')
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    install_dir = installation.get('install_dir', '')
    result = config_manager.restore(backup_path)
    if result.get('success'):
        cache.invalidate_version_cache(distro_id)
        cache.invalidate_envs_cache(distro_id)
        _invalidate_memory_versions(distro_id)
        log_operation('conda', f'恢复配置 {distro_id}', success=True, detail=backup_path)
    return JsonResponse(result)


@require_POST
def api_config_reset(request):
    """重置为空配置"""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)
    distro_id = data.get('distro_id', '')
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    install_dir = installation.get('install_dir', '')
    backup_result = config_manager.backup()
    reset_result = config_manager.reset()
    if reset_result.get('success'):
        reset_result['message'] += f'（已备份到 {backup_result.get("backup_path", "")}）'
        cache.invalidate_version_cache(distro_id)
        cache.invalidate_envs_cache(distro_id)
        _invalidate_memory_versions(distro_id)
        log_operation('conda', f'重置配置 {distro_id}', success=True)
    return JsonResponse(reset_result)
@require_POST
def api_config_clean_cache(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)
    distro_id = data.get('distro_id', '')
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    install_dir = installation.get('install_dir', '')
    return JsonResponse(config_manager.clean_index_cache())


@require_POST
def api_clean_download_cache(request):
    """清理 conda 安装包下载缓存目录（conda_download），下次下载自动重建。"""
    import shutil
    from .config import DOWNLOAD_DIR
    if not DOWNLOAD_DIR.is_dir():
        return JsonResponse({'success': True, 'freed_mb': 0, 'message': '无下载缓存'})
    total_bytes = sum(f.stat().st_size for f in DOWNLOAD_DIR.iterdir() if f.is_file())
    try:
        shutil.rmtree(DOWNLOAD_DIR)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'清理失败: {e}'})
    freed_mb = round(total_bytes / 1024 / 1024, 1)
    return JsonResponse({
        'success': True,
        'freed_mb': freed_mb,
        'message': f'已清理下载缓存，释放 {freed_mb} MB',
    })


@require_POST
def api_config_delete_backup(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)
    distro_id = data.get('distro_id', '')
    backup_path = data.get('backup_path', '')
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    install_dir = installation.get('install_dir', '')
    return JsonResponse(config_manager.delete_backup(backup_path))


# ── 包安装页面 + API ────────────────────────────────

class PkgInstallView(CondaMixin, TemplateView):
    """包安装页面（独立路由）"""
    template_name = 'conda/pkg_install.html'

    def get_context_data(self, distro_id=None, env_name=None, **kwargs):
        context = super().get_context_data(**kwargs)
        installation = storage.get_installation(record_id=distro_id)
        context['installation'] = installation
        context['distro_id'] = distro_id
        context['env_name'] = env_name or ''
        env_prefix = ''
        if env_name and installation:
            try:
                envs = conda_ops.list_envs(installation['conda_bat'])
                matched = next((e for e in envs if (e.get('name') or '').strip() == env_name), None)
                if matched:
                    env_prefix = matched['prefix']
            except Exception:
                pass
        context['env_prefix'] = env_prefix
        install_dir = installation.get('install_dir', '') if installation else ''
        env_norm = os.path.normpath(env_prefix) if env_prefix else ''
        is_base = bool(install_dir and env_norm and env_norm == os.path.normpath(install_dir))
        context['is_base_env'] = is_base
        context['page_title'] = f'安装包 - {installation.get("display_name", "")}' if installation else '安装包'

        if is_base:
            cat_data = {
                k: {
                    **v,
                    'packages': [p for p in v['packages'] if p.get('base_only')]
                }
                for k, v in PACKAGE_CATEGORIES.items()
            }
            bundle_data = {k: v for k, v in PACKAGE_BUNDLES.items() if v.get('base_only')}
            cat_data = {k: v for k, v in cat_data.items() if v['packages']}
        else:
            cat_data, bundle_data = PACKAGE_CATEGORIES, PACKAGE_BUNDLES

        context['package_categories'] = cat_data
        context['package_bundles'] = bundle_data
        context['package_categories_json'] = json.dumps(cat_data, ensure_ascii=False)
        context['package_bundles_json'] = json.dumps(bundle_data, ensure_ascii=False)
        context['breadcrumb'] = [
            {'title': '环境管理', 'href': reverse('conda:index'), 'active': False},
            {'title': '环境管理', 'href': reverse('conda:env_manage', args=[distro_id]), 'active': False},
            {'title': '安装包', 'href': None, 'active': True},
        ]
        return context


@require_GET
def api_search_packages(request):
    """模糊搜索包名"""
    distro_id = request.GET.get('distro_id', '').strip()
    keyword = request.GET.get('keyword', '').strip()
    if not keyword:
        return JsonResponse({'success': False, 'message': '请输入搜索关键词'}, status=400)

    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    result = conda_ops.search_packages(
        installation['conda_bat'], keyword,
    )
    return JsonResponse(result)


def api_install_pkg(request):
    """启动后台任务：安装包（JieTaskLog 流式）"""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)

    distro_id = data.get('distro_id', '').strip()
    env_prefix = conda_ops.normalize_env_prefix(data.get('env_prefix', ''))
    packages = data.get('packages', [])

    if not distro_id or not env_prefix:
        return JsonResponse({'success': False, 'message': '参数缺失'}, status=400)
    if not packages:
        return JsonResponse({'success': False, 'message': '未选择任何包'}, status=400)

    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    display_names = ', '.join(
        f"{p.get('name', '?')}{'=' + p['version'] if p.get('version') and p['version'] not in ('latest', '') else ''}"
        for p in packages
    )
    task = tasks.create_task('conda.install_pkg', meta={
        'distro_id': distro_id, 'env_prefix': env_prefix,
        'packages': packages,
        'installation': installation.get('display_name', ''),
    }, title=f'安装包: {display_names}')

    tasks.append_log(task, f'准备安装: {display_names}')
    tasks.run_in_thread(task, _install_pkg_worker,
                        installation['conda_bat'], env_prefix, packages, installation)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _install_pkg_worker(task: dict, conda_bat: str, env_prefix: str,
                        packages: list, installation: dict):
    """后台线程：流式执行 conda install"""

    def log_cb(line):
        tasks.append_log(task, line)

    display_names = ', '.join(p.get('name', '?') for p in packages)
    tasks.set_progress(task, stage=f'安装: {display_names}')

    result = conda_ops.install_packages_streaming(
        conda_bat, env_prefix, packages,
        log_cb=log_cb,
    )

    distro_id = installation.get('id', '')
    if result.get('success'):
        if distro_id:
            conda_ops.remove_pkg_snapshot(distro_id, env_prefix)
            cache.invalidate_envs_cache(distro_id)
        tasks.set_progress(task, progress=100, stage='完成')
        tasks.set_result(task, {'success': True, 'message': f'安装成功: {display_names}'})
        log_operation('conda', f'安装包 {display_names}', success=True,
                      detail=f'{installation.get("display_name", "")}')
    else:
        tasks.set_result(task, {'success': False, 'message': result.get('message', '安装失败')})
        log_operation('conda', f'安装包 {display_names}', success=False,
                      detail=result.get('message', ''))
        raise RuntimeError(result.get('message', '安装失败'))


@require_POST
def api_remove_pkg(request):
    """启动后台任务：卸载单个包（JieTaskLog 流式）"""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)

    distro_id = data.get('distro_id', '').strip()
    env_prefix = conda_ops.normalize_env_prefix(data.get('env_prefix', ''))
    package_name = data.get('package_name', '').strip()

    if not distro_id or not env_prefix or not package_name:
        return JsonResponse({'success': False, 'message': '参数缺失'}, status=400)

    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    if package_name.lower() in CORE_DEPENDENCIES:
        return JsonResponse({'success': False, 'message': f'{package_name} 是核心依赖包，禁止卸载。'}, status=400)

    task = tasks.create_task('conda.remove_pkg', meta={
        'distro_id': distro_id, 'env_prefix': env_prefix,
        'package_name': package_name,
        'installation': installation.get('display_name', ''),
    }, title=f'卸载包: {package_name}')

    tasks.append_log(task, f'准备卸载: {package_name}')
    tasks.run_in_thread(task, _remove_pkg_worker,
                        installation['conda_bat'], env_prefix, package_name, installation)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _remove_pkg_worker(task: dict, conda_bat: str, env_prefix: str,
                       package_name: str, installation: dict):
    """后台线程：流式执行 conda remove"""

    def log_cb(line):
        tasks.append_log(task, line)

    tasks.set_progress(task, stage=f'卸载: {package_name}')

    result = conda_ops.remove_package_streaming(
        conda_bat, env_prefix, package_name,
        log_cb=log_cb,
    )

    distro_id = installation.get('id', '')
    if result.get('success'):
        if distro_id:
            conda_ops.remove_pkg_snapshot(distro_id, env_prefix)
            cache.invalidate_envs_cache(distro_id)
        tasks.set_progress(task, progress=100, stage='完成')
        tasks.set_result(task, {'success': True, 'message': f'卸载成功: {package_name}'})
        log_operation('conda', f'卸载包 {package_name}', success=True,
                      detail=f'{installation.get("display_name", "")}')
    else:
        tasks.set_result(task, {'success': False, 'message': result.get('message', '卸载失败')})
        log_operation('conda', f'卸载包 {package_name}', success=False,
                      detail=result.get('message', ''))


def api_upgrade_pkg(request):
    """升级包（走 JieTaskLog 流式，同 install_pkg 模式）"""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)

    distro_id = data.get('distro_id', '').strip()
    env_prefix = conda_ops.normalize_env_prefix(data.get('env_prefix', ''))
    packages = data.get('packages', [])

    if not distro_id or not env_prefix:
        return JsonResponse({'success': False, 'message': '参数缺失'}, status=400)

    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    display_names = ', '.join(p.get('name', '?') for p in packages) if packages else '全部'
    task = tasks.create_task('conda.upgrade_pkg', meta={
        'distro_id': distro_id, 'env_prefix': env_prefix,
        'packages': packages,
        'installation': installation.get('display_name', ''),
    }, title=f'升级包: {display_names}')

    tasks.append_log(task, f'准备升级: {display_names}')
    tasks.run_in_thread(task, _upgrade_pkg_worker,
                        installation['conda_bat'], env_prefix, packages, installation)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _upgrade_pkg_worker(task: dict, conda_bat: str, env_prefix: str,
                        packages: list, installation: dict):
    """后台线程：conda upgrade -p <env_prefix> pkg... -y（或 conda upgrade --all）"""

    def log_cb(line):
        tasks.append_log(task, line)

    display_names = ', '.join(p.get('name', '?') for p in packages) if packages else '全部'
    tasks.set_progress(task, stage=f'升级: {display_names}')

    safe_env = conda_ops._safe_env()

    if packages:
        args = [conda_bat, 'upgrade', '-p', env_prefix] + \
               [p['name'] for p in packages] + ['-y']
    else:
        args = [conda_bat, 'upgrade', '-p', env_prefix, '--all', '-y']

    log_cb(f'执行: {" ".join(args)}')
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=conda_ops.CREATE_NO_WINDOW,
            env=safe_env, encoding='utf-8', errors='replace',
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip('\r\n')
            if line:
                log_cb(line)
        rc = proc.wait()
    except Exception as e:
        rc = -1
        log_cb(f'执行失败: {e}')

    tasks.append_log(task, f'conda 退出码: {rc}（0=成功）')

    distro_id = installation.get('id', '')
    if rc == 0:
        if distro_id:
            conda_ops.remove_pkg_snapshot(distro_id, env_prefix)
            cache.invalidate_envs_cache(distro_id)
        tasks.set_progress(task, progress=100, stage='完成')
        tasks.set_result(task, {'success': True, 'message': f'升级成功: {display_names}'})
        log_operation('conda', f'升级包 {display_names}', success=True,
                      detail=f'{installation.get("display_name", "")}')
    else:
        tasks.set_result(task, {'success': False, 'message': f'升级失败（退出码 {rc}）'})
        log_operation('conda', f'升级包 {display_names}', success=False,
                      detail=f'退出码 {rc}')
        raise RuntimeError(f'升级失败（退出码 {rc}）')


@require_GET
def api_check_conda_update(request):
    """异步检查 conda 是否有新版本（dry-run，不修改任何东西）。"""
    distro_id = request.GET.get('id', '').strip() or request.GET.get('distro_id', '').strip()
    if not distro_id:
        return JsonResponse({'success': False, 'message': '缺少 distro_id'}, status=400)
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)
    try:
        result = conda_ops.check_conda_update(installation['conda_bat'])
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)
    return JsonResponse({'success': True, **result})


@require_POST
def api_upgrade_conda(request):
    """升级 conda 本身（conda update -n base conda，后台任务）。"""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        data = {}
    distro_id = (data.get('distro_id') or data.get('id')
                 or request.POST.get('distro_id', '')
                 or request.POST.get('id', '')).strip()
    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    task = tasks.create_task('conda.upgrade_conda', meta={
        'distro_id': distro_id,
        'installation': installation.get('display_name', ''),
    }, title=f'升级 Conda - {installation.get("display_name", "")}')

    tasks.append_log(task, f'准备升级 Conda（{installation.get("display_name", "")}）...')
    tasks.run_in_thread(task, _upgrade_conda_worker, installation)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


def _upgrade_conda_worker(task: dict, installation: dict):
    """后台线程：流式升级 conda 本身。"""

    def log_cb(line):
        tasks.append_log(task, line)

    distro_id = installation.get('id', '')
    display_name = installation.get('display_name', '')
    install_dir = installation.get('install_dir', '')
    old_version = installation.get('version', '')

    tasks.set_progress(task, stage=f'升级 Conda...')
    result = conda_ops.upgrade_conda_streaming(installation['conda_bat'], log_cb=log_cb)

    # ── 升级后重新扫描注册表刷新版本号（无论成功失败）─────
    scan_results = detect.scan_registry()
    matched = next((r for r in scan_results
                    if os.path.normpath(r.get('install_dir', '')) == os.path.normpath(install_dir)), None)
    new_version = (matched or {}).get('version', '')
    if matched and distro_id:
        storage.upsert_installation({**matched, 'id': distro_id, 'is_external': installation.get('is_external', True)})
        log_cb(f'版本探测: {old_version or "?"} → {new_version or "?"}')

    if distro_id:
        cache.invalidate_version_cache(distro_id)
        cache.invalidate_envs_cache(distro_id)
        conda_ops.clean_pkg_snapshots_for_distro(distro_id)

    if result.get('success'):
        tasks.set_progress(task, progress=100, stage='完成')
        msg = f'Conda 升级成功（{old_version or "?"} → {new_version or old_version or "?"}）'
        tasks.set_result(task, {'success': True, 'message': msg})
        log_operation('conda', f'升级 Conda', success=True, detail=msg)
    else:
        tasks.set_result(task, {'success': False, 'message': result.get('message', '升级失败')})
        log_operation('conda', f'升级 Conda', success=False,
                      detail=result.get('message', ''))
        raise RuntimeError(result.get('message', '升级失败'))

def api_check_updates(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)

    distro_id = data.get('distro_id', '').strip()
    env_prefix = conda_ops.normalize_env_prefix(data.get('env_prefix', ''))

    if not distro_id or not env_prefix:
        return JsonResponse({'success': False, 'message': '参数缺失'}, status=400)

    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    updates = conda_ops.check_updates(installation['conda_bat'], env_prefix)
    return JsonResponse({'success': True, 'updates': updates})


@require_POST
def api_clone_env(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)

    distro_id = data.get('distro_id', '').strip()
    src_prefix = conda_ops.normalize_env_prefix(data.get('env_prefix', ''))
    new_name = data.get('new_name', '').strip()

    if not distro_id or not src_prefix or not new_name:
        return JsonResponse({'success': False, 'message': '参数缺失'}, status=400)

    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    existing = None
    if distro_id:
        existing = cache.get_envs_cache(distro_id)
    if existing is None:
        existing = conda_ops.list_envs(installation['conda_bat'])
        if distro_id:
            cache.set_envs_cache(distro_id, existing)
    for e in existing:
        if e.get('name') == new_name:
            return JsonResponse({'success': False, 'message': f'环境 {new_name} 已存在'})

    task = tasks.create_task(f'克隆环境: {src_prefix} → {new_name}', 'env')

    def worker(task):
        def log_cb(line):
            tasks.append_log(task, line)
        tasks.set_progress(task, stage=f'克隆 {new_name}')
        result = conda_ops.clone_env_streaming(
            installation['conda_bat'], src_prefix, new_name, log_cb)
        rc = result.get('returncode', -1)
        tasks.append_log(task, f'退出码: {rc}')
        if rc == 0:
            conda_ops.remove_pkg_snapshot(distro_id, os.path.join(os.path.dirname(src_prefix), new_name))
            cache.invalidate_envs_cache(distro_id)
            tasks.set_progress(task, progress=100, stage='完成')
            tasks.set_result(task, {'success': True, 'message': f'克隆成功: {new_name}'})
        else:
            tasks.set_result(task, {'success': False, 'message': f'克隆失败（退出码 {rc}）'})
            raise RuntimeError(f'克隆失败（退出码 {rc}）')

    tasks.run_in_thread(task, worker)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


@require_POST
def api_rename_env(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)

    distro_id = data.get('distro_id', '').strip()
    old_prefix = conda_ops.normalize_env_prefix(data.get('env_prefix', ''))
    new_name = data.get('new_name', '').strip()

    if not distro_id or not old_prefix or not new_name:
        return JsonResponse({'success': False, 'message': '参数缺失'}, status=400)

    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    existing = None
    if distro_id:
        existing = cache.get_envs_cache(distro_id)
    if existing is None:
        existing = conda_ops.list_envs(installation['conda_bat'])
        if distro_id:
            cache.set_envs_cache(distro_id, existing)
    for e in existing:
        if e.get('name') == new_name:
            return JsonResponse({'success': False, 'message': f'环境 {new_name} 已存在'})

    task = tasks.create_task(f'重命名环境: {old_prefix} → {new_name}', 'env')

    def worker(task):
        def log_cb(line):
            tasks.append_log(task, line)
        tasks.set_progress(task, stage=f'重命名 → {new_name}')
        result = conda_ops.rename_env_streaming(
            installation['conda_bat'], old_prefix, new_name, log_cb)
        rc = result.get('returncode', -1)
        tasks.append_log(task, f'退出码: {rc}')
        if rc == 0:
            conda_ops.remove_pkg_snapshot(distro_id, old_prefix)
            cache.invalidate_envs_cache(distro_id)
            tasks.set_progress(task, progress=100, stage='完成')
            tasks.set_result(task, {'success': True, 'message': f'重命名成功: {new_name}'})
        else:
            tasks.set_result(task, {'success': False, 'message': f'重命名失败（退出码 {rc}）'})
            raise RuntimeError(f'重命名失败（退出码 {rc}）')

    tasks.run_in_thread(task, worker)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


@require_POST
def api_export_env_yml(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)

    distro_id = data.get('distro_id', '').strip()
    env_prefix = conda_ops.normalize_env_prefix(data.get('env_prefix', ''))

    if not distro_id or not env_prefix:
        return JsonResponse({'success': False, 'message': '参数缺失'}, status=400)

    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    result = conda_ops.export_env_yml(installation['conda_bat'], env_prefix)
    rc = result.get('returncode', -1)
    stdout = result.get('stdout', '')
    if rc != 0:
        stderr = result.get('stderr', '')
        return JsonResponse({'success': False, 'message': f'导出失败: {stderr or stdout}'})

    temp_dir = config.DATA_DIR / 'temp'
    temp_dir.mkdir(exist_ok=True)
    env_name = os.path.basename(env_prefix.rstrip('\\/')) or 'environment'
    yml_path = temp_dir / f'{env_name}_{int(time.time())}.yml'
    try:
        yml_path.write_text(stdout, encoding='utf-8')
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'保存 yml 失败: {e}'})

    download_url = reverse('conda:api_download_yml', args=[yml_path.name])
    return JsonResponse({
        'success': True,
        'yml_content': stdout,
        'download_url': download_url,
        'filename': yml_path.name,
    })


@require_GET
def api_download_yml(request, filename):
    from django.http import FileResponse
    temp_dir = config.DATA_DIR / 'temp'
    filepath = temp_dir / filename
    if not filepath.exists():
        return JsonResponse({'success': False, 'message': '文件不存在'}, status=404)
    response = FileResponse(
        filepath.open('rb'), as_attachment=True,
        filename=os.path.basename(filename))
    return response


@require_POST
def api_import_env_yml(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': '请求体解析失败'}, status=400)

    distro_id = data.get('distro_id', '').strip()
    yml_path = data.get('yml_path', '').strip()
    env_name = data.get('env_name', '').strip()

    if not distro_id or not yml_path:
        return JsonResponse({'success': False, 'message': '参数缺失'}, status=400)

    installation = storage.get_installation(record_id=distro_id)
    if not installation:
        return JsonResponse({'success': False, 'message': '未找到发行版'}, status=404)

    if not os.path.isabs(yml_path):
        yml_path = str(config.DATA_DIR / 'temp' / yml_path)
    if not Path(yml_path).exists():
        return JsonResponse({'success': False, 'message': 'yml 文件不存在'}, status=400)

    task = tasks.create_task(f'导入环境: {yml_path}', 'env')

    def worker(task):
        def log_cb(line):
            tasks.append_log(task, line)
        tasks.set_progress(task, stage=f'导入 {env_name or "环境"}')
        result = conda_ops.create_env_from_yml_streaming(
            installation['conda_bat'], yml_path, env_name, log_cb)
        rc = result.get('returncode', -1)
        tasks.append_log(task, f'退出码: {rc}')
        if rc == 0:
            cache.invalidate_envs_cache(distro_id)
            tasks.set_progress(task, progress=100, stage='完成')
            tasks.set_result(task, {'success': True, 'message': f'导入成功: {env_name or "环境"}'})
        else:
            tasks.set_result(task, {'success': False, 'message': f'导入失败（退出码 {rc}）'})
            raise RuntimeError(f'导入失败（退出码 {rc}）')

    tasks.run_in_thread(task, worker)
    return JsonResponse({'success': True, 'task_id': task['task_id']})


@require_POST
def api_upload_yml(request):
    from django.core.files.uploadedfile import InMemoryUploadedFile
    temp_dir = config.DATA_DIR / 'temp'
    temp_dir.mkdir(exist_ok=True)

    file = request.FILES.get('file')
    if not file:
        return JsonResponse({'success': False, 'message': '未收到文件'}, status=400)

    filename = file.name or 'environment.yml'
    safe_name = f"{os.path.splitext(filename)[0]}_{int(time.time())}.yml"
    filepath = temp_dir / safe_name
    try:
        with open(filepath, 'wb+') as f:
            for chunk in file.chunks():
                f.write(chunk)
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'保存失败: {e}'})

    return JsonResponse({
        'success': True,
        'filename': safe_name,
        'yml_path': str(filepath),
    })