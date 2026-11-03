import os
import re
import sys
import ctypes
import locale
import functools
from itertools import dropwhile

# pre 段别名到排序值，a < b < rc
_PRE_ALIASES = {
    'a': 0, 'alpha': 0,
    'b': 1, 'beta': 1,
    'c': 2, 'rc': 2, 'pre': 2, 'preview': 2,
}

_VERSION_RE = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>\d+)!)?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>\d+)?)?
    (?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n>\d+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

def _cmpkey(epoch, release, pre, post, dev):
    # 去掉 release 末尾的 0，让 2025.2 与 2025.2.0 相等
    release = tuple(reversed(tuple(dropwhile(lambda x: x == 0, reversed(release)))))
    if not release:
        release = (0,)

    # pre 段构造：
    # - 没有 pre，但只有 dev：比任何 pre 都小
    # - 没有 pre，也没有 dev：比任何 pre 都大
    # - 有 pre：用 (rank, n)
    if pre is None and post is None and dev is not None:
        _pre = (-1, 0)
    elif pre is None:
        _pre = (99, 0)
    else:
        _pre = pre

    # post 段：没有 post 比有 post 小
    _post = (-1, 0) if post is None else (0, post)

    # dev 段：没有 dev 比有 dev 大
    _dev = (99, 0) if dev is None else (0, dev)

    return (epoch, release, _pre, _post, _dev)


def parse_version(ver: str) -> tuple:
    """
    解析版本字符串，返回可直接比较的元组。
    支持 PEP 440 风格的版本：
        2025.2.3
        2025.2.3rc1
        2025.2.3b1
        2025.2.3a2
        2025.2.3.post1
        2025.2.3.dev1
        2025.2.3rc1.dev1
        1!2025.2.3        （epoch）
    抛 ValueError 表示无法解析。
    """
    m = _VERSION_RE.match(ver)
    if not m:
        raise ValueError(f"无法解析版本号: {ver!r}")

    epoch = int(m.group('epoch') or 0)
    release = tuple(int(x) for x in m.group('release').split('.'))

    pre_l = m.group('pre_l')
    pre = (_PRE_ALIASES[pre_l.lower()], int(m.group('pre_n') or 0)) if pre_l else None

    post = int(m.group('post_n') or 0) if m.group('post_l') else None

    dev = int(m.group('dev_n') or 0) if m.group('dev_l') else None

    return _cmpkey(epoch, release, pre, post, dev)


"""通用格式化工具函数。"""
from typing import Union

Number = Union[int, float]

def format_size(num_bytes: Number, precision: int = 2) -> str:
    """把字节数转换为人类直观的大小字符串。

    Args:
        num_bytes: 字节数，允许 int / float；非法或 None 返回 '未知大小'。
        precision: 小数位数，默认 2；当单位为 B 时忽略（直接取整）。

    Returns:
        形如 '1.15 GB'、'998.70 MB'、'0 B' 的字符串。

    Examples:
        >>> format_size(0)
        '0 B'
        >>> format_size(1536)
        '1.50 KB'
        >>> format_size(1234567890)
        '1.15 GB'
        >>> format_size(None)
        '未知大小'
    """
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return '未知大小'

    if size <= 0:
        return '0 B'

    units = ('B', 'KB', 'MB', 'GB', 'TB', 'PB')
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1

    if idx == 0:  # 字节直接取整
        return f'{int(size)} B'
    return f'{size:.{precision}f} {units[idx]}'


@functools.lru_cache(maxsize=1)
def get_cmd_encoding() -> str:
    """Windows 原生命令（netstat/tasklist/sc/ipconfig…）重定向输出的编码名。

    cmd.exe 走系统 OEM 代码页（中文 cp936，英文 cp437…），不是 ANSI，也不是 UTF-8。
    注意 locale.getpreferredencoding 在 UTF-8 mode / Python 3.15+ 会返回 utf-8，
    不可靠，故用 GetOEMCP()。非 Windows 返回 utf-8。

    仅适用于传统 Windows 原生命令；git/node/python 等工具输出 UTF-8，不适用。
    .bat 文件写入应使用固定 'gbk'（批处理格式要求）。
    """
    if os.name != 'nt':
        return 'utf-8'
    try:
        return f'cp{ctypes.windll.kernel32.GetOEMCP()}'
    except Exception:
        try:
            if sys.version_info >= (3, 11):
                return locale.getencoding()
            return locale.getpreferredencoding(False)
        except Exception:
            return 'gbk'