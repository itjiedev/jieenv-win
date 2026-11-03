"""
Python 安装包下载源管理。

职责：
- 维护内置镜像源列表 DOWNLOAD_SOURCES（南大、华为云、清华、官方）；
- 持久化用户选择的默认下载源到 DATA_DIR/download_source.json；
- 按版本号拼接 Windows 64-bit exe 安装包直链；
- 在实际下载前用 HEAD 请求探测源可用性，不可用时自动回退到下一个源，
  最终兜底到官方源，保证下载链路尽量不中断。

对外主要接口：
    list_sources()                     列出全部可选源（含"自动探测"）
    get_default_source()               读取持久化的默认源（None 表示自动）
    set_default_source(name)           写入默认源；name='auto' 清除为自动
    build_exe_url(base_url, version)   拼接 exe 直链
    resolve_download(version, ...)     综合"临时指定 > 默认 > 自动探测"解析出
                                       最终 (source_dict, exe_url)
"""
import json
import logging
import urllib.request
import urllib.error

from .config import DOWNLOAD_SOURCE_FILE

logger = logging.getLogger(__name__)

# 下载/探测专用请求头。
# 注意：不能用抓 HTML 版本页的 BROWSER_HEADERS（含浏览器 User-Agent）。
# 实测南京大学镜像对浏览器 UA 返回 302 且 Location 指向同一 URL（无限重定向循环），
# 清华 TUNA 镜像对浏览器 UA 直接返回 403；二者对中性 UA（如 curl/Python-urllib）均正常 200。
# 故下载二进制 exe 与 HEAD 探测统一使用下面这个非浏览器 UA。
DOWNLOAD_USER_AGENT = 'jieEnv-python-installer/1.0'
DOWNLOAD_HEADERS = {
    'User-Agent': DOWNLOAD_USER_AGENT,
    'Accept': '*/*',
}

# 内置下载源，顺序即自动探测的优先级：镜像站放前面（国内快），官方兜底。
# 所有镜像都遵循 <base>/<version>/python-<version>-amd64.exe 的目录布局。
DOWNLOAD_SOURCES = [
    {'name': '南京大学', 'url': 'https://mirror.nju.edu.cn/python/'},
    {'name': '华为云',   'url': 'https://mirrors.huaweicloud.com/python/'},
    {'name': '清华大学', 'url': 'https://mirrors.tuna.tsinghua.edu.cn/python/'},
    {'name': '淘宝',     'url': 'https://cdn.npmmirror.com/binaries/python/'},
    {'name': '官方',     'url': 'https://www.python.org/ftp/python/'},
]

# 特殊源名：自动探测（不持久化具体镜像，运行时按顺序 HEAD 探测）
AUTO_SOURCE_NAME = 'auto'
_AUTO_ENTRY = {'name': AUTO_SOURCE_NAME, 'display_name': '自动探测（安装时自动使用首个可用源）', 'url': ''}

# HEAD 探测超时（秒）——镜像站偶尔抖动，短超时快速切换到下一个
PROBE_TIMEOUT = 3


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


