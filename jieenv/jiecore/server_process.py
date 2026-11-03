"""
服务进程 PID 管理
提供 PID 文件的读写、进程存活检测与跨平台优雅停止能力。

约定：
- PID 文件位于 tmp/server.pid，由进程启动时（apps.ready）自动登记；
- PID 文件为 JSON，记录 pid / role / start_time / argv，用于身份校验：
  已有存活的其他角色登记时拒绝覆盖（桌面壳附着模式不会顶掉真实服务的 PID），
  停止前校验 start_time 一致，避免 PID 被系统回收复用后误杀无关进程；
- runserver 自动重载场景登记的是父进程（监视进程），停止时连同子进程一并终止；
- 重启（restart_server）后新进程会在启动时重新登记 PID，无需外部干预。

进程角色：
- 由环境变量 JIEENV_ROLE 显式声明（server / desktop / cli），优先级最高；
- 未声明时按 argv 推断：runserver 与 server.py 为 server，desktop.py 为
  desktop，manage.py 为 cli；其余未知入口保守按 server 处理，避免自定义生产
  入口静默丢失 PID 登记，一次性脚本请显式设 JIEENV_ROLE=cli。

停止策略：
- Windows：先写停止请求文件让服务进程自行优雅退出（关闭 WSGI 服务器释放端口、
  flush 日志），超时后降级为 taskkill /F /T 终止整个进程树；
- Linux：先向进程组发送 SIGTERM 优雅退出，超时后升级为 SIGKILL；
- runserver 模式下 PID 登记在父（监视）进程上，停止请求通知不到真正处理请求
  的子进程，故登记时标记 graceful=False，直接走强制终止。
"""
import os
import sys
import json
import time
import signal
import logging
import threading
import subprocess

from django.conf import settings

logger = logging.getLogger(__name__)

PID_FILE = settings.TMP_DIR / 'server.pid'
# 停止请求文件：stop_server 写入，服务进程内的监视线程轮询到后优雅退出。
# Windows 下 taskkill 只有 /F（强杀）可用，脱离控制台的进程收不到 Ctrl+C，
# 故用文件信号补出 Linux SIGTERM 那样的优雅退出窗口
STOP_REQUEST_FILE = settings.TMP_DIR / 'server.stop'
# 重启请求文件：子进程模式（壳-子进程拆分）下，服务子进程收到重启请求时写入此文件
# 后自行退出，壳进程检测到子进程退出 + 此文件存在 → 拉新子进程 → 窗口 reload
RESTART_REQUEST_FILE = settings.TMP_DIR / 'server.restart'

STOP_WATCH_INTERVAL = 0.5       # 停止请求轮询间隔
_WATCHER_THREAD_NAME = 'jie-stop-watcher'

# 壳-子进程拆分模式标记：desktop.py 启动 server.py 子进程时注入，
# 服务进程据此知道重启应委托壳处理（写重启文件 + 退出），而非自行 Popen
SUBPROCESS_MODE_ENV = 'JIEENV_SUBPROCESS_MODE'

# 重启互斥锁：防止并发重启拉起多个进程抢端口
_restart_lock = threading.Lock()

# 进程角色。显式声明优先于按 argv 推断，避免“入口脚本改名/被包装即失效”
ROLE_ENV = 'JIEENV_ROLE'
ROLE_SERVER = 'server'      # 对外提供 Web 服务的进程（server.py / runserver）
ROLE_DESKTOP = 'desktop'    # 桌面壳且自行启动 waitress
ROLE_CLI = 'cli'            # 一次性命令、临时脚本、附着模式的桌面壳

# 未显式声明角色时按入口文件名推断（项目实际存在的入口）
_SERVER_ENTRIES = {'server.py'}
_DESKTOP_ENTRIES = {'desktop.py'}


def get_role() -> str:
    """返回当前进程角色：环境变量显式声明优先，其次按 argv 推断"""
    role = os.environ.get(ROLE_ENV, '').strip().lower()
    if role in (ROLE_SERVER, ROLE_DESKTOP, ROLE_CLI):
        return role
    if 'runserver' in sys.argv:
        return ROLE_SERVER
    argv0 = os.path.basename(sys.argv[0]).lower()
    if argv0 in _DESKTOP_ENTRIES:
        return ROLE_DESKTOP
    if argv0 in _SERVER_ENTRIES:
        return ROLE_SERVER
    if argv0 == 'manage.py':
        return ROLE_CLI
    return ROLE_SERVER


