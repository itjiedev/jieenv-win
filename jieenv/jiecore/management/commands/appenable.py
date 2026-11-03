"""
应用启用/禁用命令
用法: python manage.py appenable <app_name> [--disable]
"""
from django.core.management.base import BaseCommand
from jiecore.app_manager import AppManager


class Command(BaseCommand):
    help = '启用或禁用扩展应用'

    def add_arguments(self, parser):
        parser.add_argument('app_name', type=str, help='应用包名')
        parser.add_argument('--disable', action='store_true', help='禁用应用（默认为启用）')

    def handle(self, *args, **options):
        app_name = options['app_name']
        disable = options['disable']

        if disable:
            self.stdout.write(f'正在禁用应用: {app_name}')
            result = AppManager.disable_app(app_name)
        else:
            self.stdout.write(f'正在启用应用: {app_name}')
            result = AppManager.enable_app(app_name)

        if result['success']:
            self.stdout.write(self.style.SUCCESS(result['message']))
        else:
            self.stdout.write(self.style.ERROR(result['message']))