def get_default_source():
    """读取持久化的默认下载源。

    返回 dict {name, url}；文件不存在、损坏或 name='auto' 时返回 None，
    表示"自动探测"模式。
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
        return src
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
    else:
        src = _find_source_by_name(name)
        if not src:
            raise ValueError(f'未知的下载源: {name}')
        payload = {'name': src['name'], 'url': src['url']}
    try:
        DOWNLOAD_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DOWNLOAD_SOURCE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as e:
        logger.exception('下载源配置写入失败')
        raise RuntimeError(f'保存下载源配置失败: {e}')
    return {
        'name': payload['name'],
        'url': payload['url'],
        'display_name': '自动探测（推荐）' if payload['name'] == AUTO_SOURCE_NAME else payload['name'],
    }


# ── URL 构造 / 探测 ────────────────────────────────

def build_exe_url(base_url: str, version: str) -> str:
    """按镜像根地址拼接 Windows 64-bit exe 安装包直链。

    所有内置镜像与官方 ftp 都遵循 <base>/<version>/python-<version>-amd64.exe
    的目录布局，base_url 末尾有无 '/' 均可。
    """
    base = (base_url or '').rstrip('/')
    return f'{base}/{version}/python-{version}-amd64.exe'


def build_launcher_url(base_url: str, version: str) -> str:
    """按镜像根地址拼接 py launcher MSI 安装包直链。

    Python 官方 ftp 与各镜像站在 <base>/<version>/win32/launcher.msi 路径下
    提供独立的 py launcher 安装包，无需下载完整 Python exe 即可安装。
    base_url 末尾有无 '/' 均可。
    """
    base = (base_url or '').rstrip('/')
    return f'{base}/{version}/win32/launcher.msi'


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
            # HEAD 不被允许，用 Range GET 取 1 字节验证
            return _probe_with_range_get(url, timeout)
        return 400 <= e.code < 500 and e.code not in (404, 403)
    except Exception:
        return False


def _probe_with_range_get(url: str, timeout: int) -> bool:
    headers = dict(DOWNLOAD_HEADERS)
    headers['Range'] = 'bytes=0-0'
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


# ── 综合解析 ──────────────────────────────────────

def resolve_download(version: str, preferred_name: str = None, on_progress=None) -> dict:
    """按"临时指定 > 持久化默认 > 自动探测"解析出最终下载源与 exe 直链。

    参数：
        version         版本号，如 '3.13.5'
        preferred_name  本次安装临时指定的源名（None/'auto' 表示不指定）
        on_progress     可选回调，接收探测过程的可读日志字符串

    返回 dict：
        {
            'source': {'name', 'url'},   # 最终选中的源
            'exe_url': 'https://.../python-x.y.z-amd64.exe',
            'mode': 'preferred' | 'default' | 'auto' | 'fallback',
        }

    解析策略：
    1. preferred_name 有效 → 直接用它（不探测，尊重用户显式选择）；
    2. 否则读持久化默认源 → 用它（不探测）；
    3. 否则按 DOWNLOAD_SOURCES 顺序 HEAD 探测，第一个可达的即用；
    4. 全部不可达 → 兜底用官方源（可能网络受限，但让下载去报错比这里
       直接失败更直观，用户能看到真实的 HTTP 错误）。
    """
    def _log(msg):
        if on_progress:
            on_progress(msg)

    # 1. 临时指定
    if preferred_name and preferred_name != AUTO_SOURCE_NAME:
        src = _find_source_by_name(preferred_name)
        if src:
            _log(f'使用本次指定的下载源：{src["name"]}')
            return {
                'source': src,
                'exe_url': build_exe_url(src['url'], version),
                'mode': 'preferred',
            }
        _log(f'指定的下载源 {preferred_name} 不存在，回退到默认/自动')

    # 2. 持久化默认
    default = get_default_source()
    if default:
        _log(f'使用已保存的默认下载源：{default["name"]}')
        return {
            'source': default,
            'exe_url': build_exe_url(default['url'], version),
            'mode': 'default',
        }

    # 3. 自动探测：按 DOWNLOAD_SOURCES 顺序 HEAD 探测，第一个可达的即用
    _log('未设置默认下载源，按顺序探测可用镜像...')
    for src in DOWNLOAD_SOURCES:
        exe_url = build_exe_url(src['url'], version)
        if probe_url(exe_url):
            _log(f'探测到可用镜像：{src["name"]}')
            return {'source': dict(src), 'exe_url': exe_url, 'mode': 'auto'}

    # 4. 全部不可达，兜底官方
    official = DOWNLOAD_SOURCES[-1]
    exe_url = build_exe_url(official['url'], version)
    _log(f'所有镜像均不可达，兜底使用官方源：{exe_url}')
    return {'source': dict(official), 'exe_url': exe_url, 'mode': 'fallback'}


def resolve_launcher_download(version: str, preferred_name: str = None,
                              on_progress=None) -> dict:
    """按"临时指定 > 持久化默认 > 自动探测"解析出 py launcher MSI 下载源与直链。

    参数与 resolve_download 相同，但拼接的是 win32/launcher.msi 路径。
    返回 dict 结构同 resolve_download，字段 launcher_url 替代 exe_url。
    """
    def _log(msg):
        if on_progress:
            on_progress(msg)

    # 1. 临时指定
    if preferred_name and preferred_name != AUTO_SOURCE_NAME:
        src = _find_source_by_name(preferred_name)
        if src:
            _log(f'使用本次指定的下载源：{src["name"]}')
            return {
                'source': src,
                'launcher_url': build_launcher_url(src['url'], version),
                'mode': 'preferred',
            }
        _log(f'指定的下载源 {preferred_name} 不存在，回退到默认/自动')

    # 2. 持久化默认
    default = get_default_source()
    if default:
        _log(f'使用已保存的默认下载源：{default["name"]}')
        return {
            'source': default,
            'launcher_url': build_launcher_url(default['url'], version),
            'mode': 'default',
        }

    # 3. 自动探测：按 DOWNLOAD_SOURCES 顺序 HEAD 探测，第一个可达的即用
    _log('未设置默认下载源，按顺序探测可用镜像...')
    for src in DOWNLOAD_SOURCES:
        launcher_url = build_launcher_url(src['url'], version)
        if probe_url(launcher_url):
            _log(f'探测到可用镜像：{src["name"]}')
            return {'source': dict(src), 'launcher_url': launcher_url, 'mode': 'auto'}

    # 4. 全部不可达，兜底官方
    official = DOWNLOAD_SOURCES[-1]
    launcher_url = build_launcher_url(official['url'], version)
    _log(f'所有镜像均不可达，兜底使用官方源：{launcher_url}')
    return {'source': dict(official), 'launcher_url': launcher_url, 'mode': 'fallback'}