def is_server_process() -> bool:
    """
    判断当前进程是否为服务进程（区别于一次性的 manage.py 命令）。
    兼容保留的旧接口，等价于 get_role() != ROLE_CLI；
    新代码请使用 get_role()，按角色分别决策 PID 登记。
    """
    return get_role() != ROLE_CLI


def is_autoreload_child() -> bool:
    """
    判断当前进程是否为 runserver 自动重载的子进程（工作进程）。
    兼容新旧 Django：
    - Django < 5.2：子进程设置 RUN_MAIN=true；
    - Django >= 5.2：子进程设置 DJANGO_AUTORELOAD_ENV=true。
    """
    if os.environ.get('RUN_MAIN') == 'true':
        return True
    try:
        from django.utils.autoreload import DJANGO_AUTORELOAD_ENV
        return os.environ.get(DJANGO_AUTORELOAD_ENV) == 'true'
    except ImportError:
        return False


def _process_start_time(pid: int) -> float:
    """取进程创建时间（epoch 秒），用于识别 PID 是否已被系统回收复用。
    psutil 不可用或查询失败时返回 0.0，此时身份校验自动退化为只判存活。"""
    if pid <= 0:
        return 0.0
    try:
        import psutil
        return float(psutil.Process(pid).create_time())
    except Exception:
        return 0.0


def _is_same_process(info: dict) -> bool:
    """校验 PID 文件记录的进程是否仍是当初登记的那一个。
    未记录 start_time（旧格式或 psutil 不可用）时退化为只判存活。"""
    try:
        recorded = float(info.get('start_time') or 0)
    except (TypeError, ValueError):
        return True
    if not recorded:
        return True
    actual = _process_start_time(int(info.get('pid', 0) or 0))
    if not actual:
        return True
    return abs(actual - recorded) < 1.0


def read_pid_info() -> dict:
    """读取 PID 文件全部内容。兼容旧格式：纯数字文本按 {'pid': N} 处理

    注意裸数字本身就是合法 JSON（json.loads('4321') 返回 int 而不报错），
    故不能用异常区分新旧格式，必须按解析结果的类型分派。
    """
    try:
        raw = PID_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        return data
    if isinstance(data, int) and not isinstance(data, bool):
        return {'pid': data}          # 旧格式：纯数字
    try:
        return {'pid': int(raw)}
    except ValueError:
        return {}


def write_pid_file(pid: int = None, role: str = None, graceful: bool = None) -> bool:
    """登记服务进程身份到 PID 文件，返回是否写入成功。

    已有存活进程且角色不同时拒绝覆盖：桌面壳附着模式下不得顶掉真实服务的
    PID，否则 stopserver 会去杀窗口而放过仍在监听端口的服务进程。

    graceful 标记本进程内是否跑着停止请求监视线程（见 start_stop_watcher）；
    缺省按是否 runserver 推断：runserver 登记的是父（监视）进程，停止请求
    通知不到真正处理请求的子进程，故不支持优雅停止。
    """
    pid = pid if pid is not None else os.getpid()
    role = role or get_role()
    if graceful is None:
        graceful = 'runserver' not in sys.argv
    existing = read_pid_info()
    try:
        old_pid = int(existing.get('pid', 0) or 0)
    except (TypeError, ValueError):
        old_pid = 0
    if old_pid and old_pid != pid and existing.get('role') != role \
            and is_process_alive(old_pid) and _is_same_process(existing):
        logger.warning('PID 文件已由 %s 进程 %s 持有，%s 进程 %s 不覆盖',
                       existing.get('role') or 'unknown', old_pid, role, pid)
        return False
    payload = {'pid': pid, 'role': role, 'graceful': bool(graceful),
               'start_time': _process_start_time(pid),
               'argv': ' '.join(sys.argv), 'written_at': time.time()}
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PID_FILE.with_suffix('.pid.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        tmp.replace(PID_FILE)
        return True
    except OSError as e:
        logger.warning('写入 PID 文件失败: %s', e)
        return False


def read_pid_file() -> int:
    """读取 PID，不存在或内容非法时返回 0（兼容保留的旧接口）"""
    try:
        return int(read_pid_info().get('pid', 0) or 0)
    except (TypeError, ValueError):
        return 0


def remove_pid_file():
    """删除 PID 文件（不存在时静默）"""
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError as e:
        logger.warning('删除 PID 文件失败: %s', e)


_kernel32 = None


def _win_kernel32():
    """取带 last-error 捕获且声明好签名的 kernel32 句柄（惰性单例）。

    必须显式声明 restype/argtypes：ctypes 缺省把返回值当 c_int，64 位下
    句柄会被截断；参数不声明则靠猜测传值，同样不可靠。"""
    global _kernel32
    if _kernel32 is None:
        import ctypes
        _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _kernel32.OpenProcess.restype = ctypes.c_void_p
        _kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool,
                                          ctypes.c_uint32]
        _kernel32.GetExitCodeProcess.restype = ctypes.c_bool
        _kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        _kernel32.CloseHandle.restype = ctypes.c_bool
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return _kernel32


