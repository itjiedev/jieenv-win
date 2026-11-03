"""
平台升级独立脚本（脱离 Django 机制）。

用法：
    python upgrade_platform.py platform-1.32.1.zip
    python upgrade_platform.py path/to/any-platform-zip.zip --env-root D:\\target\\jieEnv-win

特性：
- 零 Django import：前半部分全用标准库（pathlib / shutil / zipfile / importlib）
- 升级包暂存 tmp（tempfile.mkdtemp），解压 → 备份 → 合并覆盖 → 后处理 → 清理
- 合并模式覆盖（dirs_exist_ok=True）：zip 有什么覆盖什么，ENV_ROOT 原有独有文件保留
- 精确回滚：收集原有文件集合 → 失败时恢复备份旧文件 + 删掉升级新增文件
- 后处理覆盖：pip install（zip 自带 requirements.txt）→ migrate → upgrade.py 钩子
- 骨架应用发现：纯磁盘扫描 APPS_DIR 下 app_meta.py 的 is_platform_fixed 字段
- 升级前必须先停服务，升级后必须重启
- --env-root 参数：允许从任意位置调用（如 SFX 自解压 exe 解压到 temp 后指定目标）

路径布局（脚本自己推算，不依赖 Django settings）：
    默认 ENV_ROOT = upgrade_platform.py 所在目录
    显式 ENV_ROOT = --env-root 参数值
    BASE_DIR = ENV_ROOT / jieenv
    APPS_DIR = BASE_DIR / apps
    PYTHON = ENV_ROOT / runtime / python.exe
"""
import os
import sys
import shutil
import zipfile
import importlib.util
import subprocess
import tempfile
import argparse
import ctypes
from pathlib import Path


def _get_cmd_encoding() -> str:
    """同 jiecore.utils.get_cmd_encoding，SFX 独立脚本零 Django import 需自实现。
    cmd.exe 走系统 OEM 代码页（中文 cp936），GetOEMCP() 比 locale 可靠。"""
    if os.name != 'nt':
        return 'utf-8'
    try:
        return f'cp{ctypes.windll.kernel32.GetOEMCP()}'
    except Exception:
        return 'gbk'


_CMD_ENC = _get_cmd_encoding()

# 确保脚本自身目录在 sys.path 最前面
# 场景：SFX 解压到 Downloads\，bat 先 cd 到 D:\jieEnv（ENV_ROOT），再调 python Downloads\upgrade_platform.py
# 此时 cwd = D:\jieEnv，而 _build_helper.py 在 Downloads\，裸 import 会找不到
_SCRIPT_SELF_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_SELF_DIR))

# 注意：这些全局变量名 _SCRIPT_DIR 实际存的是 ENV_ROOT（安装目录），
# 用来做备份/合并/后处理的根路径，不是脚本所在目录
_PYTHON = None
_MANAGE_PY = None
_SCRIPT_DIR = None   # = ENV_ROOT
_BASE_DIR = None     # = ENV_ROOT / jieenv


def _init_paths(env_root: Path):
    global _PYTHON, _MANAGE_PY, _SCRIPT_DIR, _BASE_DIR
    _SCRIPT_DIR = env_root.resolve()
    _BASE_DIR = _SCRIPT_DIR / 'jieenv'
    _PYTHON = _SCRIPT_DIR / 'runtime' / 'python.exe'
    _MANAGE_PY = _BASE_DIR / 'manage.py'

    import _build_helper as helper
    helper.SCRIPT_DIR = _SCRIPT_DIR
    helper.BASE_DIR = _BASE_DIR
    helper.APPS_DIR = _BASE_DIR / 'apps'
    return helper


def _make_ignore(*exclude_dirs: Path):
    """
    生成 copytree 可用的 ignore 回调，排除指定的绝对目录。
    防止备份/覆盖时把升级临时目录、残留备份目录又递归复制进去，
    导致路径无限嵌套最终触发 WinError 206（文件名或扩展名太长）。
    """
    exclude_resolved = {p.resolve() for p in exclude_dirs if p.exists()}

    def _ignore(src, names):
        if not exclude_resolved:
            return set()
        src_path = Path(src).resolve()
        ignored = set()
        for name in names:
            if (src_path / name).resolve() in exclude_resolved:
                ignored.add(name)
        return ignored

    return _ignore


