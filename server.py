# server.py
import os
import sys
import time
import logging
from waitress.server import create_server
from django.core.wsgi import get_wsgi_application
from port_config import read_port

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# jieenv/ 目录须在 django.setup() 之前就位：单实例判重得在 setup 之前完成，
# 而 jiecore.server_process 在 jieenv/ 下（正常情况下由 config/constants.py 在
# 加载 settings 时插入同一路径，此处只是提前）
JIEENV_DIR = os.path.join(BASE_DIR, 'jieenv')
if JIEENV_DIR not in sys.path:
    sys.path.insert(0, JIEENV_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', "jieenv.config.production")
# 显式声明进程角色（见 jiecore/server_process.py）：不依赖按 argv[0] 推断，
# 本脚本被改名或包装启动时仍能正确登记 PID 并执行启动期自愈
os.environ.setdefault('JIEENV_ROLE', 'server')


def _preflight_single_instance():
    """启动前判重：已有登记在案且存活的服务时拒绝启动。

    必须在 django.setup() 之前调用：apps.ready() 在 setup 期间就会
    write_pid_file()，等 setup 之后再查 PID 文件，读到的已经是自己刚写进去的
    记录，判重形同虚设（实测：第二个 server.py 照样启动并把 PID 文件改成自己）。

    也不能只靠“端口绑定失败重试 10 次”硬撞：Windows 下 waitress 带
    SO_REUSEADDR，第二个进程会直接从第一个手里把端口抢走，连报错都不会有，
    旧进程则退化为既活着又收不到请求的幽灵。
    """
    if __name__ != '__main__':
        return          # 作为模块被导入（如 WSGI 部署）时不做判重
    # JIEENV_RESTARTING 由 restart_server 拉新进程时注入：旧进程此刻尚未退出、
    # PID 文件也刚被改写成新 PID，不跳过会被自己的单实例保护拦住
    if os.environ.get('JIEENV_RESTARTING'):
        return
    from jiecore.server_process import get_running_service
    existing = get_running_service()
    if not existing or existing.get('pid') == os.getpid():
        return
    msg = (f'已有服务在运行（PID {existing.get("pid")}，角色 '
           f'{existing.get("role")}，命令行 {existing.get("argv")}），'
           f'请先执行 python manage.py stopserver')
    # 此时 django.setup() 还没跑，日志配置尚未生效，logger 会走 lastResort
    # 处理器同样写到 stderr，两者只留一个，避免 run.bat 窗口里重复打印
    print(msg, file=sys.stderr)
    raise SystemExit(1)


_preflight_single_instance()

# 判重完成后清除重启标记，避免传染给后续 Popen 的子进程（#14）
os.environ.pop('JIEENV_RESTARTING', None)

import django
django.setup()

from django.conf import settings
logger = logging.getLogger(__name__)
logger.info('JieEnv 平台版本：%s', getattr(settings, 'PLATFORM_VERSION', 'unknown'))

application = get_wsgi_application()


# waitress 特定关闭逻辑：停 handler 线程 → 关 socket → 释放端口
def _close_waitress(srv):
    if srv is None:
        return
    try:
        srv.task_dispatcher.shutdown(cancel_pending=False, timeout=3)
    except Exception:
        pass
    try:
        srv.close()
    except Exception:
        pass


from jiecore.server_process import register_close_wsgi_handler
register_close_wsgi_handler(_close_waitress)

# 全局引用，供 restart_server 优雅关闭
_waitress_server = None

if __name__ == '__main__':
    PORT = read_port()   # 端口来自根目录 port.txt（refreshport.bat 生成），缺失则回退 8000
    logging.basicConfig()
    logger.info('服务监听端口：%s（来源 port.txt，缺失则默认 8000）', PORT)

    # listen=0.0.0.0:<PORT> 允许局域网访问
    # 使用 create_server 以便持有 server 引用，支持优雅关闭
    # 重启场景下端口可能尚未释放，自动重试绑定（判重已在 setup 前完成）
    from jiecore.server_process import register_wsgi_server
    _max_retries = 10
    for _attempt in range(_max_retries):
        try:
            _waitress_server = create_server(
                application,
                listen=f"0.0.0.0:{PORT}",
                threads=8,
                url_prefix=""
            )
            # 登记服务器对象，供 stopserver 优雅停止时先关 socket 释放端口
            register_wsgi_server(_waitress_server)
            _waitress_server.print_listen("Serving on http://{}:{}")
            _waitress_server.run()
            break
        except OSError as e:
            _waitress_server = None
            if _attempt < _max_retries - 1:
                logger.warning('端口绑定失败（%s），%d 秒后重试 (%d/%d)',
                               e, 2, _attempt + 1, _max_retries)
                time.sleep(2)
            else:
                logger.error('端口绑定失败，已重试 %d 次: %s', _max_retries, e)
                raise