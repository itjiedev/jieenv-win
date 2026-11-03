"""
MySQL 进程管理模块（Windows 进程层纯函数）。

定位：不导入 Django、不读写实例记录、不操作任务注册表，与 mysql_resolve.py
同层同风格；供 views.py 的进程启停/重启后台任务调用。

能力:
- 进程定位: netstat -ano 取端口监听 PID + tasklist 取映像名，端口 + 映像名
  双重校验，防止误杀占用同端口的其他程序
- 进程终止: 两阶段策略（温和 taskkill 不带 /F → 宽限期等待 → 强制 /F /T 降级），
  以当前用户权限执行，不提权
- 端口等待: 复用 mysql_resolve.check_port 实时探测（不走缓存），
  启动等端口就绪 / 停止等端口释放
- mysqld 拉起: --defaults-file 首参数位 + DETACHED flags，
  使 mysqld 脱离 jieEnv 进程树独立存活
"""
import os
import time
import logging
import subprocess
from pathlib import Path

from .mysql_resolve import check_port
from jiecore.utils import get_cmd_encoding

logger = logging.getLogger(__name__)

# mysqld 映像名比对基准（tasklist 返回值统一小写比较）
MYSQLD_IMAGE = 'mysqld.exe'
# 子进程统一超时（秒）
_CMD_TIMEOUT = 15
# 子进程隐藏窗口标志：防止 netstat/tasklist/taskkill 闪现黑色控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 启动端口就绪上限（秒）
START_READY_TIMEOUT = 60
# 停止总上限（秒，含温和宽限期）
STOP_CONFIRM_TIMEOUT = 30
# 温和终止宽限期（秒）
GRACE_PERIOD = 15
# 端口轮询间隔（秒），与既有 _run_service_task 的 2s 节奏一致
POLL_INTERVAL = 2
# 强制终止后端口释放确认下限（秒）
MIN_FORCE_CONFIRM = 5

