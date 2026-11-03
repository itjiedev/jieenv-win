"""
MySQL 社区版 ZIP 包自动下载与安装（Windows）。

安装流程（全部在后台线程执行，通过 tasks 模块汇报进度）：
1. 从官网 CDN / 归档地址下载 winx64 ZIP 包；
2. 解压到安装目录；
3. 生成 my.ini（端口/目录/字符集等选项）；
4. `mysqld --initialize-insecure` 初始化数据目录；
5. 注册为 Windows 服务并启动（需管理员权限），失败时降级为
   普通后台进程方式启动；
6. 等待端口就绪后为 root 账号设置密码（并创建 root@127.0.0.1）；
7. 将实例信息登记到 storage，供首页登录管理。

安装选项（options）:
    version        版本号，如 8.4.11
    url            zip 下载地址
    install_dir    安装根目录（实例会装到其下 mysql-<版本>-winx64 子目录）
    port           监听端口（默认 3306）
    root_password  root 密码（必填）
    service_name   Windows 服务名（默认 MySQL<版本去点>）
    as_service     是否尝试注册为 Windows 服务
    charset        服务器字符集（默认 utf8mb4）
"""
import re
import os
import socket
import zipfile
import logging
import subprocess
import urllib.request
from pathlib import Path

from config.constants import ENV_ROOT

from . import storage
from . import config as app_config
from .mysql_versions import BROWSER_HEADERS
from jiecore.system_ops import start_service
from jiecore.utils import get_cmd_encoding
from jiecore import tasks
from .mysql_service import register_mysql_service

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = app_config.DBMYSQL_TMP_DIR / 'mysql_download'


def _get_default_install_root() -> Path:
    """从应用配置读取默认安装根目录"""
    return Path(app_config.load_config().get('install_dir') or str(ENV_ROOT / 'databases'))

# 各阶段子进程超时（秒）
INIT_TIMEOUT = 600
SERVICE_CMD_TIMEOUT = 60

# 子进程隐藏窗口标志：防止执行 mysqld/net 等命令时闪现黑色控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

_MY_INI_TPL = """[mysqld]
port={port}
basedir={basedir}
datadir={datadir}
character-set-server={charset}
default-storage-engine=INNODB

[client]
port={port}
default-character-set={charset}

[mysql]
default-character-set={charset}
"""

class InstallError(Exception):
    """安装过程可预期错误，消息直接展示给用户"""


def _port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', int(port)), timeout=1):
            return True
    except (OSError, ValueError):
        return False


def _run(cmd: list, timeout: int = SERVICE_CMD_TIMEOUT, cwd: str = None) -> tuple:
    """执行命令，返回 (returncode, stdout+stderr 文本)"""
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd,
                          creationflags=_CREATE_NO_WINDOW)
    out = (proc.stdout or b'').decode(get_cmd_encoding(), errors='replace')
    err = (proc.stderr or b'').decode(get_cmd_encoding(), errors='replace')
    return proc.returncode, (out + '\n' + err).strip()


# ── 步骤实现 ─────────────────────────────────────────────

def _download(task: dict, url: str, version: str) -> Path:
    tasks.set_progress(task, progress=2, stage='下载安装包')
    tasks.append_log(task, f'开始下载: {url}')
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DOWNLOAD_DIR / f'mysql-{version}-winx64.zip'

    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    resp = urllib.request.urlopen(req, timeout=120)
    total = int(resp.headers.get('Content-Length') or 0)

    # 已下载完整则直接复用
    if zip_path.exists() and total and zip_path.stat().st_size == total:
        tasks.append_log(task, f'检测到已下载的完整安装包，直接复用: {zip_path}')
        tasks.set_progress(task, progress=55)
        return zip_path
    if zip_path.exists():
        zip_path.unlink()

    downloaded = 0
    chunk_size = 1024 * 256
    last_pct = -1
    with open(zip_path, 'wb') as f:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = int(downloaded * 53 / total)  # 下载占 2%~55%
                if pct != last_pct:
                    last_pct = pct
                    mb = downloaded / 1024 / 1024
                    total_mb = total / 1024 / 1024
                    tasks.set_progress(task, progress=2 + pct,
                                       stage=f'下载安装包 {mb:.1f}/{total_mb:.1f} MB')
    if total and downloaded < total:
        raise InstallError(f'下载不完整: 期望 {total} 字节，实际 {downloaded} 字节')
    tasks.append_log(task, f'下载完成: {zip_path}（{downloaded / 1024 / 1024:.1f} MB）')
    return zip_path