def is_process_alive(pid: int) -> bool:
    """检测指定 PID 的进程是否仍在运行。

    Windows 下不能只看 OpenProcess 是否成功：进程退出后若仍有其他进程持有
    其句柄（父进程未回收、调试器附着等），内核对象会继续存在，OpenProcess
    照样返回有效句柄，但 taskkill 与进程枚举都已找不到它（僵尸态）。实测下
    这种 PID 会让 stopserver 报 taskkill 退出码 128 且永远清不掉陈旧 PID 文件，
    也会让 get_running_service() 把幽灵服务当真服务。故必须再查退出码，
    STILL_ACTIVE(259) 之外的值说明进程已终止。
    """
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        kernel32 = _win_kernel32()
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 拒绝访问说明进程存在，只是当前用户无权查询（与下方 POSIX 分支的
            # PermissionError 处理一致）；其余错误（如 87 参数无效）为进程不存在
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_uint32(STILL_ACTIVE)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True     # 查不到退出码时保守认为存活，交由 taskkill 定夺
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 信号被拒说明进程存在，只是当前用户无权操作
        return True


def get_running_service() -> dict:
    """返回当前已登记且确实存活的服务信息，无则返回 {}。

    “服务是否已在运行”曾有三套互不感知的判定（startserver 看 PID 文件、
    desktop.py 看命名互斥体+端口探测、server.py 硬撞端口重试），本函数作为
    单一真值源供各入口共用：既校验进程存活，也校验 start_time 防 PID 复用。
    """
    info = read_pid_info()
    try:
        pid = int(info.get('pid', 0) or 0)
    except (TypeError, ValueError):
        return {}
    if pid <= 0 or not is_process_alive(pid) or not _is_same_process(info):
        return {}
    return info


def is_service_running() -> bool:
    """是否已有登记在案且存活的服务进程"""
    return bool(get_running_service())


# ── 重启策略与 WSGI 服务器对象注册 ─────────────────────
# 桌面壳采用“壳-子进程拆分”模式（窗口不关闭，杀子进程→拉新子进程→窗口 reload），
# 而默认策略是关服务→拉新进程→退出当前进程。原先靠替换模块属性实现，
# 但 views.py 用的是 from ... import restart_server，名字已绑进各自命名空间，
# 必须两处都打补丁；任何新增的导入点都会静默失效并回退到杀进程重启。
# 改为集中注册：入口只调一次 register_restart_handler()，所有调用点自动生效。
_restart_handler = None
_close_wsgi_handler = None
_wsgi_server = None


def register_restart_handler(handler):
    """注册自定义重启实现（handler 无参，返回 dict）。传 None 可注销"""
    global _restart_handler
    _restart_handler = handler


def get_restart_handler():
    """取已注册的重启实现，未注册返回 None（走默认进程重生策略）"""
    return _restart_handler


def register_close_wsgi_handler(handler):
    """注册 WSGI 服务器关闭实现（handler 接收 server 参数，执行服务器特定的关闭）。
    传 None 可注销。入口脚本（server.py / desktop.py）应在启动时注册，
    将 waitress 等特定服务器的关闭逻辑留在入口侧，jiecore 不直接引用其 API。"""
    global _close_wsgi_handler
    _close_wsgi_handler = handler


