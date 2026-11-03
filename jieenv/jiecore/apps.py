import sys
import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class JiecoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'jiecore'
    verbose_name = '核心框架'

    def ready(self):
        # 启动时登记当前进程 PID 到 tmp/server.pid，供 stopserver 优雅停止
        # 仅服务进程登记（排除 migrate/startserver 等一次性命令）；
        # runserver 自动重载场景下跳过子进程（工作进程），登记父进程（监视进程），
        # 停止时连同子进程一并终止
        try:
            from .server_process import (write_pid_file, is_server_process,
                                         is_autoreload_child, start_stop_watcher)
            if is_server_process() and not is_autoreload_child():
                write_pid_file()
                # 监视停止请求，使 Windows 下也能优雅退出（先关 WSGI 服务器释放端口、
                # flush 日志，而不是被 taskkill /F 直接掐断）。runserver 的 PID 登记在
                # 父（监视）进程上，请求通知不到真正处理请求的子进程，故不启动
                if 'runserver' not in sys.argv:
                    start_stop_watcher()
        except Exception as e:
            logger.warning('登记服务 PID 失败(可忽略): %s', e)
        # 启动时将已启用应用的模板目录加入 TEMPLATES DIRS（不依赖引擎初始化时机）
        try:
            from .hot_reload import sync_template_dirs
            sync_template_dirs()
        except Exception as e:
            logger.warning('启动模板目录同步失败(可忽略): %s', e)
