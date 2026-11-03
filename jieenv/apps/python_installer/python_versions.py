"""
抓取 Python 官网版本列表。

数据源: https://www.python.org/downloads/windows/
仅解析页面中 "Windows installer (64-bit)" 下载链接，提取版本号与直链。

版本列表缓存：成功抓取后持久化到 data/python_installer_versions.json，
之后默认直接读本地缓存，仅在显式要求重新抓取时才联网官网。
"""
import re
import gzip
import json
import zlib
import logging
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

DOWNLOADS_PAGE = 'https://www.python.org/downloads/windows/'
VERSION_DETAIL_TPL = 'https://www.python.org/downloads/release/python-{ver_no_dots}/'

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

BROWSER_HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
}

FETCH_TIMEOUT = 20

# 抓取的最低版本（主.次）：3.5.0 是首个采用现代 Windows 安装器、
# 支持 /quiet、TargetDir、Include_pip、PrependPath 等静默自动安装选项的版本，
# 低于此版本无法保证自动安装，故以“可提供自动安装的版本”为下限。
MIN_VERSION = (3, 5)

# 默认返回的版本数量上限（需足够大以覆盖 MIN_VERSION 以上的全部版本）
DEFAULT_LIMIT = 200

# 兜底版本列表
FALLBACK_VERSIONS = [
    '3.13.5', '3.13.4', '3.13.3', '3.13.2', '3.13.1', '3.13.0',
    '3.12.10', '3.12.9', '3.12.8', '3.12.7', '3.12.6',
    '3.11.12', '3.11.11', '3.11.10', '3.11.9',
    '3.10.17', '3.10.16', '3.10.15',
]

# 匹配 Windows 64-bit 可执行安装包（-amd64.exe）直链，提取版本号。
# 新版页面链接文字为 "Windows installer (64-bit)"，旧版为 "Windows x86-64 executable installer"，
# 两者都指向 python-<ver>-amd64.exe，即支持静默自动安装的可执行安装包；
# web-based 安装器（-amd64-webinstall.exe）与 embeddable zip 不在匹配范围内。
_WIN64_LINK_RE = re.compile(
    r'href="https://www\.python\.org/ftp/python/(\d+\.\d+\.\d+)/python-\1-amd64\.exe"',
    re.IGNORECASE)


def _fetch_html(url: str) -> str:
    """抓取页面 HTML，自动处理 gzip/deflate 压缩响应。

    python.org 无论请求是否声明 Accept-Encoding 都会返回 gzip 压缩内容，
    而 urllib 不会自动解压，直接按文本解码会得到乱码，导致解析不到任何版本。
    """
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read()
        encoding = (resp.headers.get('Content-Encoding') or '').lower()
    if raw[:2] == b'\x1f\x8b' or 'gzip' in encoding:
        raw = gzip.decompress(raw)
    elif 'deflate' in encoding:
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode('utf-8', errors='replace')


def _parse_versions(html: str) -> list:
    """从 Windows 下载页 HTML 中提取提供 64-bit 可执行安装包的版本号列表。

    仅保留指向 python-<ver>-amd64.exe 的自动安装包（兼容新旧两种链接命名），
    天然满足 "仅针对 windows 的 installer 64-bit" 的要求；并过滤低于 MIN_VERSION 的版本。
    """
    versions = []
    for match in _WIN64_LINK_RE.finditer(html):
        ver = match.group(1)
        if _version_key(ver)[:2] < MIN_VERSION:
            continue
        if ver not in versions:
            versions.append(ver)
    return versions


def _version_key(ver: str):
    return tuple(int(x) for x in ver.split('.'))


def build_version_entry(version: str) -> dict:
    """构造版本条目，包含 MSI 安装包直链和 py launcher MSI 直链"""
    ver_no_dots = version.replace('.', '')
    major_minor = '.'.join(version.split('.')[:2])
    return {
        'version': version,
        'major_minor': major_minor,
        'detail_url': VERSION_DETAIL_TPL.format(ver_no_dots=ver_no_dots),
        'msi_url': f'https://www.python.org/ftp/python/{version}/python-{version}-amd64.exe',
        'launcher_url': f'https://www.python.org/ftp/python/{version}/win32/launcher.msi',
    }


def fetch_python_versions(limit: int = DEFAULT_LIMIT) -> dict:
    """
    抓取 Python 官网版本列表。
    返回 {success, source, message, versions}
    """
    try:
        html = _fetch_html(DOWNLOADS_PAGE)
        versions = _parse_versions(html)
    except Exception as e:
        logger.warning('抓取 Python 版本列表失败: %s', e)
        versions = []

    if not versions:
        entries = [build_version_entry(v) for v in FALLBACK_VERSIONS]
        return {
            'success': True,
            'source': 'fallback',
            'message': '官网抓取失败，已使用内置版本列表',
            'versions': entries[:limit],
        }

    versions.sort(key=_version_key, reverse=True)
    entries = [build_version_entry(v) for v in versions]
    return {
        'success': True,
        'source': 'website',
        'message': '抓取成功',
        'versions': entries[:limit],
    }


# ── 版本列表本地 JSON 缓存 ──────────────────────────

from .config import VERSION_CACHE_FILE


def _load_version_cache() -> dict:
    if not VERSION_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(VERSION_CACHE_FILE.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('versions'), list) and data['versions']:
            return data
    except (OSError, ValueError) as e:
        logger.warning('版本列表缓存读取失败: %s', e)
    return None


def _save_version_cache(result: dict):
    try:
        VERSION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'fetched_at': result.get('fetched_at', ''),
            'source': result.get('source', ''),
            'versions': result.get('versions', []),
        }
        VERSION_CACHE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as e:
        logger.warning('版本列表缓存保存失败: %s', e)


def get_python_versions(refresh: bool = False, limit: int = DEFAULT_LIMIT) -> dict:
    """
    获取版本列表（优先本地缓存）。
    refresh=False 且缓存存在时直接返回缓存；否则联网抓取。
    """
    if not refresh:
        cached = _load_version_cache()
        if cached:
            return {
                'success': True,
                'source': 'cache',
                'fetched_at': cached.get('fetched_at', ''),
                'message': f"本地缓存（抓取于 {cached.get('fetched_at', '未知时间')}）",
                'versions': cached['versions'][:limit],
            }
    result = fetch_python_versions(limit)
    result['fetched_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if result.get('versions'):
        _save_version_cache(result)
    return result