def _collect_dir_relpaths(dir_path: Path) -> set:
    rels = set()
    if not dir_path.exists() or not dir_path.is_dir():
        return rels
    for root, _dirs, files in os.walk(dir_path):
        for f in files:
            full = Path(root) / f
            rels.add(str(full.relative_to(dir_path)).replace(os.sep, '/'))
    return rels


def _rollback_merge(target: Path, backup: Path, original_files: set) -> None:
    if backup.is_dir():
        shutil.copytree(backup, target, dirs_exist_ok=True)
        current_files = _collect_dir_relpaths(target)
        for rel in current_files - original_files:
            extra = target / rel
            if extra.exists():
                extra.unlink()
    else:
        if target.exists():
            target.unlink()
        shutil.copy2(backup, target)


def _run_upgrade_hook(app_name: str, old_version: str, new_version: str) -> None:
    import _build_helper as helper
    hook_file = helper.get_app_dir(app_name) / 'upgrade.py'
    if not hook_file.exists():
        return
    spec = importlib.util.spec_from_file_location(f'_upgrade_hook_{app_name}', hook_file)
    if not (spec and spec.loader):
        print(f'  [SKIP] {app_name}/upgrade.py cannot load')
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hook = getattr(mod, 'upgrade', None)
    if not callable(hook):
        print(f'  [SKIP] {app_name}/upgrade.py missing upgrade func')
        return
    print(f'  [HOOK] {app_name}/upgrade.py  ({old_version} -> {new_version})')
    hook(old_version, new_version)


def _decode_out(result: subprocess.CompletedProcess) -> str:
    out = ((result.stdout or b'') + b'\n' + (result.stderr or b'')).decode(_CMD_ENC, errors='replace')
    return out.strip()


def _run_pip(requirements: Path) -> None:
    cmd = [str(_PYTHON), '-m', 'pip', 'install', '-r', str(requirements)]
    print(f'  [PIP] pip install -r {requirements.name}')
    result = subprocess.run(cmd, capture_output=True)
    out_text = _decode_out(result)
    if result.returncode != 0:
        raise RuntimeError(f'pip install failed (exit={result.returncode}):\n{out_text}')
    if out_text:
        for line in out_text.splitlines()[-5:]:
            print(f'         {line}')


def _run_migrate() -> None:
    cmd = [str(_PYTHON), str(_MANAGE_PY), 'migrate', '--noinput']
    print(f'  [MIGRATE] python manage.py migrate --noinput')
    result = subprocess.run(cmd, capture_output=True, cwd=str(_BASE_DIR))
    out_text = _decode_out(result)
    if result.returncode != 0:
        raise RuntimeError(f'migrate failed (exit={result.returncode}):\n{out_text}')
    for line in out_text.splitlines()[-5:]:
        print(f'           {line}')


def _run_apprescan() -> None:
    cmd = [str(_PYTHON), str(_MANAGE_PY), 'apprescan', '--all', '--no-deps', '--no-migrate']
    print(f'  [APPSCAN] python manage.py apprescan --all --no-deps --no-migrate')
    result = subprocess.run(cmd, capture_output=True, cwd=str(_BASE_DIR))
    out_text = _decode_out(result)
    if result.returncode != 0:
        raise RuntimeError(f'apprescan failed (exit={result.returncode}):\n{out_text}')
    for line in out_text.splitlines()[-8:]:
        print(f'            {line}')


def _run_refreshcache() -> None:
    cmd = [str(_PYTHON), str(_MANAGE_PY), 'refreshcache', '--sync']
    print(f'  [REFRESH] python manage.py refreshcache --sync')
    result = subprocess.run(cmd, capture_output=True, cwd=str(_BASE_DIR))
    out_text = _decode_out(result)
    if result.returncode != 0:
        raise RuntimeError(f'refreshcache failed (exit={result.returncode}):\n{out_text}')
    for line in out_text.splitlines()[-5:]:
        print(f'            {line}')


def _run_checkdeps() -> None:
    cmd = [str(_PYTHON), str(_MANAGE_PY), 'checkdeps']
    print(f'  [DEPS] python manage.py checkdeps')
    result = subprocess.run(cmd, capture_output=True, cwd=str(_BASE_DIR))
    out_text = _decode_out(result)
    if result.returncode != 0:
        raise RuntimeError(f'checkdeps failed (exit={result.returncode}):\n{out_text}')
    for line in out_text.splitlines()[-8:]:
        print(f'            {line}')


UNINSTALL_REG_KEY = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\JieEnv-win'


