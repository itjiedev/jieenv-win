"""
刷新缓存命令
用法: python manage.py refreshcache [--sync]
  刷新 JSON 缓存文件，--sync 同时从磁盘同步应用状态
"""
from django.core.management.base import BaseCommand
from jiecore.cache_handler import refresh_cache_from_db
from jiecore.menu_aggregator import refresh_menu_cache
from jiecore.app_manager import AppManager


class Command(BaseCommand):
    help = '刷新应用注册和菜单的 JSON 缓存'

    def add_arguments(self, parser):
        parser.add_argument('--sync', action='store_true', help='同时从磁盘同步应用状态')

    def handle(self, *args, **options):
        if options['sync']:
            self.stdout.write('正在从磁盘同步应用状态...')
            AppManager.sync_from_disk()
            self.stdout.write(self.style.SUCCESS('磁盘同步完成'))

        self.stdout.write('正在从数据库刷新缓存...')
        try:
            refresh_cache_from_db()
            self.stdout.write(self.style.SUCCESS('数据库缓存已刷新'))
        except Exception as e:
            self.stdout.write(self.style.WARNING(
                f'数据库刷新失败（表可能不存在）: {e}\n'
                '请先执行 python manage.py migrate 初始化数据库'
            ))
            return

        self.stdout.write('正在刷新菜单缓存...')
        refresh_menu_cache()
        self.stdout.write(self.style.SUCCESS('菜单缓存已刷新'))
        self.stdout.write(self.style.SUCCESS('全部缓存刷新完成'))
