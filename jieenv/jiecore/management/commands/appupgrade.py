"""
应用升级命令
用法: python manage.py appupgrade <app_name> <zip_path>
  或: python manage.py appupgrade --scan  (扫描可用升级包)
"""
from django.core.management.base import BaseCommand
from jiecore.app_manager import AppManager
from jiecore.version_manager import upgrade_from_zip, scan_upgrade_packages


class Command(BaseCommand):
    help = '升级扩展应用或扫描可用升级包'

    def add_arguments(self, parser):
        parser.add_argument('app_name', nargs='?', type=str, help='应用包名')
        parser.add_argument('zip_path', nargs='?', type=str, help='升级 zip 包路径')
        parser.add_argument('--scan', action='store_true', help='扫描升级目录中的可用升级包')

    def handle(self, *args, **options):
        if options['scan']:
            self._scan()
            return

        app_name = options.get('app_name')
        zip_path = options.get('zip_path')
        if not app_name or not zip_path:
            self.stdout.write(self.style.ERROR('请提供应用名和 zip 包路径，或使用 --scan'))
            return

        self.stdout.write(f'正在升级应用 {app_name}...')
        result = upgrade_from_zip(app_name, zip_path)
        if result['success']:
            self.stdout.write(self.style.SUCCESS(result['message']))
        else:
            self.stdout.write(self.style.ERROR(result['message']))

    def _scan(self):
        packages = scan_upgrade_packages()
        if not packages:
            self.stdout.write('暂无可用升级包')
            return
        self.stdout.write(self.style.SUCCESS(f'发现 {len(packages)} 个升级包:'))
        for pkg in packages:
            status = '可升级' if pkg['can_upgrade'] else '已是最新'
            self.stdout.write(f"  {pkg['filename']}  ({pkg['app_name']} v{pkg['version']}) [{status}]")