def _extract(task: dict, zip_path: Path, install_root: Path) -> Path:
    tasks.set_progress(task, progress=58, stage='解压安装包')
    tasks.append_log(task, f'解压到: {install_root}')
    install_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        members = zf.namelist()
        top_dirs = set(m.split('/', 1)[0] for m in members if '/' in m)
        total = len(members)
        # 逐个解压以汇报进度
        for i, member in enumerate(members):
            zf.extract(member, install_root)
            if i % 200 == 0 and total:
                tasks.set_progress(task, progress=58 + int(i * 12 / total))

    # 定位含 bin/mysqld.exe 的基础目录
    for top in top_dirs:
        base = install_root / top
        if (base / 'bin' / 'mysqld.exe').exists():
            tasks.append_log(task, f'解压完成，基础目录: {base}')
            return base
    # 兜底：扫一级子目录
    for base in install_root.iterdir():
        if base.is_dir() and (base / 'bin' / 'mysqld.exe').exists():
            return base
    raise InstallError('解压后未找到 bin/mysqld.exe，安装包可能异常')


def _write_ini(task: dict, base_dir: Path, port: int, charset: str) -> Path:
    ini_path = base_dir / 'my.ini'
    content = _MY_INI_TPL.format(
        port=port,
        basedir=str(base_dir).replace('\\', '/'),
        datadir=(base_dir / 'data').as_posix(),
        charset=charset,
    )
    ini_path.write_text(content, encoding='utf-8')
    tasks.append_log(task, f'已生成配置文件: {ini_path}')
    return ini_path


