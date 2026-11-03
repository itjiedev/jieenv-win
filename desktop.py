# desktop.py
r"""
桌面壳模式：内置浏览器内核（Windows WebView2 / Edge Chromium）直接加载本项目 Web 页面。

- 壳-子进程拆分：桌面壳（本进程）只负责 pywebview 窗口，Django/waitress
  服务运行在独立子进程（server.py）中；
- 重启时壳杀子进程 → 拉新子进程 → 窗口 reload，窗口始终不关闭，
  新子进程完整冷启动（django.setup / AppConfig.ready），
  所有修改（模型变更、配置调整、应用启停等）全部生效；
- 若端口已有运行中的服务（run.bat / startserver 已启动），
  则直接开窗附着，不重复启动、关窗也不影响该服务；
- 本程序自行启动的子进程会在关闭窗口时一并终止；
- 首次运行若缺少 pywebview 会自动 pip 安装（状态写入 logs/desktop.log）。

用法:
    python\python.exe desktop.py
或直接双击 run_desktop.bat
"""
import os
import sys
import time
import socket
import logging
import threading
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
# jieenv/ 目录须提前加入 sys.path：port_config 等根目录模块依赖它，
# 子进程（server.py）启动时也会自行设置 DJANGO_SETTINGS_MODULE
JIEENV_DIR = os.path.join(BASE_DIR, 'jieenv')
if JIEENV_DIR not in sys.path:
    sys.path.insert(0, JIEENV_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'jieenv.config.dev')

from port_config import read_port   # 端口约定：根目录 port.txt（refreshport.bat 生成）

# ── 可调整参数 ─────────────────────────────────────────
# 平台版本号：从 apps/mainapp/app_meta.py 的 platform_version 字段读取
# （desktop.py 不经过 django.setup()，直接读文件）。
# mainapp 不存在时回退到 jiecore/app_meta.py 的 version。
_PLATFORM_VERSION = '0.0.0'
try:
    import importlib.util as _ilu
    _meta_file = os.path.join(BASE_DIR, 'jieenv', 'apps', 'mainapp', 'app_meta.py')
    if os.path.exists(_meta_file):
        _spec = _ilu.spec_from_file_location('_desktop_version_meta', _meta_file)
        if _spec and _spec.loader:
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _PLATFORM_VERSION = getattr(_mod, 'meta', {}).get('platform_version', '0.0.0')
except Exception:
    pass

if _PLATFORM_VERSION == '0.0.0':
    try:
        import importlib.util as _ilu
        _meta_file = os.path.join(BASE_DIR, 'jieenv', 'jiecore', 'app_meta.py')
        if os.path.exists(_meta_file):
            _spec = _ilu.spec_from_file_location('_desktop_version_fallback', _meta_file)
            if _spec and _spec.loader:
                _mod = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                _PLATFORM_VERSION = getattr(_mod, 'meta', {}).get('version', '0.0.0')
    except Exception:
        pass

WINDOW_TITLE = f'JieEnv项目环境管理 v{_PLATFORM_VERSION}'
HOST = '127.0.0.1'          # 窗口只连本机
LISTEN = '0.0.0.0'          # waitress 仍监听全部网卡，不影响局域网访问
PORT = read_port()          # 从 port.txt 读取，缺失则回退 8000
WINDOW_WIDTH = 1250
WINDOW_HEIGHT = 800
START_TIMEOUT = 60          # 等待 waitress 就绪的秒数

# 单实例锁：Windows 命名互斥体，进程启动即创建、退出即由系统释放，
# 可同时覆盖"窗口已打开"的运行态与"waitress 尚未监听端口"的启动中状态
MUTEX_NAME = 'Local\\JieenvDesktopShell_SingleInstance'

LOG_FILE = os.path.join(BASE_DIR, 'jieenv', 'logs', 'desktop.log')

# 持有单实例互斥体句柄，进程存续期间不得关闭（关闭即释放锁）
_SINGLE_INSTANCE_MUTEX = None


def _log(message: str):
    """简易日志：pythonw 无控制台，统一写 logs/desktop.log 便于诊断"""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{stamp}] {message}\n')
    except OSError:
        pass


