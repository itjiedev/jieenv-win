"""
requirements.txt 解析模块（纯函数，无 Django 依赖，可独立单测）

提供 requirements.txt 内容的行级三分法解析（有效 / 忽略 / 无法识别）
与本机路径五项复检读取，供 python_main 的解析预览与批量安装 API 调用。

使用方式::

    from apps.python_main.requirements_parser import (
        parse_requirements_text, read_requirements_file, summarize_ignored)

    # 解析文本内容（上传 / 粘贴通道，前端已读为文本）
    result = parse_requirements_text('requests\\nflask==3.0.0\\n')

    # 读取并解析本机文件（本机文件选择通道，服务端读原始字节）
    read = read_requirements_file('D:/deps/requirements.txt')
    if read['success']:
        result = parse_requirements_text(read['content'])
"""
import re
from pathlib import Path

# 输入约束常量（spec 7.1）
MAX_FILE_BYTES = 1048576        # 文件 / 文本内容大小上限：1MB
MAX_TOTAL_LINES = 5000          # 总行数上限，超出由调用方整体拒绝
MAX_LINE_LENGTH = 1000          # 单行长度上限，超出降级为无法识别（不尝试匹配）
PIP_INSTALL_TIMEOUT = 3600      # 批量安装超时上限（秒），与批量升级一致
REQ_TXT_SUFFIX = '.txt'         # 本机文件选择入口仅接受 .txt
LOG_DETAIL_LIMIT = 200          # 任务日志头部各明细段行数封顶（jiecore 任务日志 2000 行从头截断的缓解）

# 行内注释：自首个"空白 + #"起截断（pip 官方语义；紧贴式 # 不算注释，如 foo#bar）
_INLINE_COMMENT_RE = re.compile(r'\s#')

# 有效规格：裸包名 / 操作符单约束 / 逗号分隔多约束（7 种操作符，与直接安装表单一致）
_SPEC_RE = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9._-]*'
    r'(?:\s*(?:==|!=|<=|>=|~=|<|>)\s*[A-Za-z0-9.*!+_-]+'
    r'(?:\s*,\s*(?:==|!=|<=|>=|~=|<|>)\s*[A-Za-z0-9.*!+_-]+)*)?$'
)

# 本地路径形态：./ 或 ../ 相对路径、Windows 盘符绝对路径、\\ UNC 路径（URL scheme 形态不在此列）
_LOCAL_PATH_RE = re.compile(r'^(?:\.{1,2}[\\/]|[A-Za-z]:[\\/]|\\\\)')

# 合法包名（name @ url 形式的前半段校验）
_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')

# VCS / URL 前缀：这些形态的规格整行透传给 pip 自行处理
_URL_PREFIXES = ('git+', 'hg+', 'svn+', 'bzr+', 'http://', 'https://', 'ftp://')

# 汇总文案中各忽略原因的短名（括号内说明不进入汇总）
_IGNORED_SHORT_NAMES = {
    '空行': '空行',
    '注释行': '注释',
    '嵌套引用（不递归解析）': '嵌套引用',
    '可编辑安装（不支持）': '可编辑安装',
    '选项行（不透传，遵循全局源配置）': '选项',
    '本地路径安装（不支持）': '本地路径',
}


def _classify_line(raw: str):
    """对单行分类（三分法的单行判定，顺序固定即优先级）。

    返回 (kind, payload)：
      - ('valid', 规格文本)：透传给 pip 的行（已剥离行内注释，环境标记整行保留）；
      - ('ignored', 原因文案)：忽略行（空行 / 注释 / 嵌套引用 / 选项 / 可编辑 / 本地路径）；
      - ('unrecognized', None)：无法识别行。
    单行判定永不抛异常（宽容策略，异常输入一律归 unrecognized）。
    """
    try:
        # 超长行不尝试匹配，直接降级为无法识别（spec 7.1.4）
        if len(raw) > MAX_LINE_LENGTH:
            return 'unrecognized', None
        text = raw.strip()
        if not text:
            return 'ignored', '空行'
        if text.startswith('#'):
            return 'ignored', '注释行'
        # 剥离行内注释：自首个"空白 + #"起截断；剥离后为空说明整行是注释
        m = _INLINE_COMMENT_RE.search(text)
        if m:
            text = text[:m.start()].rstrip()
            if not text:
                return 'ignored', '注释行'
        # 选项 / 引用 / 可编辑安装：以首个空白分隔的 token 判定
        first = text.split(None, 1)[0].lower()
        if first in ('-r', '--requirement', '-c', '--constraint'):
            return 'ignored', '嵌套引用（不递归解析）'
        if first in ('-e', '--editable'):
            return 'ignored', '可编辑安装（不支持）'
        if text.startswith('-'):
            return 'ignored', '选项行（不透传，遵循全局源配置）'
        # 本地路径形态（相对 / 盘符绝对 / UNC），URL scheme 形态不匹配此正则
        if _LOCAL_PATH_RE.match(text):
            return 'ignored', '本地路径安装（不支持）'
        # VCS / 直接 URL 规格：整行透传给 pip
        if text.lower().startswith(_URL_PREFIXES):
            return 'valid', text
        # name @ url 形式：包名与 URL 均合法时透传
        if ' @ ' in text:
            name, _, url = text.partition(' @ ')
            if _NAME_RE.match(name) and url.strip().lower().startswith(_URL_PREFIXES):
                return 'valid', text
        # 环境标记：分号前部分匹配规格正则时整行（含 marker）透传，marker 由 pip 自行评估
        spec = text.split(';', 1)[0].strip()
        if spec and _SPEC_RE.match(spec):
            return 'valid', text
        return 'unrecognized', None
    except Exception:
        return 'unrecognized', None


