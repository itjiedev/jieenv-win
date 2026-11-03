"""
抓取 python 的压缩包系列版本
"""
import re
import json
import logging
import time
import urllib.request
from datetime import datetime
from urllib.parse import urljoin, urlparse, unquote

from .config import VERSION_CACHE_FILE

logger = logging.getLogger(__name__)

SOURCE = OFFICIAL_BASE = "https://www.python.org/ftp/python/"
INDEX_URL = urljoin(OFFICIAL_BASE, "index-windows.json")

# ---- 预编译正则 ----
_PRE_RE = re.compile(r"(?:rc|alpha|beta|dev|pre|a|b)\d+$", re.I)
_ARM_RE = re.compile(r"arm64|aarch64|armv7|armv8|\barm\b", re.I)
_EMBED_RE = re.compile(r"embeddable|\bembed\b", re.I)

_X64_RE = re.compile(r"amd64|win64|x86[_-]?64|x64|64[_-]?bit|64bit", re.I)
_X86_RE = re.compile(r"win32|x32|x86|32[_-]?bit|32bit", re.I)

_FREETHREADED_RE = re.compile(r"free[\s_-]?threaded|freethreaded", re.I)
_WITH_TESTS_RE = re.compile(r"with[\s_-]?tests?\b", re.I)
_WITHOUT_TESTS_RE = re.compile(r"without[\s_-]?tests?\b|no[\s_-]?tests?\b", re.I)

_PAREN_RE = re.compile(r"[（(].*?[)）]")
_PY_PREFIX_RE = re.compile(r"^Python\s+", re.I)
_WS_TAIL_RE = re.compile(r"\s.*$")
_DIGITS_RE = re.compile(r"\d+")
_ID_SANITIZE_RE = re.compile(r"[^a-z0-9]+")
_ARCH_SUFFIX_64_RE = re.compile(r"[-_ ]64\b")
_ARCH_SUFFIX_32_RE = re.compile(r"[-_ ]32\b")
_NUPKG_X86_RE = re.compile(r"pythonx86|win32|x32")

TIMEOUT = 30
DEFAULT_LIMIT = 200

# 用于一次性生成 blob / attr_text 的字段列表
_BLOB_KEYS = ("url", "display-name", "name", "sort-version", "id", "tag")
_ATTR_KEYS = ("url", "display-name", "name", "sort-version",
              "id", "tag", "install-for")


# ---------------------------------------------------------------------------
# 网络抓取
# ---------------------------------------------------------------------------
import requests

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Python-Version-Fetcher/1.0",
    "Accept-Encoding": "gzip, deflate",
})


def _fetch_json(url):
    t0 = time.perf_counter()
    r = _session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    logger.debug("fetch %s: %.3fs (%d bytes after decode)",
                 url, time.perf_counter() - t0, len(r.content))
    return data


def _get_next(data, base):
    if not isinstance(data, dict):
        return None
    v = data.get("next")
    return urljoin(base, v) if isinstance(v, str) and v else None


from concurrent.futures import ThreadPoolExecutor

_INDEXES = [
    "https://www.python.org/ftp/python/index-windows.json",
    "https://www.python.org/ftp/python/index-windows-recent.json",
    "https://www.python.org/ftp/python/index-windows-legacy.json",
]


