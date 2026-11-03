"""
抓取 MySQL 官网社区版（Community Server）版本列表。

数据源：
1. 当前 GA 版本: https://dev.mysql.com/downloads/mysql/
   页面含 "Select Version:" 下拉框，直接列出当前可用的 GA 版本；
   下载地址: https://cdn.mysql.com/Downloads/MySQL-<主.次>/mysql-<版本>-winx64.zip
2. 历史归档版本: https://downloads.mysql.com/archives/community/
   页面含 version 下拉框，列出全部历史版本；
   下载地址: https://downloads.mysql.com/archives/get/p/23/file/mysql-<版本>-winx64.zip

只保留正式版（纯 x.y.z 版本号，排除 rc/dm/m/alpha 等预览版），
且只保留 5.5 及以上版本（更早版本不提供 winx64 ZIP 包）。
抓取失败时返回内置兜底列表，保证离线也能看到可选项。

版本列表缓存：成功抓取后持久化到 data/dbmysql_versions.json，
之后默认直接读本地缓存，仅在缓存文件不存在或显式要求重新抓取时
才联网官网（get_mysql_versions）。
"""
import re
import json
import logging
import urllib.request
from datetime import datetime

from django.conf import settings

logger = logging.getLogger(__name__)

CURRENT_PAGE = 'https://dev.mysql.com/downloads/mysql/'
ARCHIVES_PAGE = 'https://downloads.mysql.com/archives/community/'

CDN_URL_TPL = 'https://cdn.mysql.com/Downloads/MySQL-{major}/mysql-{version}-winx64.zip'
ARCHIVE_URL_TPL = 'https://downloads.mysql.com/archives/get/p/23/file/mysql-{version}-winx64.zip'
# MSI 安装包直链：当前版在 CDN，历史版在 CDN archives（与 zip 同目录）
CDN_MSI_TPL = 'https://cdn.mysql.com/Downloads/MySQL-{major}/mysql-{version}-winx64.msi'
ARCHIVE_MSI_TPL = 'https://cdn.mysql.com/archives/mysql-{major}/mysql-{version}-winx64.msi'

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

# MySQL 官网有 WAF，需要完整模拟浏览器请求头，否则返回 403
BROWSER_HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
               'image/avif,image/webp,*/*;q=0.8'),
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'identity',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Cache-Control': 'max-age=0',
}

FETCH_TIMEOUT = 20

# 抓取失败时的兜底版本列表
FALLBACK_VERSIONS = ['9.7.2', '9.7.1', '9.6.0', '9.5.0', '9.4.0', '9.3.0', '9.2.0',
                     '9.1.0', '9.0.1', '8.4.11', '8.4.9', '8.4.7', '8.4.5', '8.4.3',
                     '8.4.0', '8.0.46', '8.0.45', '8.0.43', '8.0.41', '8.0.39',
                     '8.0.36', '5.7.44', '5.7.42', '5.7.40']

# 版本下拉框 option 解析（value + 文本）：当前 GA 页的 value 是版本系列（如 8.4），
# 精确版本号在 option 文本中（如 "8.4.11 LTS"），两处都要尝试
_OPTION_PAIR_RE = re.compile(
    r'<option\s+value="([^"]*)"[^>]*>\s*([^<]+?)\s*</option>',
    re.IGNORECASE | re.S)
# 正式版本号（三段纯数字）
_STABLE_RE = re.compile(r'^\d+\.\d+\.\d+$')
# 预览版标记（rc/dm/m/alpha/beta 等）
_PREVIEW_RE = re.compile(r'(rc|dmr|dm|m\d|alpha|beta)', re.IGNORECASE)


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read().decode('utf-8', errors='replace')


def _candidate_version(value: str, text: str) -> str:
    """从 option 的 value 与文本中提取正式版本号，预览版返回空串"""
    for cand in (value, text):
        tokens = cand.strip().split()
        if not tokens:
            continue
        # 后缀含预览标记（如 "8.0.4 rc"）的跳过
        if any(_PREVIEW_RE.search(t) for t in tokens[1:]):
            continue
        if _STABLE_RE.fullmatch(tokens[0]):
            return tokens[0]
    return ''


def _parse_versions(html: str) -> list:
    """从页面 HTML 中提取版本号列表"""
    versions = []
    for value, text in _OPTION_PAIR_RE.findall(html):
        ver = _candidate_version(value, text)
        if ver and ver not in versions:
            versions.append(ver)
    return versions