def _update_registry_version(env_root: Path, new_version: str) -> None:
    """升级成功后，同步更新注册表卸载项的 DisplayVersion 和 InstallLocation。

    如果注册表里没有 JieEnv-win 项（首次安装未走 install.bat），
    则补写完整注册表（模仿 install.bat 的逻辑），确保以后升级/卸载都能找到。
    """
    try:
        import winreg
    except ImportError:
        print('  [REG] winreg 不可用，跳过注册表更新')
        return

    install_dir = str(env_root.resolve())
    if install_dir.endswith('\\'):
        install_dir = install_dir[:-1]

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_KEY, 0, winreg.KEY_SET_VALUE)
        reg_exists = True
    except FileNotFoundError:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_KEY)
        reg_exists = False
        print('  [REG] 注册表里没有 JieEnv-win 项，补建完整条目')
    except OSError as e:
        print(f'  [WARN] 打开注册表失败: {e}')
        return

    try:
        winreg.SetValueEx(key, 'DisplayVersion', 0, winreg.REG_SZ, new_version)
        winreg.SetValueEx(key, 'InstallLocation', 0, winreg.REG_SZ, install_dir)
        if not reg_exists:
            winreg.SetValueEx(key, 'DisplayName', 0, winreg.REG_SZ, 'JieEnv(win)环境管理')
            winreg.SetValueEx(key, 'Publisher', 0, winreg.REG_SZ, 'JieSoft')
            winreg.SetValueEx(key, 'UninstallString', 0, winreg.REG_SZ, install_dir + r'\uninstall.bat')
            winreg.SetValueEx(key, 'NoModify', 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, 'NoRepair', 0, winreg.REG_DWORD, 1)
            icon_path = env_root / 'favicon.ico'
            if icon_path.exists():
                winreg.SetValueEx(key, 'DisplayIcon', 0, winreg.REG_SZ, str(icon_path))
        print(f'  [REG] DisplayVersion -> {new_version}')
    except OSError as e:
        print(f'  [WARN] 注册表写入失败: {e}')
    finally:
        try:
            winreg.CloseKey(key)
        except OSError:
            pass


def _clean_path_string(raw: str) -> str:
    r"""把可能脏的路径字符串洗干净。

    可能的脏形态：
        "C:\xxx\jieenv-win" /f      ← 被误写入的完整 reg add 命令行
        C:\xxx\jieenv-win /         ← 尾部空格 + 斜杠
        C:\xxx\jieenv-win\          ← 尾反斜杠
        C:\xxx\jieenv-win"          ← 尾部多余引号
    """
    s = raw.strip()

    # 形态1: 引号包裹后带参数  "C:\xxx" /f  →  取引号内
    if s.startswith('"') and '" ' in s:
        s = s[1:s.index('" ')]

    # 去所有引号（包括首尾残留）
    s = s.replace('"', '')

    # 去首尾空白
    s = s.strip()

    # 循环去尾：去掉所有不属于合法路径结尾的字符
    # 合法路径结尾只能是字母数字 \ . 或 :
    while s and not s[-1].isalnum() and s[-1] not in '\\.:':
        s = s[:-1]
        s = s.rstrip()

    # 去尾反斜杠（保留根路径如 C:\）
    while s.endswith('\\') and not s.endswith(':\\'):
        s = s[:-1]

    return s.strip()


def _try_as_env_root(path_str: str) -> Path | None:
    """字符串 → Path → 验证是否为 jieEnv 安装目录（必须有 runtime/python.exe）"""
    if not path_str:
        return None
    try:
        p = Path(path_str).resolve()
    except (OSError, ValueError):
        return None
    if p.exists() and (p / 'runtime' / 'python.exe').exists():
        return p
    return None


