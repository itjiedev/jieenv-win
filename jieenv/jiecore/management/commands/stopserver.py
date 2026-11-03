"""
停止服务命令
用法: python manage.py stopserver [--pid PID] [--force] [--timeout SECONDS]
  读取 tmp/server.pid 终止服务进程树；
  两个平台都是先优雅退出（Windows 写停止请求文件让服务自行关闭 waitress
  并 flush 日志，Linux 发 SIGTERM），超时后强制终止
  （Windows taskkill /F /T，Linux SIGKILL）。
"""
from django.core.management.base import BaseCommand
from jiecore.server_process import stop_server


class Command(BaseCommand):
    help = '根据 PID 文件停止服务进程（跨平台）'

    def add_arguments(self, parser):
        parser.add_argument('--pid', type=int, default=None,
                            help='指定要停止的进程 PID（默认读取 PID 文件）')
        parser.add_argument('--force', action='store_true',
                            help='强制终止，跳过优雅退出（Windows 直接 taskkill /F /T，'
                                 'Linux 直接 SIGKILL）')
        parser.add_argument('--timeout', type=float, default=10.0,
                            help='优雅退出的等待秒数，超时后强制终止（默认 10）')

    def handle(self, *args, **options):
        result = stop_server(pid=options['pid'], force=options['force'],
                             timeout=options['timeout'])
        if result['success']:
            self.stdout.write(self.style.SUCCESS(result['message']))
        else:
            self.stderr.write(self.style.ERROR(result['message']))
