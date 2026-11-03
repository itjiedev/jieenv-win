"""
应用安装命令
用法:
  python manage.py appinstall <zip_path>              # 从 zip 包安装
  python manage.py appinstall --path <python_path>     # 从 Python 路径安装（如 apps.home）
"""
from django.core.management.base import BaseCommand
from jiecore.app_manager import AppManager


class Command(BaseCommand):
    help = '安装扩展应用（支持 zip 包或 Python 路径）'

    def add_arguments(self, parser):
        parser.add_argument('source', nargs='?', type=str, help='应用 zip 包文件路径')
        parser.add_argument('--path', action='store_true',
                            help='以 Python 路径模式安装，source 为应用的点分路径（如 apps.home）')

    def handle(self, *args, **options):
        source = options.get('source')
        if not source:
            self.stdout.write(self.style.ERROR('请提供 zip 包路径或使用 --path 指定应用路径（如 apps.home）'))
            return

        is_path_mode = options['path']

        if is_path_mode:
            self.stdout.write(f'正在从 Python 路径安装应用: {source}')
            result = AppManager.install_from_path(source)
        else:
            self.stdout.write(f'正在从 zip 包安装应用: {source}')
            result = AppManager.install_from_zip(source)

        if result['success']:
            self.stdout.write(self.style.SUCCESS(result['message']))
        else:
            self.stdout.write(self.style.ERROR(result['message']))