def parse_requirements_text(text: str) -> dict:
    """行级三分法解析（有效 / 忽略 / 无法识别）。

    输入：已解码文本（BOM 已剥离或由本函数兜底剥离）。
    返回 {valid: list[str], ignored: list[dict], unrecognized: list[dict],
          counts: dict, total_lines: int, overflow: bool}；
    overflow=True 表示总行数超上限（调用方据此整体拒绝，明细无需消费）。
    单行异常永不导致整体失败（宽容策略）。
    """
    text = (text or '').lstrip('\ufeff')
    lines = text.splitlines()
    total = len(lines)
    if total > MAX_TOTAL_LINES:
        return {
            'valid': [], 'ignored': [], 'unrecognized': [],
            'counts': {'valid': 0, 'ignored': 0, 'unrecognized': 0},
            'total_lines': total, 'overflow': True,
        }
    valid, ignored, unrecognized = [], [], []
    for idx, raw in enumerate(lines, start=1):
        kind, payload = _classify_line(raw)
        if kind == 'valid':
            valid.append(payload)
        elif kind == 'ignored':
            ignored.append({'line': idx, 'text': raw.strip(), 'reason': payload})
        else:
            unrecognized.append({'line': idx, 'text': raw.strip()})
    return {
        'valid': valid,
        'ignored': ignored,
        'unrecognized': unrecognized,
        'counts': {'valid': len(valid), 'ignored': len(ignored),
                   'unrecognized': len(unrecognized)},
        'total_lines': total,
        'overflow': False,
    }


def read_requirements_file(path: str) -> dict:
    """本机路径五项复检 + 读取（REQ-NEW-013，顺序即失败消息优先级）。

    五项复检：①存在 ②是单个文件 ③.txt 扩展名 ④大小 ≤ 1MB ⑤UTF-8 可解码
    （utf-8-sig 自动剥离 BOM，严格拒绝 GBK 等其他编码）。
    返回 {success, content, message}；content 仅在 success=True 时有意义。
    ①④与⑤之间存在 TOCTOU 窗口，窗口期异常统一收敛为同步拒绝，
    任何失败均不产生部分读取状态。UNC 路径与普通路径走完全相同的校验。
    """
    path = (path or '').strip()
    if not path:
        return {'success': False, 'content': '', 'message': '未指定文件路径'}
    p = Path(path)
    # ① 存在性
    if not p.exists():
        return {'success': False, 'content': '', 'message': f'文件不存在或无法访问：{path}'}
    # ② 单个文件（目录拒绝）
    if not p.is_file():
        return {'success': False, 'content': '', 'message': '仅支持选择单个 .txt 文件'}
    # ③ 扩展名（服务端权威校验：前端 JieDirPicker 过滤可被地址栏绕过）
    if p.suffix.lower() != REQ_TXT_SUFFIX:
        return {'success': False, 'content': '', 'message': '仅支持选择单个 .txt 文件'}
    # ④ 大小上限
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return {'success': False, 'content': '', 'message': '文件超过 1MB 上限'}
    except OSError:
        return {'success': False, 'content': '', 'message': f'文件不存在或无法访问：{path}'}
    # ⑤ 读取 + 严格 UTF-8 解码（窗口期异常兜底）
    try:
        raw = p.read_bytes()
    except PermissionError:
        return {'success': False, 'content': '',
                'message': '无权读取该文件，请检查文件权限或复制内容后改用粘贴入口'}
    except (FileNotFoundError, OSError):
        # ①④ 与读取之间文件被删改（TOCTOU 窗口）
        return {'success': False, 'content': '', 'message': f'文件不存在或无法访问：{path}'}
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return {'success': False, 'content': '', 'message': '仅支持 UTF-8 文本文件'}
    return {'success': True, 'content': content, 'message': ''}


def summarize_ignored(counts: dict, ignored: list) -> str:
    """生成忽略 / 无法识别的原因分类汇总文案（REQ-NEW-005）。

    如 "3 行注释、2 行空行、1 行选项被忽略，2 行无法识别"；
    没有任何忽略与无法识别行时返回 "文件内容为空"。
    """
    stats = {}
    for item in ignored or []:
        reason = item.get('reason', '')
        key = _IGNORED_SHORT_NAMES.get(reason) or reason.split('（')[0] or '其他'
        stats[key] = stats.get(key, 0) + 1
    ignored_part = ''
    if stats:
        parts = [f'{n} 行{k}' for k, n in sorted(stats.items(), key=lambda kv: -kv[1])]
        ignored_part = '、'.join(parts) + '被忽略'
    unrecognized_count = (counts or {}).get('unrecognized', 0)
    unrecognized_part = f'{unrecognized_count} 行无法识别' if unrecognized_count else ''
    if ignored_part and unrecognized_part:
        return f'{ignored_part}，{unrecognized_part}'
    if ignored_part or unrecognized_part:
        return ignored_part or unrecognized_part
    return '文件内容为空'