def register_wsgi_server(server):
    """入口脚本登记自己的 WSGI 服务器对象，供关闭 handler 使用。"""
    global _wsgi_server
    _wsgi_server = server


def close_wsgi_server() -> bool:
    """优雅关闭 WSGI 服务器：委托给已注册的关闭 handler 执行。
    未注册 handler 时返回 False，由调用方决定后续动作。"""
    if _close_wsgi_handler is None:
        return False
    try:
        _close_wsgi_handler(_wsgi_server)
    except Exception as e:
        logger.debug('关闭 WSGI 服务器失败: %s', e)
    return True


# ── 优雅停止 ────────────────────────────────────
def write_stop_request(pid: int) -> bool:
    """写入停止请求，通知目标服务进程自行优雅退出"""
    try:
        STOP_REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_REQUEST_FILE.write_text(str(pid), encoding='utf-8')
        return True
    except OSError as e:
        logger.warning('写入停止请求失败: %s', e)
        return False


def is_subprocess_mode() -> bool:
    """当前进程是否运行在壳-子进程拆分模式下（由 desktop.py 启动的 server.py 子进程）"""
    return os.environ.get(SUBPROCESS_MODE_ENV) == '1'


def write_restart_request() -> bool:
    """写入重启请求文件，通知壳进程执行子进程重启"""
    try:
        RESTART_REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESTART_REQUEST_FILE.write_text(str(os.getpid()), encoding='utf-8')
        return True
    except OSError as e:
        logger.warning('写入重启请求失败: %s', e)
        return False


def remove_restart_request():
    """删除重启请求文件（不存在时静默）"""
    try:
        RESTART_REQUEST_FILE.unlink(missing_ok=True)
    except OSError as e:
        logger.warning('删除重启请求失败: %s', e)


def remove_stop_request():
    """删除停止请求文件（不存在时静默）。
    必须清理：残留的请求文件会让下一个启动的服务进程刚起来就自杀。"""
    try:
        STOP_REQUEST_FILE.unlink(missing_ok=True)
    except OSError as e:
        logger.warning('删除停止请求失败: %s', e)


def _stop_request_targets_me() -> bool:
    """当前进程是否为停止请求的目标（空内容视为广播，兼容手工创建）"""
    try:
        raw = STOP_REQUEST_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return False
    return raw in ('', str(os.getpid()))


def start_stop_watcher():
    """在服务进程内启动停止请求监视线程（幂等，重复调用只起一个）。

    轮询到属于自己的停止请求后：关闭 WSGI 服务器释放端口 → flush 日志 → 退出。
    这样 Windows 下也能有 Linux SIGTERM 那样的优雅退出窗口，不必直接 taskkill /F
    掐断（强杀会跳过 atexit、日志 flush 与 SQLite WAL checkpoint）。
    """
    if any(t.name == _WATCHER_THREAD_NAME for t in threading.enumerate()):
        return

    def _loop():
        while True:
            if STOP_REQUEST_FILE.exists() and _stop_request_targets_me():
                # 用 warning 而非 info：server.py 走 config.production，日志级别被
                # apply_log_level('WARNING') 压到 WARNING，INFO 不会落盘。“服务为何
                # 停了”是运维排查的关键事件，不能在生产模式下静默消失
                logger.warning('收到停止请求，正在优雅退出（由 stopserver 触发）')
                remove_stop_request()
                close_wsgi_server()
                time.sleep(0.3)         # 等 OS 回收端口
                _close_db_for_shutdown()  # 关 DB 连接，避免 WAL 残留
                logging.shutdown()      # flush 所有 handler，避免日志丢尾
                os._exit(0)
            time.sleep(STOP_WATCH_INTERVAL)

    threading.Thread(target=_loop, name=_WATCHER_THREAD_NAME, daemon=True).start()