def _initialize(task: dict, base_dir: Path, ini_path: Path):
    tasks.set_progress(task, progress=72, stage='初始化数据目录')
    mysqld = str(base_dir / 'bin' / 'mysqld.exe')
    tasks.append_log(task, '执行 mysqld --initialize-insecure（可能需要几十秒）...')
    try:
        code, output = _run([mysqld, f'--defaults-file={ini_path}',
                             '--initialize-insecure', '--console'],
                            timeout=INIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise InstallError('初始化数据目录超时')
    if code != 0:
        tail = '\n'.join(output.splitlines()[-15:])
        raise InstallError(f'初始化数据目录失败:\n{tail}')
    tasks.append_log(task, '数据目录初始化完成')


def _start_server(task: dict, base_dir: Path, ini_path: Path,
                  service_name: str, as_service: bool) -> str:
    """启动 MySQL，返回运行模式 service/process"""
    mysqld = str(base_dir / 'bin' / 'mysqld.exe')
    run_mode = 'process'

    if as_service:
        tasks.set_progress(task, progress=80, stage='注册 Windows 服务')
        tasks.append_log(task, f'正在注册服务 {service_name}（可能需要管理员权限）...')
        reg_result = register_mysql_service(mysqld, service_name, str(ini_path))
        tasks.append_log(task, reg_result['message'])
        if reg_result['success']:
            # 使用 system_ops.start_service（自带 UAC 提权）
            result = start_service(service_name, on_step=lambda m: tasks.append_log(task, m))
            if result['success']:
                tasks.append_log(task, f'服务 {service_name} 已启动')
                return 'service'
            tasks.append_log(task, f'服务启动失败: {result["message"][:200]}')

    tasks.set_progress(task, progress=80, stage='启动 MySQL 进程')
    # 以分离的后台进程方式启动
    DETACHED = 0x00000008 | 0x00000200 | _CREATE_NO_WINDOW  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    subprocess.Popen([mysqld, f'--defaults-file={ini_path}'],
                     creationflags=DETACHED,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tasks.append_log(task, 'MySQL 已以后台进程方式启动（重启系统后需手动在本页重新启动）')
    return run_mode


def _wait_ready(task: dict, port: int, retries: int = 30) -> bool:
    import time
    for i in range(retries):
        if _port_in_use(port):
            return True
        time.sleep(2)
        if i % 5 == 4:
            tasks.append_log(task, f'等待 MySQL 端口 {port} 就绪...')
    return False


def _setup_root_password(task: dict, port: int, root_password: str):
    import MySQLdb
    tasks.set_progress(task, progress=90, stage='设置 root 密码')
    conn = None
    try:
        conn = MySQLdb.connect(host='127.0.0.1', port=int(port), user='root',
                               passwd='', connect_timeout=10)
        cur = conn.cursor()
        try:
            cur.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (root_password,))
        except Exception:
            # 旧版本(5.6及以下)回退语法
            cur.execute("SET PASSWORD FOR 'root'@'localhost' = PASSWORD(%s)", (root_password,))
        # 允许本机 TCP(127.0.0.1) 登录
        try:
            cur.execute("CREATE USER IF NOT EXISTS 'root'@'127.0.0.1' IDENTIFIED BY %s",
                        (root_password,))
            cur.execute("GRANT ALL PRIVILEGES ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION")
        except Exception as e:
            tasks.append_log(task, f'创建 root@127.0.0.1 失败（可忽略）: {e}')
        cur.execute('FLUSH PRIVILEGES')
        conn.commit()
        tasks.append_log(task, 'root 密码设置完成')
    finally:
        if conn:
            conn.close()


# ── 主流程 ───────────────────────────────────────────────

def validate_options(options: dict) -> str:
    """校验安装选项，返回错误消息（空串表示通过）"""
    if not re.match(r'^\d+\.\d+\.\d+$', options.get('version', '')):
        return '版本号格式不正确'
    url = options.get('url', '')
    if not url.startswith('https://'):
        return '下载地址必须为 https 链接'
    try:
        port = int(options.get('port') or 3306)
        if not 1 <= port <= 65535:
            return '端口超出范围'
    except ValueError:
        return '端口必须为数字'
    if len(options.get('root_password', '') or '') < 4:
        return 'root 密码至少 4 位'
    # 服务名留空合法，安装时自动生成默认名 MySQL<版本去点>（见 run_install）
    service_name = options.get('service_name', '')
    if service_name and not re.match(r'^[A-Za-z][A-Za-z0-9_\-]{0,31}$', service_name):
        return '服务名必须以字母开头，仅含字母/数字/下划线/中划线'
    if not re.match(r'^[a-z0-9_]+$', options.get('charset', 'utf8mb4')):
        return '字符集不合法'
    return ''


def run_install(task: dict, options: dict):
    """后台线程执行完整安装流程（含失败自动回滚清理）"""
    import shutil

    version = options['version']
    port = int(options.get('port') or 3306)
    install_root = Path(options.get('install_dir') or _get_default_install_root())

    # 回滚追踪：记录已创建/修改的资源，失败时逆序清理
    rollback = {
        'zip_path': None,       # 下载的 ZIP
        'base_dir': None,       # 解压目录
        'service_name': None,   # 已注册的 Windows 服务名
        'run_mode': None,       # service / process
        'record_id': None,      # 已登记的实例记录 ID
    }

    def _cleanup(phase: str):
        """失败回滚清理：按创建的逆序清理，确保不留残留"""
        tasks.append_log(task, f'⚠️ 安装失败（阶段: {phase}），开始回滚清理...')
        import traceback
        logger.warning('MySQL 安装失败触发回滚, phase=%s, rollback=%s\n%s',
                       phase, rollback, traceback.format_exc())
        errors = []

        # 7. 已登记 storage 记录 → 先移除
        if rollback['record_id']:
            try:
                storage.remove_instance(rollback['record_id'])
                tasks.append_log(task, '  - 已移除实例登记记录')
            except Exception as e:
                errors.append(f'移除实例记录失败: {e}')

        # 6. 已注册 Windows 服务 → 先停再卸
        if rollback['service_name'] and rollback['run_mode'] == 'service':
            try:
                from jiecore.system_ops import stop_service
                stop_service(rollback['service_name'])
            except Exception:
                pass
            try:
                from .mysql_service import remove_mysql_service
                remove_mysql_service(rollback['service_name'])
                tasks.append_log(task, f'  - 已卸载 Windows 服务 {rollback["service_name"]}')
            except Exception as e:
                errors.append(f'卸载服务失败: {e}')

        # 5. 进程模式启动的 mysqld → 终止
        if rollback['base_dir']:
            try:
                from .mysql_process import terminate_process
                terminate_process(port)
            except Exception:
                pass

        # 4. 解压目录 → 整体删除（含 data/my.ini/bin）
        if rollback['base_dir'] and rollback['base_dir'].exists():
            try:
                shutil.rmtree(str(rollback['base_dir']), ignore_errors=True)
                tasks.append_log(task, f'  - 已删除安装目录: {rollback["base_dir"]}')
            except Exception as e:
                errors.append(f'删除安装目录失败: {e}')

        # 3. 下载的 ZIP → 删除（已解压过，不再需要）
        if rollback['zip_path'] and rollback['zip_path'].exists():
            try:
                rollback['zip_path'].unlink()
                tasks.append_log(task, f'  - 已删除安装包: {rollback["zip_path"].name}')
            except Exception:
                pass

        if errors:
            tasks.append_log(task, f'  - 清理过程中有 {len(errors)} 项残留未清，'
                                  f'请手动检查（安装目录/服务/文件）')
        tasks.append_log(task, '回滚清理完成')

    # 主流程：try/except 包裹，任何失败都触发 _cleanup
    try:
        if _port_in_use(port):
            raise InstallError(f'端口 {port} 已被占用，请更换端口或先停止占用该端口的程序')

        # 1. 下载
        zip_path = _download(task, options['url'], version)
        rollback['zip_path'] = zip_path

        # 2. 解压
        base_dir = _extract(task, zip_path, install_root)
        rollback['base_dir'] = base_dir
        if (base_dir / 'data').exists():
            raise InstallError(f'数据目录已存在: {base_dir / "data"}，请更换安装目录或先删除')

        # 3. 生成配置
        ini_path = _write_ini(task, base_dir, port, options.get('charset', 'utf8mb4'))

        # 4. 初始化
        _initialize(task, base_dir, ini_path)

        # 5. 启动
        service_name = options.get('service_name') or f"MySQL{version.replace('.', '')}"
        rollback['service_name'] = service_name
        run_mode = _start_server(task, base_dir, ini_path, service_name,
                                 bool(options.get('as_service', True)))
        rollback['run_mode'] = run_mode

        # 6. 等待就绪并设置密码
        tasks.set_progress(task, progress=85, stage='等待 MySQL 就绪')
        if not _wait_ready(task, port):
            raise InstallError(f'MySQL 启动超时（端口 {port} 未就绪），请检查日志')
        _setup_root_password(task, port, options['root_password'])

        # 7. 登记实例
        tasks.set_progress(task, progress=96, stage='登记实例信息')
        record = storage.add_instance({
            'name': f'MySQL {version} ({service_name if run_mode == "service" else "进程模式"})',
            'host': '127.0.0.1',
            'port': port,
            'version': version,
            'source': 'installed',
            'install_dir': str(base_dir),
            'data_dir': str(base_dir / 'data'),
            'service_name': service_name if run_mode == 'service' else '',
            'run_mode': run_mode,
            'default_user': 'root',
            'remark': f'由 dbmysql 应用自动安装，字符集 {options.get("charset", "utf8mb4")}',
        })
        rollback['record_id'] = record['id']
        task['result'] = {'instance_id': record['id'], 'port': port, 'run_mode': run_mode}
        tasks.append_log(task, f'安装完成！实例已登记，端口 {port}，用户 root，可回到首页登录管理')

    except (InstallError, subprocess.TimeoutExpired) as e:
        # 可预期错误（版本问题/端口占用/超时）→ 清理后重新抛出
        _cleanup(str(e))
        raise
    except Exception as e:
        # 不可预期错误 → 清理后重新抛出
        _cleanup(f'未预期错误: {e}')
        raise