def _detect_env_root(explicit: str = None) -> Path:
    """解析 ENV_ROOT，完整清洗 + 多级兜底。

    优先级：
    1. --env-root 显式指定（先清洗再验证，不行继续往下）
    2. 注册表 HKCU\\...\\Uninstall\\JieEnv-win\\InstallLocation（清洗 + 验证）
    3. 搜索常见安装位置
    4. fallback：__file__.parent（兼容便携场景）
    """
    candidates = []

    # --- 来源1: explicit（bat 传 --env-root） ---
    if explicit:
        raw = explicit
        cleaned = _clean_path_string(raw)
        if cleaned != raw:
            print(f'[INFO] --env-root 清洗: [{raw}] → [{cleaned}]')
        candidates.append(('CLI --env-root', cleaned))

    # --- 来源2: 注册表 ---
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_KEY) as key:
            value, _ = winreg.QueryValueEx(key, 'InstallLocation')
            cleaned = _clean_path_string(value)
            if cleaned != value:
                print(f'[INFO] 注册表 InstallLocation 清洗: [{value}] → [{cleaned}]')
            candidates.append(('注册表 InstallLocation', cleaned))
    except ImportError:
        print('[WARN] winreg 不可用，跳过注册表查找')
    except FileNotFoundError:
        print('[INFO] 注册表无 JieEnv-win 卸载记录')
    except OSError as e:
        print(f'[WARN] 读取注册表失败: {e}')

    # --- 来源3: 搜索常见安装位置 ---
    import os as _os
    username = _os.environ.get('USERNAME', '')
    for loc in [
        _os.environ.get('LOCALAPPDATA', '') + r'\Programs\jieenv-win',
        r'C:\jieEnv',
        r'D:\jieEnv',
        r'C:\jieenv-win',
        r'D:\jieenv-win',
        f'C:\\Users\\{username}\\jieEnv',
        f'D:\\jieEnv-win',
    ]:
        if loc and loc not in [c[1] for c in candidates]:
            candidates.append(('常见位置搜索', loc))

    # --- 逐个尝试验证 ---
    for source, path_str in candidates:
        result = _try_as_env_root(path_str)
        if result:
            print(f'[OK] 找到 jieEnv: {result}（来源: {source}）')
            return result
        else:
            # 打印诊断信息帮助排查
            p = Path(path_str)
            exists = p.exists()
            has_python = (p / 'runtime' / 'python.exe').exists() if exists else False
            print(f'[SKIP] {source}: [{path_str}] exists={exists} runtime/python.exe={has_python}')

    # --- 最终 fallback ---
    fallback = Path(__file__).resolve().parent
    print(f'[WARN] 所有候选都无效，fallback 到脚本目录: {fallback}')
    return fallback