def _acquire_single_instance_lock() -> bool:
    """获取单实例锁（Windows 命名互斥体）。

    互斥体在进程启动之初创建、进程退出时由系统自动释放，因此既能识别
    "窗口已打开"的运行态，也能识别"waitress 尚未监听端口"的启动中状态，
    避免用户多次双击导致多窗口或端口冲突。

    返回 True 表示本次成功持锁、可继续启动；False 表示已有实例在运行/启动中。
    """
    global _SINGLE_INSTANCE_MUTEX
    if sys.platform != 'win32':
        return True     # 非 Windows 桌面壳不做单实例限制
    try:
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        ERROR_ALREADY_EXISTS = 183
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        last_error = ctypes.get_last_error()
        if not handle:
            # 创建失败不阻塞启动，仅记录（避免误判为已有实例）
            _log(f'创建单实例互斥体失败(err={last_error})，忽略并继续启动')
            return True
        if last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _SINGLE_INSTANCE_MUTEX = handle   # 持有句柄，进程退出前勿关闭
        return True
    except Exception as e:
        _log(f'单实例锁检测异常（忽略，继续启动）: {e}')
        return True


def _warn_already_running():
    """已有实例在运行/启动中：提示用户，并尽力把已打开的窗口切到前台。"""
    _log('检测到项目已启动或正在启动，拒绝本次重复启动')
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.MessageBoxW(
            0,
            '系统已经在运行（或正在启动中），请勿重复启动。\n'
            '点击"确定"后将切换到已打开的窗口。',
            WINDOW_TITLE, 0x30)   # MB_ICONWARNING
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
    except Exception as e:
        _log(f'重复启动提示/激活窗口失败: {e}')


def _ensure_webview():
    """确保 pywebview 已安装，缺失时自动 pip 安装"""
    try:
        import webview  # noqa: F401
        return
    except ImportError:
        pass
    _log('pywebview 未安装，开始自动安装...')
    print('首次运行，正在安装 pywebview（请保持联网）...')
    proc = subprocess.run([sys.executable, '-m', 'pip', 'install',
                           '--disable-pip-version-check', 'pywebview'],
                          capture_output=True,
                          creationflags=0x08000000)
    if proc.returncode != 0:
        tail = ((proc.stderr or b'') + b'\n' + (proc.stdout or b'')).decode('utf-8', errors='replace')
        _log(f'pywebview 安装失败: {tail.strip()[-500:]}')
        raise SystemExit('pywebview 安装失败，请手动执行: '
                         'runtime\\python.exe -m pip install pywebview')
    import webview  # noqa: F401
    _log('pywebview 安装完成')


def _webview2_installed() -> bool:
    """检测系统是否安装 WebView2 Runtime（Edge Chromium 内核）：先查注册表，再找运行时文件"""
    # 1) EdgeUpdate 登记的常青版注册键（部分安装方式不写该键，故再补文件探测）
    try:
        import winreg
        guid = r'{F3017226-FE2A-4295-8BEF-535EB9BE9EC9}'
        roots = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
        views = (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY)
        for root in roots:
            for view in views:
                for sub in ('Clients', 'ClientState'):
                    path = rf'SOFTWARE\Microsoft\EdgeUpdate\{sub}\{guid}'
                    try:
                        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | view) as key:
                            value, _ = winreg.QueryValueEx(key, 'pv')
                            if str(value).strip() not in ('', '0.0.0.0'):
                                return True
                    except OSError:
                        continue
    except Exception:
        return True     # 检测失败不阻塞启动
    # 2) 直接探测运行时文件 msedgewebview2.exe
    bases = [os.environ.get('ProgramFiles(x86)', ''),
             os.environ.get('ProgramFiles', ''),
             os.environ.get('LOCALAPPDATA', '')]
    for base in bases:
        if not base:
            continue
        app_dir = os.path.join(base, 'Microsoft', 'EdgeWebView', 'Application')
        try:
            for name in os.listdir(app_dir):
                if os.path.isfile(os.path.join(app_dir, name, 'msedgewebview2.exe')):
                    return True
        except OSError:
            continue
    return False


def _warn_no_webview2():
    """缺少 WebView2 时弹窗提示（pywebview 会降级为 IE 内核，效果较差）"""
    _log('未检测到 WebView2 Runtime，窗口将降级为 IE 内核')
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            '未检测到 Microsoft Edge WebView2 运行时，窗口将以 IE 兼容模式显示，效果较差。\n'
            '建议下载安装"WebView2 Evergreen Bootstrapper"：\n'
            'https://developer.microsoft.com/microsoft-edge/webview2/',
            WINDOW_TITLE, 0x30)
    except Exception:
        pass


def _port_ready(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=1):
            return True
    except OSError:
        return False