def _run(cmd: list, timeout: int = _CMD_TIMEOUT) -> tuple:
    """执行命令返回 (returncode, stdout_text, stderr_text)，失败返回 (-1, '', 原因)，不抛异常"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=_CREATE_NO_WINDOW,
                              encoding=get_cmd_encoding(), errors='backslashreplace')
        return (proc.returncode, proc.stdout, proc.stderr)
    except Exception as e:
        logger.debug('命令执行失败 %s: %s', cmd, e)
        return (-1, '', str(e))


def find_mysqld_pids_by_port(port: int) -> list:
    """按端口查找 TCP 监听进程 PID，覆盖 IPv4/IPv6 双栈，失败返回空列表"""
    _, out, _ = _run(['netstat', '-ano', '-p', 'tcp'])
    if not out:
        return []
    suffix = f':{port}'
    pids = []
    for line in out.splitlines():
        parts = line.split()
        # 形如: TCP  0.0.0.0:3306  0.0.0.0:0  LISTENING  1234
        if len(parts) < 5 or 'LISTENING' not in parts:
            continue
        # 本地地址形如 0.0.0.0:3306 / [::]:3306 / 127.0.0.1:3306
        if not parts[1].endswith(suffix):
            continue
        pid = parts[-1]
        if pid.isdigit() and int(pid) not in pids:
            pids.append(int(pid))
    return pids


def get_process_image_name(pid: int) -> str:
    """按 PID 查询进程映像名（小写返回），查不到返回空串（不抛异常）"""
    _, out, _ = _run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'])
    line = out.strip().splitlines()[0].strip() if out.strip() else ''
    if not line:
        return ''
    head = line.split(',')[0].strip('"').lower()
    # 无匹配时 tasklist 输出提示行（INFO: / 信息: ...），非 CSV 数据
    if not head.endswith('.exe'):
        return ''
    return head


def process_exists(pid: int) -> bool:
    """按 PID 确认真实退出（tasklist 查得 .exe 实体行即存活）。

    保守语义：命令异常/查询失败返回 False（视为"进程已退出"），
    避免因查询故障误伤停止成功路径。不抛异常。
    """
    return get_process_image_name(pid) != ''


def locate_mysqld_process(port: int) -> dict:
    """按端口定位 mysqld 进程（端口 + 映像名双重校验，防止误杀其他程序）。

    返回 {found, pid, image_name, reason}；found=False 时 reason 说明原因。
    不抛异常。
    """
    pids = find_mysqld_pids_by_port(port)
    if not pids:
        return {'found': False, 'pid': None, 'image_name': '',
                'reason': f'端口 {port} 无监听进程'}
    identified = []
    for pid in pids:
        image = get_process_image_name(pid)
        if image == MYSQLD_IMAGE:
            return {'found': True, 'pid': pid, 'image_name': image, 'reason': ''}
        if image:
            identified.append((pid, image))
    if not identified:
        return {'found': False, 'pid': pids[0], 'image_name': '',
                'reason': '无法查询监听进程的映像名，已取消操作'}
    pid0, img0 = identified[0]
    return {'found': False, 'pid': pid0, 'image_name': img0,
            'reason': f'监听端口 {port} 的进程为 {img0}（PID {pid0}），'
                      f'不是 mysqld，已取消操作以避免误杀其他程序'}


def terminate_process(pid: int, force: bool = False) -> tuple:
    """终止进程：温和尝试（不带 /F）或强制结束（/F /T 含子进程树）。

    返回 (ok, detail)：ok 以 taskkill 退出码 rc==0 为主判据（兼容中文系统
    SUCCESS 输出非 ASCII）；detail 为 stdout/stderr 摘要，供任务日志如实展示。
    端口是否释放由 wait_port_released 确认；以当前用户权限执行，不提权；不抛异常。
    """
    if force:
        cmd = ['taskkill', '/F', '/T', '/PID', str(pid)]
    else:
        cmd = ['taskkill', '/PID', str(pid)]
    rc, out, err = _run(cmd)
    if rc == 0:
        detail = (out or err or '').strip() or f'exit code {rc}'
        logger.info('taskkill PID %s (force=%s) rc=%s: %s', pid, force, rc, detail[:120])
        return (True, detail)
    detail = (err or out or '').strip() or f'exit code {rc}'
    logger.info('taskkill PID %s (force=%s) failed: %s', pid, force, detail[:120])
    return (False, detail)


def wait_port_ready(host: str, port: int, timeout: float = START_READY_TIMEOUT) -> bool:
    """循环探测端口直到可连通或超时（实时直连，不走缓存），不抛异常"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check_port(host, port):
            return True
        time.sleep(POLL_INTERVAL)
    return check_port(host, port)


def wait_port_released(host: str, port: int, timeout: float = STOP_CONFIRM_TIMEOUT) -> bool:
    """循环探测端口直到不再监听或超时（以实例 host 实时直连，不抛异常）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not check_port(host, port):
            return True
        time.sleep(POLL_INTERVAL)
    return not check_port(host, port)


def launch_mysqld(mysqld_exe: str, ini_path) -> int:
    """以 DETACHED 后台进程拉起 mysqld，返回新进程 PID。

    --defaults-file 必须为首参数位（mysqld 参数顺序要求）；
    拉起失败（文件缺失/权限不足/参数错误）抛 RuntimeError（人可读原因）。
    """
    exe = str(mysqld_exe)
    if not Path(exe).exists():
        raise RuntimeError(f'mysqld 可执行文件不存在: {exe}')
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    detached = 0x00000008 | 0x00000200 | 0x08000000
    try:
        proc = subprocess.Popen(
            [exe, f'--defaults-file={ini_path}'],
            creationflags=detached,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        raise RuntimeError(f'启动 mysqld 失败: {e}')
    return proc.pid
