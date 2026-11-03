"""
应用卸载/删除命令
用法: python manage.py appremove <app_name> [--purge]
  默认卸载（保留文件），--purge 彻底删除
"""
from django.core.management.base import BaseCommand
from jiecore.app_manager import AppManager


class Command(BaseCommand):
    help = '卸载或彻底删除扩展应用'

    def add_arguments(self, parser):
        parser.add_argument('app_name', type=str, help='应用包名')
        parser.add_argument('--purge', action='store_true', help='彻底删除应用（回滚迁移、清理数据、删除文件）')

    def handle(self, *args, **options):
        app_name = options['app_name']
        purge = options['purge']

        if purge:
            self.stdout.write(f'正在彻底删除应用: {app_name}（含迁移回滚和数据清理）')
            result = AppManager.delete_app(app_name)
        else:
            self.stdout.write(f'正在卸载应用: {app_name}')
            result = AppManager.uninstall_app(app_name)

        if result['success']:
            self.stdout.write(self.style.SUCCESS(result['message']))
        else:
            self.stdout.write(self.style.ERROR(result['message']))
