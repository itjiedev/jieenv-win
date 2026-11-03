"""
全局 PyPI 源配置模块

通过项目 runtime 环境的 python.exe 执行 `pip config` 命令，读取 / 设置 / 清除
全局（user 级）pip 镜像源，实现"一处设置，全局生效"。

pip config 机制说明:
- 配置写入 user 级配置文件（Windows: %APPDATA%\\pip\\pip.ini）
- `global.index-url` 对应配置文件 [global] 段的 index-url 键
- 对当前 Windows 用户下的所有 pip 调用生效（虚拟环境内独立配置除外）

使用方式::

    from apps.python_main import pypi_config

    pypi_config.get_effective_source()          # 当前生效源信息
    pypi_config.set_index_url(url)              # 设置全局源
    pypi_config.unset_index_url()               # 恢复官方默认源
    pypi_config.get_all_sources()               # 内置源 + 自定义源合并列表
    pypi_config.add_custom_source(name, url)    # 添加自定义源（JSON 持久化）
    pypi_config.remove_custom_source(src_id)    # 删除自定义源
"""
import os
import sys
import uuid
import logging
import subprocess
from pathlib import Path
from datetime import datetime

from django.conf import settings

from jiecore.json_storage import JsonStore
from .config import PYPI_CUSTOM_SOURCES_FILE

logger = logging.getLogger(__name__)

# 子进程隐藏窗口标志
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 官方默认源（pip 内置默认，未自定义时使用）
OFFICIAL_INDEX_URL = 'https://pypi.org/simple'

# 常见国内 PyPI 镜像源（均为 https，无需额外 trusted-host）
COMMON_SOURCES = [
    {'name': '官方默认', 'url': OFFICIAL_INDEX_URL},
    {'name': '清华大学', 'url': 'https://pypi.tuna.tsinghua.edu.cn/simple'},
    {'name': '阿里云', 'url': 'https://mirrors.aliyun.com/pypi/simple/'},
    {'name': '腾讯云', 'url': 'https://mirrors.cloud.tencent.com/pypi/simple'},
    {'name': '中科大', 'url': 'https://pypi.mirrors.ustc.edu.cn/simple/'},
    {'name': '华为云', 'url': 'https://repo.huaweicloud.com/repository/pypi/simple'},
    {'name': '南京大学', 'url': 'https://mirror.nju.edu.cn/pypi/web/simple'},
    {'name': '豆瓣', 'url': 'https://pypi.doubanio.com/simple/'},
    {'name': '网易', 'url': 'https://mirrors.163.com/pypi/simple/'},
]

# 自定义源存储（用户手动添加的源，持久化到 JSON 文件）
_custom_store = JsonStore(PYPI_CUSTOM_SOURCES_FILE, default=[])


def get_runtime_python_exe() -> str:
    """
    获取项目 runtime 环境的 python.exe 路径。

    优先使用 ENV_ROOT/runtime/python.exe；不存在时（如开发态用系统解释器启动）
    回退到当前运行的解释器 sys.executable，保证功能可用。
    """
    env_root = getattr(settings, 'ENV_ROOT', None)
    if env_root:
        runtime_exe = Path(env_root) / 'runtime' / 'python.exe'
        if runtime_exe.exists():
            return str(runtime_exe)
    return sys.executable


def _run_pip_config(args: list, timeout: int = 30) -> tuple:
    """
    通过 runtime python.exe 执行 `pip config <args>` 命令。
    返回 (returncode, stdout, stderr)，输出均已 strip。
    """
    python_exe = get_runtime_python_exe()
    try:
        proc = subprocess.run(
            [python_exe, '-m', 'pip', 'config'] + args,
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        out = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
        err = (proc.stderr or b'').decode('utf-8', errors='replace').strip()
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        return 1, '', '命令执行超时'
    except Exception as e:
        logger.exception('执行 pip config 失败')
        return 1, '', str(e)


def get_current_index_url() -> str:
    """
    获取当前全局 index-url。
    未设置时返回空字符串（表示使用 pip 内置默认，即官方源）。
    """
    code, out, err = _run_pip_config(['get', 'global.index-url'])
    # 成功时 stdout 为源地址；未设置时输出 "ERROR: No such key - global.index-url"
    if code == 0 and out and not out.upper().startswith('ERROR'):
        return out
    return ''


def get_effective_source() -> dict:
    """
    获取当前生效的源信息（供页面展示）。
    返回 {url, name, is_custom}
      - url:       当前生效的源地址（未自定义时为官方源）
      - name:      匹配到的常见源名称，自定义源为 '自定义源'，未设置为 '官方默认'
      - is_custom: 是否已自定义（写入过 global.index-url）
    """
    url = get_current_index_url()
    if not url:
        return {'url': OFFICIAL_INDEX_URL, 'name': '官方默认', 'is_custom': False}

    # 名称匹配：先查内置源，再查已保存的自定义源
    name = ''
    for s in COMMON_SOURCES:
        if s['url'].rstrip('/') == url.rstrip('/'):
            name = s['name']
            break
    if not name:
        for s in _custom_store.all():
            if s.get('url', '').rstrip('/') == url.rstrip('/'):
                name = s.get('name') or '自定义源'
                break
    return {'url': url, 'name': name or '自定义源', 'is_custom': True}


def set_index_url(url: str) -> dict:
    """
    设置全局 index-url（写入 user 级 pip.ini）。
    返回 {success, message}
    """
    url = (url or '').strip()
    if not url:
        return {'success': False, 'message': '源地址不能为空'}
    if not (url.startswith('http://') or url.startswith('https://')):
        return {'success': False, 'message': '源地址必须以 http:// 或 https:// 开头'}

    code, out, err = _run_pip_config(['set', 'global.index-url', url])
    if code == 0:
        logger.info('设置全局 PyPI 源: %s', url)
        return {'success': True, 'message': f'已设置全局源: {url}'}
    return {'success': False, 'message': f'设置失败: {err or out}'[:200]}


def unset_index_url() -> dict:
    """
    清除全局 index-url（恢复 pip 内置默认，即官方源）。
    返回 {success, message}
    """
    code, out, err = _run_pip_config(['unset', 'global.index-url'])
    if code == 0:
        logger.info('已清除全局 PyPI 源，恢复官方默认')
        return {'success': True, 'message': '已恢复官方默认源'}
    # 原本就未设置时，unset 会报 "No such key"，同样视为已处于默认状态
    if 'No such key' in (err or '') or 'No such key' in (out or ''):
        return {'success': True, 'message': '当前已是官方默认源'}
    return {'success': False, 'message': f'恢复失败: {err or out}'[:200]}


def get_config_debug() -> dict:
    """
    获取 pip config 各级别配置文件路径及生效值（调试 / 详情展示用）。
    返回 {success, output}
    """
    code, out, err = _run_pip_config(['debug'])
    return {'success': code == 0, 'output': out or err}


# ── 自定义源管理（JSON 持久化）────────────────

def list_custom_sources() -> list:
    """列出所有已保存的自定义源"""
    return _custom_store.all()


def get_all_sources() -> list:
    """
    返回内置源 + 自定义源的合并列表（供页面展示）。
    每项: {name, url, builtin, id}
      - builtin=True  内置常见源（不可删除），id 为空
      - builtin=False 用户自定义源（可删除），id 为记录主键
    """
    result = [{'name': s['name'], 'url': s['url'], 'builtin': True, 'id': ''}
              for s in COMMON_SOURCES]
    for s in _custom_store.all():
        result.append({'name': s.get('name', ''), 'url': s.get('url', ''),
                       'builtin': False, 'id': s.get('id', '')})
    return result


def add_custom_source(name: str, url: str) -> dict:
    """
    添加自定义源（持久化到 JSON）。名称为空时用 URL 兜底。
    返回 {success, message, source?}
    """
    name = (name or '').strip()
    url = (url or '').strip()
    if not url:
        return {'success': False, 'message': '源地址不能为空'}
    if not (url.startswith('http://') or url.startswith('https://')):
        return {'success': False, 'message': '源地址必须以 http:// 或 https:// 开头'}
    if not name:
        name = url
    # 查重：与内置源或已有自定义源 URL 重复则拒绝
    norm = url.rstrip('/')
    for s in COMMON_SOURCES:
        if s['url'].rstrip('/') == norm:
            return {'success': False, 'message': f'该源已存在于内置列表（{s["name"]}）'}
    for s in _custom_store.all():
        if s.get('url', '').rstrip('/') == norm:
            return {'success': False, 'message': '该自定义源已存在'}
    record = {
        'id': uuid.uuid4().hex[:12],
        'name': name,
        'url': url,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    _custom_store.add(record)
    logger.info('添加自定义 PyPI 源: %s (%s)', name, url)
    return {'success': True, 'message': f'已添加自定义源: {name}', 'source': record}


def remove_custom_source(source_id: str) -> dict:
    """
    删除自定义源。
    返回 {success, message}
    """
    source_id = (source_id or '').strip()
    if not source_id:
        return {'success': False, 'message': '缺少源 ID'}
    if _custom_store.remove(source_id):
        logger.info('删除自定义 PyPI 源: %s', source_id)
        return {'success': True, 'message': '已删除自定义源'}
    return {'success': False, 'message': '自定义源不存在'}