def _wait_ready(port: int, timeout: int, proc: subprocess.Popen = None) -> bool:
    """等待服务就绪（HTTP 探测 /jiecore/api/ping/，而非仅探端口）。

    仅探端口（socket.create_connection）不够：waitress 绑定端口后仍需初始化 Django，
    此时 HTTP 请求可能返回 5xx。改用 HTTP ping 探测，与 startserver 命令一致。
    """
    import urllib.request
    import urllib.error
    ping_url = f'http://{HOST}:{port}/jiecore/api/ping/'
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(ping_url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError:
            # 有 HTTP 响应说明服务进程已就绪（即使返回非 200）
            return True
        except OSError:
            pass
        # 子进程已退出（如缓存缺失导致立即 sys.exit）→ 不再等待
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.3)
    return False


def _wait_port_freed(port: int, timeout: float = 30.0) -> bool:
    """等待端口释放（前置进程退出/socket 回收需要时间）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_ready(port):
            return True
        time.sleep(0.5)
    return False


class _ServerSubprocess:
    """壳-子进程拆分：管理 server.py 子进程的生命周期。

    壳进程（desktop.py）只负责 pywebview 窗口；Django/waitress 服务运行在
    独立子进程中，重启时写重启文件 → 子进程退出 → 壳拉新子进程 → 窗口 reload，
    窗口始终不关闭，新子进程完整冷启动确保所有修改生效。
    """

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self.stderr_file = None       # 子进程 stderr 捕获文件路径（公开属性）
        self._stderr_fh = None        # stderr 文件句柄（需正确关闭）
        # 停止/重启请求文件路径（与 server_process.py 保持一致，但不导入 Django）
        self._stop_request_file = os.path.join(BASE_DIR, 'jieenv', 'tmp', 'server.stop')
        self._restart_request_file = os.path.join(BASE_DIR, 'jieenv', 'tmp', 'server.restart')

    def start(self, restarting: bool = False) -> subprocess.Popen:
        """启动 server.py 子进程。restarting=True 时注入 JIEENV_RESTARTING
        环境变量，跳过子进程的单实例判重（重启场景旧 PID 文件可能残留）"""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return self._proc
            # 关闭上次的 stderr 句柄，避免文件描述符泄漏（#19）
            if self._stderr_fh is not None:
                try:
                    self._stderr_fh.close()
                except OSError:
                    pass
                self._stderr_fh = None
            executable = getattr(sys, '_base_executable', None) or sys.executable
            server_script = os.path.join(BASE_DIR, 'server.py')
            creationflags = 0
            if sys.platform == 'win32':
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            env = os.environ.copy()
            # 标记子进程模式：服务进程据此将重启委托壳处理（#1）
            env['JIEENV_SUBPROCESS_MODE'] = '1'
            if restarting:
                env['JIEENV_RESTARTING'] = '1'
            else:
                # 非重启模式清除残留标记，避免传染给新子进程（#14）
                env.pop('JIEENV_RESTARTING', None)
            # 强制子进程 stdout/stderr 使用 UTF-8 编码，
            # 避免 Windows 中文系统默认 GBK 导致捕获的 stderr 中文乱码
            env['PYTHONIOENCODING'] = 'utf-8'
            # stderr 捕获到临时文件：启动失败时读取展示给用户，
            # 避免 bootstrap.py 等启动期错误被 DEVNULL 吞掉而无法诊断
            self.stderr_file = os.path.join(
                BASE_DIR, 'jieenv', 'tmp', 'server_stderr.log')
            try:
                os.makedirs(os.path.dirname(self.stderr_file), exist_ok=True)
                self._stderr_fh = open(self.stderr_file, 'w', encoding='utf-8')
                stderr_fh = self._stderr_fh
            except OSError:
                stderr_fh = subprocess.DEVNULL
                self._stderr_fh = None
            self._proc = subprocess.Popen(
                [executable, server_script],
                cwd=BASE_DIR,
                env=env,
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fh,
                stdin=subprocess.DEVNULL,
            )
            _log(f'已启动服务子进程 PID={self._proc.pid}'
                 + ('（重启模式，跳过单实例判重）' if restarting else ''))
            return self._proc

    def stop(self, timeout: float = 10.0):
        """终止子进程：先尝试优雅停止（写停止请求文件），超时后降级为强杀（#5）"""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            if sys.platform == 'win32':
                # 先尝试优雅停止：写停止请求文件，让子进程内的监视线程执行
                # WSGI 关闭 + DB 关闭 + 日志 flush，避免 taskkill /F 跳过这些步骤
                try:
                    os.makedirs(os.path.dirname(self._stop_request_file), exist_ok=True)
                    with open(self._stop_request_file, 'w', encoding='utf-8') as f:
                        f.write(str(proc.pid))
                except OSError:
                    pass
                # 等待子进程自行退出
                try:
                    proc.wait(timeout=timeout)
                    _log(f'服务子进程 PID={proc.pid} 已优雅退出')
                    return
                except subprocess.TimeoutExpired:
                    _log(f'子进程 {proc.pid} 未在 {timeout}s 内优雅退出，降级为强制终止')
                # 超时后强制终止：taskkill /T 杀整棵进程树
                try:
                    subprocess.run(
                        ['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                        capture_output=True, timeout=10)
                    proc.wait(timeout=5)
                except Exception:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception as e:
            _log(f'终止子进程失败: {e}')
        else:
            _log(f'服务子进程 PID={proc.pid} 已退出')

    def terminate(self):
        """关窗时快速终止子进程（供 _on_closing 调用，不阻塞 UI 线程）。

        与 stop() 不同，此方法不等待子进程退出，避免阻塞 pywebview UI 线程。
        使用 taskkill /T /F 确保整棵进程树被终止。
        """
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None or proc.poll() is not None:
            return
        if sys.platform == 'win32':
            try:
                subprocess.Popen(
                    ['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
            except Exception as e:
                _log(f'发起 taskkill 失败: {e}')
                proc.terminate()
        else:
            proc.terminate()

    @property
    def process(self):
        return self._proc


def _terminate_subprocess(sub):
    """终止子进程（供 _on_closing 等调用）"""
    if sub is not None:
        sub.stop()


def _set_window_icon_async(title, icon_path):
    """在后台线程等待窗口创建，然后通过 Windows API 设置标题栏图标"""
    if sys.platform != 'win32' or not os.path.exists(icon_path):
        return

    def _worker():
        import ctypes
        user32 = ctypes.windll.user32
        # 等待窗口出现（最多 5 秒）
        hwnd = 0
        for _ in range(50):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            _log(f'未找到标题为"{title}"的窗口，无法设置图标')
            return
        # 加载 ico 文件并设置为窗口图标
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040
        WM_SETICON = 0x0080
        hicon = user32.LoadImageW(0, icon_path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if hicon:
            user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)  # ICON_BIG
            user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)  # ICON_SMALL
            _log(f'已设置窗口图标: {icon_path}')
        else:
            _log(f'图标加载失败: {icon_path}')

    threading.Thread(target=_worker, daemon=True).start()


def _read_server_stderr(stderr_file: str, tail_bytes: int = 8192) -> str:
    """读取子进程 stderr 捕获文件的内容，用于启动失败时展示给用户"""
    if not stderr_file or not os.path.exists(stderr_file):
        return ''
    try:
        with open(stderr_file, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(0, 2)
            size = f.tell()
            if size <= 0:
                return ''
            f.seek(max(0, size - tail_bytes))
            return f.read().strip()
    except OSError:
        return ''


def main():
    # 单实例检测：项目已启动或正在启动时提示并退出，避免重复启动造成多窗口/端口冲突
    if not _acquire_single_instance_lock():
        _warn_already_running()
        return

    _ensure_webview()
    import webview

    webview.settings['ALLOW_DOWNLOADS'] = True

    # 判重只需端口探测：壳不再 django.setup()，不登记 PID，
    # 端口被占即说明有服务在跑，直接附着。壳只做窗口 + 子进程管理。
    attach = _port_ready(PORT)

    logging.getLogger('desktop').info('桌面壳启动')

    if not _webview2_installed():
        _warn_no_webview2()

    server_sub = None
    if attach:
        _log(f'端口 {PORT} 已被占用，附着到已有服务')
        if not _wait_ready(PORT, START_TIMEOUT):
            _log(f'附着目标在 {START_TIMEOUT} 秒内未就绪，退出')
            raise SystemExit(f'端口 {PORT} 上的服务在 {START_TIMEOUT} 秒内未就绪，'
                             f'请查看 logs/ 目录日志')
    else:
        # 壳-子进程拆分：启动 server.py 子进程作为 Django/waitress 服务
        server_sub = _ServerSubprocess()
        server_sub.start()
        if not _wait_ready(PORT, START_TIMEOUT, proc=server_sub.process):
            _log(f'服务子进程在 {START_TIMEOUT} 秒内未就绪，退出')
            # 读取子进程 stderr 捕获的实际错误，展示给用户
            stderr_output = _read_server_stderr(server_sub.stderr_file)
            server_sub.stop()
            if stderr_output:
                _log(f'子进程 stderr 输出:\n{stderr_output}')
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        0,
                        f'Web 服务启动失败：\n\n{stderr_output}',
                        WINDOW_TITLE, 0x10)   # MB_ICONERROR
                except Exception:
                    pass
                raise SystemExit(
                    f'Web 服务启动失败，子进程错误输出：\n{stderr_output}')
            raise SystemExit(f'Web 服务在 {START_TIMEOUT} 秒内未就绪，请查看 logs/ 目录日志')

    # 自启子进程模式启动重启协调器：壳监视子进程退出 + 重启文件 → 拉新子进程 + 窗口 reload
    # 附着模式下不启动（重启请求由外部服务进程自身处理）
    if server_sub is not None:
        _watch_subprocess_for_restart(server_sub)

    window = webview.create_window(
        WINDOW_TITLE,
        f'http://{HOST}:{PORT}/',
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        x=0, y=0,
        text_select=True,
    )
    _set_window_icon_async(WINDOW_TITLE, os.path.join(BASE_DIR, 'favicon.ico'))

    def _on_closing():
        # 使用 terminate() 方法快速终止子进程，不阻塞 pywebview UI 线程（#16）
        if server_sub is not None:
            _log('窗口关闭，终止服务子进程')
            server_sub.terminate()

    window.events.closing += _on_closing
    webview.start()
    _log('桌面壳已退出')


def _watch_subprocess_for_restart(server_sub):
    """壳侧子进程重启协调器：监视子进程退出，检测到重启请求后拉新子进程 + 窗口 reload。

    新架构（修复跨进程 handler 不生效问题 #1-#4）：
    - 服务子进程检测到 JIEENV_SUBPROCESS_MODE 后，restart_server() 写重启请求文件
      （tmp/server.restart）后自行退出；
    - 本函数检测到子进程退出 + 重启文件存在 → 拉新子进程 → 窗口 load_url() reload；
    - 子进程异常崩溃（无重启文件）时不自动重启，避免无限循环。
    """
    restart_file = os.path.join(BASE_DIR, 'jieenv', 'tmp', 'server.restart')

    def _watcher():
        while True:
            if server_sub.process is None:
                break   # 壳主动关闭了子进程（如关窗），退出监视
            if server_sub.process.poll() is None:
                time.sleep(0.5)
                continue
            # 子进程已退出，检查是否有重启请求
            if not os.path.exists(restart_file):
                _log('子进程异常退出（无重启请求文件），不自动重启')
                break
            _log('检测到子进程重启请求，开始重启子进程')
            # 清理重启请求文件
            try:
                os.remove(restart_file)
            except OSError:
                pass
            # 清理残留的停止请求文件
            stop_file = os.path.join(BASE_DIR, 'jieenv', 'tmp', 'server.stop')
            try:
                if os.path.exists(stop_file):
                    os.remove(stop_file)
            except OSError:
                pass
            # 等待端口释放
            if not _wait_port_freed(PORT, timeout=15):
                _log(f'端口 {PORT} 迟迟未释放，子进程重启失败')
                break
            # 启动新子进程
            try:
                server_sub.start(restarting=False)
            except Exception as e:
                _log(f'拉起新子进程失败: {e}')
                break
            # 后台等待新子进程就绪后触发窗口 reload
            def _reload_when_ready():
                if _wait_ready(PORT, START_TIMEOUT, proc=server_sub.process):
                    _log('新子进程已就绪，触发窗口 reload')
                    # 读取重启前保存的页面 URL，导航回原页面而非首页
                    restart_url_file = os.path.join(
                        BASE_DIR, 'jieenv', 'tmp', 'restart_url.txt')
                    target_url = f'http://{HOST}:{PORT}/'
                    try:
                        if os.path.exists(restart_url_file):
                            with open(restart_url_file, 'r', encoding='utf-8') as f:
                                saved_url = f.read().strip()
                            os.remove(restart_url_file)
                            if saved_url and saved_url.startswith('http'):
                                target_url = saved_url
                                _log(f'导航回重启前页面: {saved_url}')
                    except OSError:
                        pass
                    try:
                        import webview
                        for w in webview.windows:
                            # 使用 load_url() 而非 w.location（#4）：
                            # pywebview 中 w.location 赋值不触发导航，
                            # 必须调用 load_url() 方法
                            w.load_url(target_url)
                    except Exception as e:
                        _log(f'窗口 reload 失败(可忽略): {e}')
                else:
                    _log('新子进程启动超时')
            threading.Thread(target=_reload_when_ready, daemon=True).start()
    threading.Thread(target=_watcher, daemon=True, name='jie-subprocess-watcher').start()


if __name__ == '__main__':
    main()