def _version_key(ver: str):
    """版本号排序键（降序用）"""
    return tuple(int(x) for x in ver.split('.'))


def build_version_entry(version: str, is_current: bool) -> dict:
    """按版本号构造下载条目（zip 直链 + MSI 安装包直链），供抓取与选项页共用"""
    major = '.'.join(version.split('.')[:2])
    if is_current:
        url = CDN_URL_TPL.format(major=major, version=version)
        installer_url = CDN_MSI_TPL.format(major=major, version=version)
        channel = 'current'
    else:
        url = ARCHIVE_URL_TPL.format(version=version)
        installer_url = ARCHIVE_MSI_TPL.format(major=major, version=version)
        channel = 'archives'
    # LTS 约定：8.4 / 9.x 中的偶数小版本系列为长期支持
    parts = version.split('.')
    lts = False
    if len(parts) >= 2:
        lts = parts[0] == '8' and parts[1] == '4'
    return {
        'version': version,
        'major': major,
        'channel': channel,
        'channel_display': '当前GA' if is_current else ('LTS' if lts else '历史版本'),
        'is_lts': lts,
        'url': url,
        'installer_url': installer_url,
        'size_hint': '',
    }


def fetch_mysql_versions(limit: int = 60) -> dict:
    """
    抓取官网社区版版本列表。
    返回 {success, source: 'website'/'fallback', message, versions: [entry, ...]}
    versions 按版本号降序，current 渠道在前。
    """
    current_versions, archive_versions = [], []
    errors = []

    try:
        current_versions = _parse_versions(_fetch_html(CURRENT_PAGE))
    except Exception as e:
        errors.append(f'当前版本页抓取失败: {e}')
        logger.warning('抓取 MySQL 当前版本页失败: %s', e)

    try:
        archive_versions = _parse_versions(_fetch_html(ARCHIVES_PAGE))
    except Exception as e:
        errors.append(f'历史版本页抓取失败: {e}')
        logger.warning('抓取 MySQL 历史版本页失败: %s', e)

    if not current_versions and not archive_versions:
        entries = [build_version_entry(v, is_current=(i < 2))
                   for i, v in enumerate(FALLBACK_VERSIONS)]
        return {
            'success': True,
            'source': 'fallback',
            'message': '官网抓取失败，已使用内置版本列表。; ' + ' ; '.join(errors),
            'versions': entries[:limit],
        }

    entries = [build_version_entry(v, is_current=True) for v in current_versions]
    seen = set(e['version'] for e in entries)
    archives = [v for v in archive_versions if v not in seen]
    # 过滤 5.5 以下（无 winx64 zip 包）
    archives = [v for v in archives if _version_key(v) >= (5, 5, 0)]
    archives.sort(key=_version_key, reverse=True)
    entries += [build_version_entry(v, is_current=False) for v in archives]
    return {
        'success': True,
        'source': 'website',
        'message': '抓取成功' if not errors else '部分页面抓取失败: ' + ' ; '.join(errors),
        'versions': entries[:limit],
    }


# ── 版本列表本地 JSON 缓存 ──────────────────────────

from .config import VERSION_CACHE_FILE
# VERSION_CACHE_FILE = settings.DATA_DIR / 'dbmysql_versions.json'


def _load_version_cache() -> dict:
    """读取本地版本缓存，不存在或损坏返回 None"""
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
    """保存版本列表缓存（仅保存成功获取的列表）"""
    try:
        VERSION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {'fetched_at': result.get('fetched_at', ''),
                   'source': result.get('source', ''),
                   'versions': result.get('versions', [])}
        VERSION_CACHE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as e:
        logger.warning('版本列表缓存保存失败: %s', e)


def get_mysql_versions(refresh: bool = False, limit: int = 60) -> dict:
    """
    获取版本列表（优先本地 JSON 缓存）。
    refresh=False 且缓存存在时直接返回缓存；否则联网官网抓取，
    抓取成功后写入缓存；完全失败且无缓存时返回内置兜底列表。
    返回 {success, source: cache/website/fallback, fetched_at, message, versions}
    """
    if not refresh:
        cached = _load_version_cache()
        if cached:
            return {
                'success': True,
                'source': 'cache',
                'fetched_at': cached.get('fetched_at', ''),
                'message': f"本地缓存（抓取于 {cached.get('fetched_at', '未知时间')}），"
                           '点“重新抓取”可从官网更新',
                'versions': cached['versions'][:limit],
            }
    result = fetch_mysql_versions(limit)
    result['fetched_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if result.get('versions'):
        _save_version_cache(result)
    return result