def _crawl(start=None):
    items = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for data in ex.map(_fetch_json, _INDEXES):
            for key in ("versions", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    items.extend(v)
    return items


# ---------------------------------------------------------------------------
# 每个条目只算一次的预处理
# ---------------------------------------------------------------------------
def _preprocess(v):
    """一次性算出 blob 与 attr_text，供下游复用。"""
    blob = " ".join(str(v.get(k, "")) for k in _BLOB_KEYS).lower()

    parts = []
    for k in _ATTR_KEYS:
        val = v.get(k)
        if isinstance(val, (list, tuple)):
            parts.append(" ".join(str(x) for x in val))
        elif val is not None:
            parts.append(str(val))
    attr_text = " ".join(parts).lower()

    return blob, attr_text


def _is_pre(v):
    """判断是否为预发布版本。"""
    for k in ("sort-version", "display-name"):
        t = _PAREN_RE.sub("", str(v.get(k, "")))
        t = _PY_PREFIX_RE.sub("", t).strip()
        t = _WS_TAIL_RE.sub("", t)
        if t and _PRE_RE.search(t):
            return True
    return False


def _arch_of(v, blob):
    """
    返回 '64' / '32' / '?'。
    判断优先级（越靠前越权威）：
      1) shortcuts[].SysArchitecture
      2) id / tag / install-for 里的 -64 / -32
      3) .nupkg 包名规则（python 为 64，pythonx86 为 32）
      4) URL / display-name / name / sort-version / id / tag 里的标记
      5) 兜底：裸 .exe 视为 64
    """
    # 1) shortcuts 里的 SysArchitecture
    sc = v.get("shortcuts")
    if isinstance(sc, list):
        for s in sc:
            if isinstance(s, dict):
                arch = str(s.get("SysArchitecture", "")).lower()
                if "64" in arch:
                    return "64"
                if "32" in arch:
                    return "32"

    # 2) id / tag 里的 -64 / -32
    for k in ("id", "tag"):
        t = str(v.get(k, "")).lower()
        if _ARCH_SUFFIX_64_RE.search(t):
            return "64"
        if _ARCH_SUFFIX_32_RE.search(t):
            return "32"

    # 2b) install-for 列表
    inst = v.get("install-for")
    if isinstance(inst, list):
        for s in inst:
            t = str(s).lower()
            if _ARCH_SUFFIX_64_RE.search(t):
                return "64"
            if _ARCH_SUFFIX_32_RE.search(t):
                return "32"

    # 3) .nupkg 特判
    url = str(v.get("url", "")).lower()
    if url.endswith(".nupkg"):
        if _NUPKG_X86_RE.search(url):
            return "32"
        if "python" in url:
            return "64"

    # 4) 通用字段匹配（用传入的 blob，避免重复拼接）
    if _X64_RE.search(blob):
        return "64"
    if _X86_RE.search(blob):
        return "32"

    # 5) 兜底：裸 .exe → 64
    if url.endswith(".exe"):
        return "64"
    return "?"


def _stem(filename):
    """去掉扩展名的文件名。"""
    return filename.rsplit(".", 1)[0] if "." in filename else filename


def _keep(v, blob):
    """过滤：保留正式版、非 ARM、非 embeddable 的有效条目。"""
    if not isinstance(v, dict) or not v.get("url"):
        return False
    if _is_pre(v):
        return False
    if _ARM_RE.search(blob) or _EMBED_RE.search(blob):
        return False
    # 去掉 x32 版本：只剔除明确判定为 32 位的条目，
    # 架构未知（'?'）的条目保持原有行为予以保留。
    # if _arch_of(v, blob) == "32":
    #     return False
    return True


def _make_id(filename):
    """
    由文件名生成稳定唯一识别符：
      小写、非字母数字统一替换为 '-'，去掉首尾 '-'。
    例：python-3.13.1-amd64.exe → python-3-13-1-amd64
    """
    return _ID_SANITIZE_RE.sub("-", filename.lower()).strip("-")


def _is_free_threaded(v, attr_text):
    """是否 free-threaded（无 GIL）版本。"""
    # 1) 专用布尔字段优先
    for k in ("free-threaded", "free_threaded", "freethreaded",
              "is-free-threaded", "is_free_threaded"):
        if v.get(k) is True:
            return True
    # 2) 文本兜底
    return bool(_FREETHREADED_RE.search(attr_text))


def _tests_flag(v, attr_text):
    """
    返回 True / False。
    Python 官方 Windows 安装包部分带测试套件，部分不带。
    """
    # 1) 专用布尔字段
    for k in ("with-tests", "with_tests", "tests"):
        val = v.get(k)
        if isinstance(val, bool):
            return val
    # 2) 文本兜底：先判 without，避免 "without tests" 被 with 规则误吞
    if _WITHOUT_TESTS_RE.search(attr_text):
        return False
    if _WITH_TESTS_RE.search(attr_text):
        return True
    return False


# ---------------------------------------------------------------------------
# 清洗 / 组装
# ---------------------------------------------------------------------------
def _version_key(v):
    """排序 key：定长三元组，避免 list 长度不一致时的比较歧义。"""
    nums = [int(x) for x in _DIGITS_RE.findall(v["version"])]
    nums = (nums + [0, 0, 0])[:3]
    return tuple(nums)


def _build_versions(raw):
    """把原始条目清洗、去重、排序成统一结构。"""
    versions, seen = [], set()
    for v in raw:
        if not isinstance(v, dict) or not v.get("url"):
            continue

        # 每个条目只算一次
        blob, attr_text = _preprocess(v)

        if not _keep(v, blob):
            continue

        url = str(v["url"])
        if not url.startswith(("http://", "https://")):
            url = urljoin(OFFICIAL_BASE, url.lstrip("/"))
        if url in seen:
            continue
        seen.add(url)

        sv = _PAREN_RE.sub("", str(v.get("sort-version", "")))
        sv = _PY_PREFIX_RE.sub("", sv).strip()
        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])

        versions.append(
            {
                "id": _make_id(filename),
                "display_name": v.get("display-name", ""),
                "version": sv or v.get("display-name", ""),
                "arch": _arch_of(v, blob),
                "url": url,
                "filename": filename,
                "stem": _stem(filename),
                "ext": filename.rsplit(".", 1)[1].lower() if "." in filename else "",
                "free_threaded": _is_free_threaded(v, attr_text),
                "with_tests": _tests_flag(v, attr_text),
            }
        )

    versions.sort(key=_version_key, reverse=True)
    return versions


def collect():
    """抓取 + 清洗，返回带元信息的完整 payload。"""
    t0 = time.perf_counter()
    raw = _crawl(INDEX_URL)
    t1 = time.perf_counter()
    built = _build_versions(raw)
    t2 = time.perf_counter()
    logger.info(
        "collect: crawl %.3fs (%d raw), build %.3fs (%d kept), total %.3fs",
        t1 - t0, len(raw), t2 - t1, len(built), t2 - t0,
    )
    return {
        'success': True,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": SOURCE,
        "versions": built,
    }


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def _load_version_cache() -> dict:
    if not VERSION_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(VERSION_CACHE_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return None

        versions = data.get('versions')
        if isinstance(versions, list) and versions:
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


def get_python_versions(refresh: bool = False) -> dict:
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
                'versions': cached['versions'],
            }
    result = collect()
    result['fetched_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if result.get('versions'):
        _save_version_cache(result)
    return result