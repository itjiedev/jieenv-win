import re
import json
import gzip
import logging
import urllib.request
from datetime import datetime

from jiecore.json_storage import JsonStore
from jiecore.utils import parse_version
from apps.devtools.services.pycharm.config import PYCHARM_VERSION_CACHE_FILE

logger = logging.getLogger(__name__)

JETBRAINS_API = 'https://data.services.jetbrains.com/products/releases'

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

BROWSER_HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'application/json',
    'Sec-Ch-Ua': '"Chromium";v="126", "Not_A Brand";v="24"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
}

FETCH_TIMEOUT = 20

FALLBACK_VERSIONS = [
    {
      "date": "2026-11-05",
      "type": "release",
      "version": "2026.3",
      "majorVersion": "2026.3",
      "build": "263.7307.206",
      "downloads": {"windows": {"link": "https://download.jetbrains.com/python/pycharm-professional-2026.3.exe"}},
    },
    {
      "date": "2026-09-07",
      "type": "release",
      "version": "2026.2.2",
      "majorVersion": "2026.2",
      "build": "262.10315.174",
      "downloads": {"windows": {"link": "https://download.jetbrains.com/python/pycharm-professional-2026.2.2.exe"}},
    }
]

_version_cache = JsonStore(PYCHARM_VERSION_CACHE_FILE, default={})


def build_version_entry(data) -> dict:
    version = data.get('version', '')
    is_unified = parse_version(version) > parse_version('2025.2.2')

    windows_link = data.get('downloads', {}).get('windows', {}).get('link', '')

    version_entry = {
        'version': version,
        'majorVersion': data.get('majorVersion', ''),
        'build': data.get('build', ''),
        'channel': 'release',
        'channel_display': '正式版',
        'is_unified': is_unified,
        'exe_url': windows_link,
        'community_exe_url': '',
    }
    if not is_unified:
        version_entry['community_exe_url'] = windows_link.replace('professional', 'community')
    return version_entry

def fetch_pycharm_versions(limit_ver: int = 2016) -> dict:
    versions = {}
    errors = []

    try:
        url = f'{JETBRAINS_API}?code=PCP'
        req = urllib.request.Request(url, headers=BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw_data = resp.read()
            if resp.headers.get('Content-Encoding') == 'gzip':
                raw_data = gzip.decompress(raw_data)
            data = json.loads(raw_data.decode('utf-8'))

        data = data['PCP']
        for product in data:
            info = {}
            info['version'] = product.get('version', '')
            if not info['version']: continue
            info['majorVersion'] = product.get('majorVersion', '')
            if info['majorVersion']:
                info['mainVersion'] = info['majorVersion'].split('.')[0]
            else:
                info['mainVersion'] = info['version'].split('.')[0]
            if int(info['mainVersion']) < limit_ver: break
            info['build'] = product.get('build', '')
            info['downloads'] = product.get('downloads', {})
            entry = build_version_entry(info)
            if not entry['exe_url']: continue
            versions.setdefault(info['mainVersion'], {}).setdefault(info['majorVersion'], {})[info['version']] = entry

    except Exception as e:
        errors.append(f'PyCharm 版本列表抓取失败: {str(e)}')
        logger.warning('抓取 PyCharm 版本列表失败: %s', e)

    if not versions:
        for product in FALLBACK_VERSIONS:
            info = {}
            info['version'] = product.get('version', '')
            if not info['version']: continue
            info['majorVersion'] = product.get('majorVersion', '')
            if info['majorVersion']:
                info['mainVersion'] = info['majorVersion'].split('.')[0]
            else:
                info['mainVersion'] = info['version'].split('.')[0]
            info['build'] = product.get('build', '')
            info['downloads'] = product.get('downloads', {})
            entry = build_version_entry(info)
            if not entry['exe_url']: continue
            versions.setdefault(info['mainVersion'], {}).setdefault(info['majorVersion'], {})[info['version']] = entry

        return {
            'success': True,
            'source': 'fallback',
            'message': '官网抓取失败，已使用内置版本列表。; ' + ' ; '.join(errors),
            'versions': versions,
        }

    return {
        'success': True,
        'source': 'website',
        'message': '抓取成功' if not errors else '部分抓取失败: ' + ' ; '.join(errors),
        'versions': versions,
    }


def _load_version_cache() -> dict:
    if not PYCHARM_VERSION_CACHE_FILE.exists():
        logger.warning('PyCharm 缓存文件不存在')
        return None
    try:
        data = _version_cache.read()
        return data
    except (OSError, ValueError) as e:
        logger.warning('PyCharm 版本缓存读取失败: %s', e)
    return None


def _save_version_cache(result: dict):
    try:
        PYCHARM_VERSION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {'fetched_at': result.get('fetched_at', ''),
                   'source': result.get('source', ''),
                   'versions': result.get('versions', [])}
        PYCHARM_VERSION_CACHE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as e:
        logger.warning('PyCharm 版本缓存保存失败: %s', e)


def get_pycharm_versions(refresh: bool = False, limit_ver: int = 2016, allow_fetch: bool = True) -> dict:
    if not refresh:
        cached = _load_version_cache()
        if cached:
            return {
                'success': True,
                'source': 'cache',
                'fetched_at': cached.get('fetched_at', ''),
                'message': f"本地缓存（抓取于 {cached.get('fetched_at', '未知时间')}），"
                           '点"重新抓取"可从官网更新',
                'versions': cached['versions'],
            }
        if not allow_fetch:
            return {
                'success': True,
                'source': 'none',
                'fetched_at': '',
                'message': '尚无版本缓存，请点击下方按钮从官网抓取',
                'versions': {},
            }
    result = fetch_pycharm_versions(limit_ver)
    result['fetched_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if result.get('versions'):
        _save_version_cache(result)
    return result