def main(zip_path_str: str, env_root_str: str = None) -> int:
    zip_file = Path(zip_path_str).resolve()
    env_root = _detect_env_root(env_root_str)

    if not env_root.exists():
        print(f'\n[ERROR] env-root does not exist: {env_root}')
        return 1

    helper = _init_paths(env_root)

    print(f'\n{"="*60}')
    print(f'  jieEnv Platform Upgrade (Standalone)')
    print(f'{"="*60}')
    print(f'  ENV_ROOT:     {_SCRIPT_DIR}')
    print(f'  BASE_DIR:     {_BASE_DIR}')
    print(f'  Upgrade zip:  {zip_file.name}')

    if not zip_file.exists():
        print(f'\n[ERROR] zip not found: {zip_file}')
        return 1

    if not _PYTHON.exists():
        print(f'\n[ERROR] runtime\\python.exe not found at {_PYTHON}')
        return 1

    stem = zip_file.stem
    parts = stem.rsplit('-', 1)
    if len(parts) != 2 or parts[0] != 'platform':
        print(f'\n[ERROR] filename must be platform-<version>.zip, got: {zip_file.name}')
        return 1
    new_version = parts[1]

    current_version = helper.get_platform_version()
    print(f'  Current ver:  {current_version or "(unknown)"}')
    print(f'  Target ver:   {new_version}')

    if not current_version:
        print(f'\n[ERROR] cannot detect current platform version')
        return 1

    if helper.parse_version(new_version) <= helper.parse_version(current_version):
        print(f'\n[SKIP] target {new_version} <= current {current_version}, nothing to do')
        return 0

    tmp_dir = Path(tempfile.mkdtemp(prefix='jieenv_upg_'))
    print(f'  Temp dir:     {tmp_dir}')
    print(f'{"-"*60}')

    legacy_upgrade_dir = _BASE_DIR / 'tmp' / 'upgrade'
    if legacy_upgrade_dir.exists():
        print(f'\n[CLEAN] 清理残留升级临时目录: {legacy_upgrade_dir}')
        shutil.rmtree(legacy_upgrade_dir, ignore_errors=True)

    platform_backup_dir = tmp_dir / '__backup__'
    _ignore_upg = _make_ignore(legacy_upgrade_dir)
    original_files = {}
    items_to_update = []

    try:
        print('\n[1/8] Extracting zip...')
        with zipfile.ZipFile(zip_file, 'r') as zf:
            zf.extractall(tmp_dir)
        for item in tmp_dir.iterdir():
            if item.name == '__backup__':
                continue
            items_to_update.append(item.name)
        if not items_to_update:
            print('[ERROR] zip contains nothing to update')
            return 1
        print(f'       Items: {", ".join(items_to_update)}')

        print('\n[2/8] Backing up existing + collecting original file set...')
        for name in items_to_update:
            src_path = _SCRIPT_DIR / name
            if src_path.exists():
                dest_path = platform_backup_dir / name
                if src_path.is_dir():
                    shutil.copytree(src_path, dest_path, ignore=_ignore_upg)
                    original_files[name] = _collect_dir_relpaths(src_path)
                else:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, dest_path)
                    original_files[name] = set()
                print(f'       Backup: {name}')

        print('\n[3/8] Recording old fixed-app versions...')
        fixed_apps = helper.get_platform_fixed_apps()
        old_versions = {}
        for app_name in fixed_apps:
            meta = helper._read_meta_from_path(helper.get_app_dir(app_name))
            old_versions[app_name] = meta.get('version', '0.0.0')
            print(f'       {app_name}: {old_versions[app_name]}')

        print('\n[4/8] Merging files (dirs_exist_ok)...')
        for name in items_to_update:
            pkg_path = tmp_dir / name
            dest_path = _SCRIPT_DIR / name
            if pkg_path.is_dir():
                shutil.copytree(pkg_path, dest_path, dirs_exist_ok=True, ignore=_ignore_upg)
            else:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(pkg_path, dest_path)
            print(f'       Overwrite: {name}')

        print('\n[5/11] pip deps...')
        req_in_pkg = tmp_dir / 'requirements.txt'
        if req_in_pkg.exists():
            _run_pip(req_in_pkg)
        else:
            print('  [PIP] no requirements.txt in zip, skip')

        print('\n[6/11] Initial migrate (bootstrap tables)...')
        _run_migrate()

        print('\n[7/11] AppRegistry sync from disk + cache rebuild...')
        _run_refreshcache()

        print('\n[8/11] Full migrate (all apps)...')
        _run_migrate()

        print('\n[9/11] AppRegistry sync (apprescan --all --no-deps --no-migrate)...')
        _run_apprescan()

        print('\n[10/11] Running upgrade.py hooks...')
        for app_name in fixed_apps:
            new_ver = helper._read_meta_from_path(helper.get_app_dir(app_name)).get('version', old_versions[app_name])
            _run_upgrade_hook(app_name, old_versions[app_name], new_ver)

        print('\n[11/11] Dependency check (checkdeps)...')
        _run_checkdeps()

        print('\n[POST] 更新注册表卸载项版本...')
        _update_registry_version(_SCRIPT_DIR, new_version)

        print(f'\n{"="*60}')
        print(f'  [SUCCESS] Platform upgraded {current_version} -> {new_version}')
        print(f'  Overwritten: {", ".join(items_to_update)}')
        print(f'  RESTART required for jiecore changes to take effect')
        print(f'{"="*60}\n')

        if platform_backup_dir.exists():
            shutil.rmtree(platform_backup_dir, ignore_errors=True)
        return 0

    except KeyboardInterrupt:
        print(f'\n[INTERRUPT] rolling back...')
        _do_rollback(items_to_update, platform_backup_dir, original_files)
        return 130

    except Exception as e:
        print(f'\n[ERROR] upgrade failed: {e}')
        import traceback
        traceback.print_exc()
        _do_rollback(items_to_update, platform_backup_dir, original_files)
        return 1

    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _do_rollback(items_to_update: list, backup_dir: Path, original_files: dict) -> None:
    print('\n[ROLLBACK] restoring files...')
    if not backup_dir.exists():
        print('  (no backup, skip rollback)')
        return
    for name in items_to_update:
        backup_path = backup_dir / name
        target_path = _SCRIPT_DIR / name
        if not backup_path.exists():
            print(f'  [SKIP] no backup: {name}')
            continue
        try:
            _rollback_merge(target_path, backup_path, original_files.get(name, set()))
            print(f'  [OK] restored: {name}')
        except Exception as rb_err:
            print(f'  [FAIL] restore {name} failed: {rb_err}')
    shutil.rmtree(backup_dir, ignore_errors=True)
    print('[ROLLBACK] done, check system state\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='jieEnv Platform Upgrade Tool')
    parser.add_argument('zip_path', help='platform-<version>.zip file path')
    parser.add_argument('--env-root', default=None,
                        help='Target ENV_ROOT (default: same dir as this script)')
    args = parser.parse_args()
    sys.exit(main(args.zip_path, args.env_root))