"""
后台启动服务命令
用法: python manage.py startserver [addr] [--no-wait]
  以独立进程方式后台启动 runserver，并将 PID 记录到 tmp/server.pid，
  之后可通过 python manage.py stopserver 优雅停止。
"""
import sys
import time
import subprocess
import urllib.request
import urllib.error

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from jiecore.server_process import (
    write_pid_file, get_running_service, PID_FILE, ROLE_SERVER,
)

PORT_FILE_PATH = settings.ENV_ROOT / 'port.txt'
PORT = 8000
if PORT_FILE_PATH.exists():
    # port_config.py 位于项目根目录（ENV_ROOT），而 manage.py 的 sys.path 仅含 BASE_DIR，
    # 故显式将 ENV_ROOT 加入后再导入，复用与 server.py/desktop.py 一致的端口读取逻辑
    _ENV_ROOT_STR = str(settings.ENV_ROOT)
    if _ENV_ROOT_STR not in sys.path:
        sys.path.insert(0, _ENV_ROOT_STR)

    from port_config import read_port

    PORT = read_port()

# 启动探测：轮询 /jiecore/api/ping/ 确认服务就绪
PING_URL_FMT = 'http://{addr}/jiecore/api/ping/'
PING_TIMEOUT = 30.0
PING_INTERVAL = 0.5

# 后台进程的 stdout/stderr 写入日志文件，便于诊断
SERVER_LOG_FILE = settings.LOG_DIR / 'server_stdout.log'

# Windows 创建标志
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

class Command(BaseCommand):
    help = '后台启动服务并记录 PID 文件（配合 stopserver 使用）'

    def add_arguments(self, parser):
        parser.add_argument('addr', nargs='?', default=f'127.0.0.1:{PORT}',
                            help='监听地址 host:{PORT}, 默认端口来自根目录 port.txt, 缺失则回退 8000')
        parser.add_argument('--no-wait', action='store_true',
                            help='启动后不探测服务是否就绪')

    def handle(self, *args, **options):
        # 单一真值源判重（含存活与 start_time 校验），与 server.py / desktop.py 共用
        running = get_running_service()
        if running:
            raise CommandError(
                f'服务已在运行（PID {running.get("pid")}，角色 '
                f'{running.get("role")}），请先执行 python manage.py stopserver')

        addr = options['addr']
        # Windows venv 下 sys.executable 是启动器桩进程，分离启动后会随父进程退出
        # 被连带终止；优先使用真实解释器（sys._base_executable）避免此问题
        executable = getattr(sys, '_base_executable', None) or sys.executable
        cmd = [executable, str(settings.BASE_DIR / 'manage.py'), 'runserver', addr]
        # 后台进程输出写入日志文件（保留句柄供子进程继承，勿在函数内关闭）
        self._log_handle = open(SERVER_LOG_FILE, 'ab', buffering=0)
        kwargs = {'stdin': subprocess.DEVNULL,
                  'stdout': self._log_handle,
                  'stderr': subprocess.STDOUT}
        if sys.platform == 'win32':
            # DETACHED_PROCESS：脱离控制台，关闭当前终端不影响服务
            kwargs['creationflags'] = DETACHED_PROCESS
            proc = self._spawn_detached(cmd, kwargs)
        else:
            # 新会话/进程组，便于 stopserver 按进程组优雅停止
            kwargs['start_new_session'] = True
            proc = subprocess.Popen(cmd, **kwargs)
        # 登记的是派生出的 runserver 服务进程；本命令自身角色为 cli，
        # 故必须显式传 ROLE_SERVER，否则 PID 文件里的 role 会记错；
        # graceful=False 因为派生的是 runserver，停止请求通知不到真正处理请求的
        # 重载子进程（缺省值按本命令的 sys.argv 推断会得出错误结果）
        write_pid_file(proc.pid, role=ROLE_SERVER, graceful=False)
        self.stdout.write(f'服务已在后台启动，PID {proc.pid}，地址 http://{addr}')
        self.stdout.write(f'PID 文件: {PID_FILE}')
        self.stdout.write(f'服务输出日志: {SERVER_LOG_FILE}')

        if not options['no_wait']:
            if self.wait_ping(addr):
                self.stdout.write(self.style.SUCCESS('服务启动探测成功（ping 已就绪）'))
            else:
                self.stdout.write(self.style.WARNING(
                    f'启动探测超时（{PING_TIMEOUT:.0f}秒），请自行确认服务状态'))

    @staticmethod
    def _spawn_detached(cmd, kwargs):
        """
        Windows 下分离启动服务进程。
        优先附加 CREATE_BREAKAWAY_FROM_JOB 脱离父进程所在的作业对象（Job Object），
        避免父进程退出后子进程被作业对象连带终止；
        若作业不允许脱离（ERROR_ACCESS_DENIED）则回退为普通分离启动。
        """
        try:
            return subprocess.Popen(
                cmd, creationflags=kwargs['creationflags'] | CREATE_BREAKAWAY_FROM_JOB,
                stdin=kwargs['stdin'], stdout=kwargs['stdout'], stderr=kwargs['stderr'])
        except OSError:
            return subprocess.Popen(cmd, **kwargs)

    @staticmethod
    def wait_ping(addr: str) -> bool:
        """轮询 ping 接口确认服务已就绪；HTTP 错误码也视为服务已启动"""
        deadline = time.time() + PING_TIMEOUT
        url = PING_URL_FMT.format(addr=addr)
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except urllib.error.HTTPError:
                # 有 HTTP 响应说明服务进程已就绪
                return True
            except OSError:
                pass
            time.sleep(PING_INTERVAL)
        return False
