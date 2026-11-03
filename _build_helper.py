"""
构建/升级共享工具函数。

纯标准库，零 Django import，供 build_platform.bat、upgrade_platform.py 等脚本复用。
所有函数基于 SCRIPT_DIR = Path(__file__).resolve().parent 推算路径，不依赖 settings。
"""
import ast
import os
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR / 'jieenv'
APPS_DIR = BASE_DIR / 'apps'
FRAMEWORK_APP = 'jiecore'


def _safe_eval(node):
    """递归安全求值 AST 节点，仅处理字面量结构。

    对 Constant/Str/Num/BoolOp/Dict/List/Tuple/Set/UnaryOp 做字面量解析；
    遇到 Call（函数调用）、IfExp（三元表达式）、Name（变量引用）、Attribute（属性访问）
    等无法安全推断的节点返回 None，让调用方忽略这些字段值。
    这样 app_meta.py 里有顶层 import 或函数调用也不会影响解析。
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        result = {}
        for k, v in zip(node.keys, node.values):
            key = _safe_eval(k)
            val = _safe_eval(v)
            if key is not None:
                result[key] = val
        return result
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_safe_eval(e) for e in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        operand = _safe_eval(node.operand)
        if isinstance(operand, (int, float)):
            return -operand if isinstance(node.op, ast.USub) else operand
    return None


def _read_meta_from_path(app_dir: Path) -> dict:
    meta_file = app_dir / 'app_meta.py'
    if not meta_file.exists():
        return {}
    try:
        source = meta_file.read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'meta':
                    if isinstance(node.value, ast.Dict):
                        return _safe_eval(node.value) or {}
    return {}


def get_platform_fixed_apps() -> list:
    """
    动态扫描所有平台骨架应用。
    来源：jiecore（框架内核自身）+ APPS_DIR 下所有 app_meta.py 声明 is_platform_fixed=True 的应用。
    新增/删除骨架应用只需改对应 app_meta.py，BAT/脚本无需任何改动。
    """
    result = [FRAMEWORK_APP]
    try:
        for child in APPS_DIR.iterdir():
            if child.is_dir() and not child.name.startswith('_'):
                meta = _read_meta_from_path(child)
                if meta.get('is_platform_fixed', False):
                    result.append(child.name)
    except OSError:
        pass
    return result


def get_app_dir(app_name: str) -> Path:
    if app_name == FRAMEWORK_APP:
        return BASE_DIR / app_name
    return APPS_DIR / app_name


def get_platform_version() -> str:
    """
    动态扫描平台版本号。
    遍历所有骨架应用，找第一个声明 platform_version 字段的；
    全没有则回退到 jiecore 自身 version。
    """
    for app_name in get_platform_fixed_apps():
        meta = _read_meta_from_path(get_app_dir(app_name))
        pv = meta.get('platform_version', '')
        if pv:
            return pv
    jc_meta = _read_meta_from_path(get_app_dir(FRAMEWORK_APP))
    return jc_meta.get('version', '0.0.0')


def parse_version(ver: str) -> tuple:
    def _split(v):
        parts = re.split(r'[.\-_]', v)
        result = []
        for p in parts:
            m = re.match(r'^(\d+)([a-z]+)?(\d*)$', p, re.IGNORECASE)
            if m:
                result.append(int(m.group(1)))
                if m.group(2):
                    tag = m.group(2).lower()
                    result.append({'a': 0, 'alpha': 0, 'b': 1, 'beta': 1,
                                   'rc': 2, 'post': 3, 'dev': 4}.get(tag, 5))
                    result.append(int(m.group(3)) if m.group(3) else 0)
        return tuple(result) if result else (0,)
    return _split(ver)


def scan_platform_items_for_staging() -> dict:
    """
    返回平台升级包应包含的所有目录条目，映射为：
        { 'staging 相对路径': '源路径（相对 SCRIPT_DIR）' }
    zip 顶层条目直接映射 ENV_ROOT，所以 staging 下的结构必须是：
        jieenv/jiecore/  ← 覆盖 ENV_ROOT/jieenv/jiecore/
        jieenv/apps/mainapp/ ← 覆盖 ENV_ROOT/jieenv/apps/mainapp/
        jieenv/apps/appmanage/ ← 覆盖 ENV_ROOT/jieenv/apps/appmanage/
        config/ ← 覆盖 ENV_ROOT/config/
        server.py desktop.py requirements.txt ← 覆盖 ENV_ROOT/xxx
    """
    items = {}
    fixed_apps = get_platform_fixed_apps()

    for app_name in fixed_apps:
        if app_name == FRAMEWORK_APP:
            src = 'jieenv/jiecore'
            dst = 'jieenv/jiecore'
        else:
            src = f'jieenv/apps/{app_name}'
            dst = f'jieenv/apps/{app_name}'
        if (SCRIPT_DIR / src).exists():
            items[dst] = src

    config_src = 'jieenv/config'
    if (SCRIPT_DIR / config_src).exists():
        items[config_src] = config_src

    for fname in (
        'server.py', 'desktop.py',
        'port_config.py',
        'requirements.txt',
        'upgrade_platform.py', '_build_helper.py',
        'jieenv/manage.py',
    ):
        if (SCRIPT_DIR / fname).exists():
            items[fname] = fname

    return items


if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'version'
    if cmd == 'version':
        print(get_platform_version())
    elif cmd == 'apps':
        print(','.join(get_platform_fixed_apps()))
    elif cmd == 'items':
        for dst, src in scan_platform_items_for_staging().items():
            print(f'{dst}|{src}')
    elif cmd == 'all':
        print(f'VERSION={get_platform_version()}')
        print(f'APPS={",".join(get_platform_fixed_apps())}')
        items = scan_platform_items_for_staging()
        print(f'ITEMS={len(items)}')
    else:
        print(f'Unknown cmd: {cmd}. Use: version | apps | items | all')