def stop_server(pid: int = None, force: bool = False, timeout: float = 10.0) -> dict:
    """
    停止服务进程（树），默认目标为 PID 文件中记录的进程。

    两个平台都是“先优雅、超时再强制”：Windows 写停止请求文件等目标进程
    自行关闭 WSGI 服务器退出，超时后降级为 taskkill /F /T 终止整个进程树；
    Linux 先 SIGTERM，超时升级为 SIGKILL。timeout 为优雅退出的等待秒数。
    --force 跳过优雅阶段；目标未登记 graceful（如 runserver）时也直接强制终止。

    返回 dict: {success: bool, message: str}
    """
    info = read_pid_info()
    from_file = pid is None
    if from_file:
        pid = read_pid_file()
    if pid <= 0:
        remove_pid_file()
        remove_stop_request()
        return {'success': False, 'message': 'PID 文件不存在或无效，服务可能未通过 PID 文件管理'}
    # 身份校验仅对“读自 PID 文件”的目标生效：显式 --pid 由调用方自负其责
    if not is_process_alive(pid) or (from_file and not _is_same_process(info)):
        remove_pid_file()
        # 这两条提前返回路径绕过了下面的 finally，必须就地清理停止请求：
        # 残留的请求文件（尤其空内容被视为广播）会让下一个启动的服务自杀
        remove_stop_request()
        return {'success': True,
                'message': f'进程 {pid} 不存在或已被系统回收，已清理残留 PID 文件'}

    try:
        if sys.platform == 'win32':
            stopped = False
            # 优先走优雅停止：仅当目标登记时声明支持（graceful）且非 --force。
            # 显式 --pid 时不写停止请求：无法确认目标进程里跑着监视线程
            if not force and from_file and info.get('graceful'):
                if write_stop_request(pid):
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        if not is_process_alive(pid):
                            stopped = True
                            break
                        time.sleep(0.2)
                    if not stopped:
                        logger.warning('进程 %s 未在 %.0f 秒内优雅退出，降级为强制终止',
                                       pid, timeout)
            if not stopped:
                # /T 连同子进程树一起终止（runserver 的自动重载子进程等）
                try:
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                                   capture_output=True, check=True,
                                   creationflags=0x08000000)
                except subprocess.CalledProcessError:
                    # taskkill 报“找不到进程”（退出码 128）说明目标已消失，按已停止
                    # 处理；否则陈旧 PID 文件会永远卡在“停不掉又清不了”的状态。
                    # 仍存活却杀不掉（如权限不足）则照旧上报失败
                    if is_process_alive(pid):
                        raise
                    logger.warning('taskkill 报告进程 %s 不存在，按已停止处理', pid)
        else:
            if force:
                _kill_tree(pid, signal.SIGKILL)
            else:
                _kill_tree(pid, signal.SIGTERM)
                deadline = time.time() + timeout
                while time.time() < deadline and is_process_alive(pid):
                    time.sleep(0.2)
                if is_process_alive(pid):
                    logger.warning('进程 %s 未在 %.0f 秒内退出，升级为 SIGKILL', pid, timeout)
                    _kill_tree(pid, signal.SIGKILL)
    except Exception as e:
        logger.error('停止进程 %s 失败: %s', pid, e)
        return {'success': False, 'message': f'停止进程失败: {e}'}
    finally:
        # 无论成败都必须清掉停止请求：残留会让下一个启动的服务刚起来就自杀
        remove_stop_request()

    remove_pid_file()
    logger.info('服务进程 %s 已停止', pid)
    return {'success': True, 'message': f'服务进程 {pid} 已停止'}


def _kill_tree(pid: int, sig: int):
    """向进程所在进程组发送信号，失败时回退为仅终止进程本身。

    安全防护：若目标进程与当前进程在同一进程组，按进程组发信号会连自己一起杀，
    此时回退为仅终止目标进程。
    """
    try:
        pgid = os.getpgid(pid)
        if pgid == os.getpgrp():
            # 同进程组，不能按组杀，否则连自己一起死
            logger.debug('进程 %s 与当前进程同组，回退为单进程终止', pid)
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            return
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError) as e:
        logger.debug('按进程组终止 %s 失败(%s)，回退为单进程终止', pid, e)
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _close_db_for_shutdown():
    """os._exit 前关闭数据库连接，避免 SQLite WAL 残留或连接损坏。

    os._exit 跳过 atexit 与 Django 的正常关闭流程，数据库连接不会被自动清理；
    SQLite 虽有 crash recovery，但主动关闭更安全。
    """
    try:
        from django.db import connections
        for conn in connections.all():
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass
