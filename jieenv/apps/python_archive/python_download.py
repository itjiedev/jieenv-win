"""
Python 压缩包下载源管理。

职责：
- 维护内置镜像源列表 DOWNLOAD_SOURCES（南京大学、华为云、清华大学、淘宝、官方）；
- 持久化用户选择的默认下载源到 DATA_DIR/download_source.json；
- 按版本信息拼接下载直链（仅对 zip 包应用镜像替换）；
- 在实际下载前用 HEAD 请求探测源可用性，不可用时自动回退到下一个源，
  最终兜底到官方源，保证下载链路尽量不中断。

对外主要接口：
    list_sources()                     列出全部可选源（含"自动探测"）
    get_default_source()               读取持久化的默认源（None 表示自动）
    set_default_source(name)           写入默认源；name='auto' 清除为自动
    build_zip_url(base_url, version)   拼接下载直链（仅对 zip 包应用镜像）
    resolve_download(version_id, ...)  综合"临时指定 > 默认 > 自动探测"解析出
                                       最终 {source, download_url, mode}
"""
import json
import logging
import os
import urllib.request
import urllib.error
from typing import Optional

from . import storage
from .config import DOWNLOAD_SOURCE_FILE

logger = logging.getLogger(__name__)

# 下载/探测专用请求头。
# 注意：不能用抓 HTML 版本页的 BROWSER_HEADERS（含浏览器 User-Agent）。
# 实测南京大学镜像对浏览器 UA 返回 302 且 Location 指向同一 URL（无限重定向循环），
# 清华 TUNA 镜像对浏览器 UA 直接返回 403；二者对中性 UA（如 curl/Python-urllib）均正常 200。
# 故下载二进制压缩包与 HEAD 探测统一使用下面这个非浏览器 UA。
DOWNLOAD_USER_AGENT = 'jieEnv-python-archive/1.0'
DOWNLOAD_HEADERS = {
    'User-Agent': DOWNLOAD_USER_AGENT,
    'Accept': '*/*',
}

# 官方源 URL 前缀，build_zip_url 会把该前缀替换为镜像根地址。
OFFICIAL_URL_PREFIX = 'https://www.python.org/ftp/python'

# 内置下载源，顺序即自动探测的优先级：镜像站放前面（国内快），官方兜底。
# 所有镜像都遵循与官方 ftp/python 相同的目录布局，即 <base>/<version>/python-<version>-amd64.<ext>。
# 注意：build_zip_url 仅对 ext == 'zip' 的版本应用镜像替换，其他格式一律使用官方地址；
# 相应地，resolve_download 在非 zip 格式下会把 source 也回落为官方源。
DOWNLOAD_SOURCES = [
    {'name': '南京大学', 'url': 'https://mirror.nju.edu.cn/python/'},
    {'name': '华为云',   'url': 'https://mirrors.huaweicloud.com/python/'},
    {'name': '清华大学', 'url': 'https://mirrors.tuna.tsinghua.edu.cn/python/'},
    {'name': '淘宝',     'url': 'https://cdn.npmmirror.com/binaries/python/'},
    {'name': '官方',     'url': 'https://www.python.org/ftp/python/'},
]

# 兜底官方源名，避免依赖 DOWNLOAD_SOURCES 的顺序。
OFFICIAL_SOURCE_NAME = '官方'

# 特殊源名：自动探测（不持久化具体镜像，运行时按顺序 HEAD 探测）
AUTO_SOURCE_NAME = 'auto'
_AUTO_ENTRY = {'name': AUTO_SOURCE_NAME, 'display_name': '自动探测', 'url': ''}

# HEAD 探测超时（秒）——镜像站偶尔抖动，短超时快速切换到下一个
PROBE_TIMEOUT = 6


# ── 源列表 / 持久化 ────────────────────────────────

def list_sources() -> list:
    """返回全部可选下载源（首项固定为"自动探测"，其后为内置镜像）。

    每项形如 {name, display_name, url}；display_name 供前端下拉展示，
    自动探测项 url 为空字符串。
    """
    items = [dict(_AUTO_ENTRY)]
    for s in DOWNLOAD_SOURCES:
        items.append({
            'name': s['name'],
            'display_name': s['name'],
            'url': s['url'],
        })
    return items


def _find_source_by_name(name: str):
    """按 name 在内置镜像中查找源 dict，未命中返回 None。"""
    if not name:
        return None
    for s in DOWNLOAD_SOURCES:
        if s['name'] == name:
            return dict(s)
    return None


def _official_source() -> dict:
    """返回官方源 dict（找不到时兜底为列表最后一项）。"""
    for s in DOWNLOAD_SOURCES:
        if s['name'] == OFFICIAL_SOURCE_NAME:
            return dict(s)
    return dict(DOWNLOAD_SOURCES[-1])


def get_default_source() -> Optional[dict]:
    """读取持久化的默认下载源。

    返回 dict {name, url, display_name}；文件不存在、损坏或 name='auto'
    时返回 None，表示"自动探测"模式。
    """
    if not DOWNLOAD_SOURCE_FILE.exists():
        return None
    try:
        data = json.loads(DOWNLOAD_SOURCE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        logger.warning('下载源配置读取失败: %s', e)
        return None
    if not isinstance(data, dict):
        return None
    name = data.get('name') or ''
    if not name or name == AUTO_SOURCE_NAME:
        return None
    src = _find_source_by_name(name)
    if src:
        return {
            'name': src['name'],
            'url': src['url'],
            'display_name': src['name'],
        }
    # 配置里存的名字已不在内置列表中（例如镜像下线），按自动处理
    logger.warning('下载源配置中的 %s 已不在内置列表，回退为自动探测', name)
    return None


def set_default_source(name: str) -> dict:
    """持久化默认下载源。

    name='auto' 或空字符串时写入 {name:'auto'} 表示恢复自动探测；
    其它值必须是 DOWNLOAD_SOURCES 中的合法名字，否则抛 ValueError。
    返回写入后的 dict（含 name / url / display_name）。
    """
    name = (name or '').strip()
    if not name or name == AUTO_SOURCE_NAME:
        payload = {'name': AUTO_SOURCE_NAME, 'url': ''}
        display_name = '自动探测'
    else:
        src = _find_source_by_name(name)
        if not src:
            raise ValueError(f'未知的下载源: {name}')
        payload = {'name': src['name'], 'url': src['url']}
        display_name = src['name']
    try:
        DOWNLOAD_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再 os.replace，避免写入中途失败留下半截配置。
        tmp_path = DOWNLOAD_SOURCE_FILE.with_suffix(DOWNLOAD_SOURCE_FILE.suffix + '.tmp')
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp_path, DOWNLOAD_SOURCE_FILE)
    except OSError as e:
        logger.exception('下载源配置写入失败')
        raise RuntimeError(f'保存下载源配置失败: {e}')
    return {
        'name': payload['name'],
        'url': payload['url'],
        'display_name': display_name,
    }


# ── URL 构造 / 探测 ────────────────────────────────

def build_zip_url(base_url: str, version: dict):
    """
    按镜像根地址拼接下载直链。

    仅当 version['ext'] == 'zip' 且 version['url'] 以官方前缀
    OFFICIAL_URL_PREFIX 开头时，把该前缀替换为 base_url；
    其他情况（非 zip 格式、或 URL 不是官方前缀）直接返回 version['url']。
    """
    if not base_url:
        return version['url']
    if version.get('ext') != 'zip':
        return version['url']
    url = version['url']
    if not url.startswith(OFFICIAL_URL_PREFIX):
        # URL 不是预期的官方前缀，镜像替换无从下手，保留原地址
        return url
    base_url = base_url.rstrip('/')
    return base_url + url[len(OFFICIAL_URL_PREFIX):]


def probe_url(url: str, timeout: int = PROBE_TIMEOUT) -> bool:
    """HEAD 探测 URL 是否可达（2xx/3xx 视为可用）。

    部分镜像对 HEAD 返回 405 但 GET 正常，遇到 405 时降级为 Range GET
    再试一次；仍失败才判为不可用。网络异常一律返回 False，不抛出。
    统一使用 DOWNLOAD_HEADERS（非浏览器 UA），避免镜像对浏览器 UA 的 302/403 拦截。
    """
    req = urllib.request.Request(url, headers=DOWNLOAD_HEADERS, method='HEAD')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as e:
        if e.code == 405:
            return _probe_with_range_get(url, timeout)
        logger.debug('HEAD 探测 %s 返回 HTTP %s', url, e.code)
        return False
    except Exception as e:
        logger.debug('HEAD 探测 %s 失败: %s', url, e)
        return False


def _probe_with_range_get(url: str, timeout: int) -> bool:
    headers = dict(DOWNLOAD_HEADERS)
    headers['Range'] = 'bytes=0-0'
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception as e:
        logger.debug('Range GET 探测 %s 失败: %s', url, e)
        return False


# ── 综合解析 ──────────────────────────────────────

def _effective_source(version: dict, src: dict) -> dict:
    """返回与 build_zip_url 结果一致的 source。

    非 zip 格式下 build_zip_url 直接返回官方地址，镜像替换不生效，
    因此这里同步把 source 回落为官方源，避免 source 与实际 download_url 不一致。
    """
    if version.get('ext') != 'zip' and src.get('name') != OFFICIAL_SOURCE_NAME:
        return _official_source()
    return src


def resolve_download(version_id: str,
                     preferred_name: Optional[str] = None,
                     on_progress=None) -> dict:
    """按"临时指定 > 持久化默认 > 自动探测"解析出最终下载源与下载直链。

    参数：
        version_id      缓存的信息 id
        preferred_name  本次安装临时指定的源名（None/'auto' 表示不指定）
        on_progress     可选回调，接收探测过程的可读日志字符串

    返回 dict：
        {
            'source': {'name', 'url'},      # 最终选中的源
                                            # 非 zip 格式下固定为官方源
            'download_url': 'https://...',  # 下载直链
            'mode': 'preferred' | 'default' | 'auto' | 'fallback',
        }

    解析策略：
    1. preferred_name 有效 → 直接用它（不探测，尊重用户显式选择）；
    2. 否则读持久化默认源 → 用它（不探测）；
    3. 否则按 DOWNLOAD_SOURCES 顺序 HEAD 探测，第一个可达的即用；
    4. 全部不可达 → 兜底用官方源（可能网络受限，但让下载去报错比这里
       直接失败更直观，用户能看到真实的 HTTP 错误）。

    补充：
    非 zip 格式（如 exe 安装包）不走镜像，build_zip_url 返回官方地址，
    source 也会相应回落为官方源；mode 仍按解析路径标记。
    """
    def _log(msg):
        if on_progress:
            on_progress(msg)

    version = storage.get_version_by_id(version_id)
    if not version:
        raise ValueError(f'未知版本: {version_id}')

    # 1. 临时指定
    if preferred_name and preferred_name != AUTO_SOURCE_NAME:
        src = _find_source_by_name(preferred_name)
        if src:
            effective = _effective_source(version, src)
            _log(f'使用本次指定的下载源：{effective["name"]}')
            return {
                'source': effective,
                'download_url': build_zip_url(src['url'], version),
                'mode': 'preferred',
            }
        _log(f'指定的下载源 {preferred_name} 不存在，回退到默认/自动')

    # 2. 持久化默认
    default = get_default_source()
    if default:
        effective = _effective_source(version, default)
        _log(f'使用已保存的默认下载源：{effective["name"]}')
        return {
            'source': effective,
            'download_url': build_zip_url(default['url'], version),
            'mode': 'default',
        }

    # 3. 自动探测：按 DOWNLOAD_SOURCES 顺序 HEAD 探测，第一个可达的即用
    _log('未设置默认下载源，按顺序探测可用下载源...')
    for src in DOWNLOAD_SOURCES:
        download_url = build_zip_url(src['url'], version)
        if probe_url(download_url):
            effective = _effective_source(version, src)
            _log(f'探测到可用下载源：{effective["name"]}')
            return {'source': effective, 'download_url': download_url, 'mode': 'auto'}

    # 4. 全部不可达，兜底官方
    official = _official_source()
    download_url = build_zip_url(official['url'], version)
    _log(f'所有镜像均不可达，兜底使用官方源：{download_url}')
    return {'source': official, 'download_url': download_url, 'mode': 'fallback'}