"""
全量依赖检测命令
用法: python manage.py checkdeps
  检查所有已安装应用的依赖是否满足（应用间依赖 + jieenv_platform 平台版本约束）。
  用于平台升级后发现跨版本不兼容。
"""
from django.core.management.base import BaseCommand
from jiecore.app_manager import AppManager


class Command(BaseCommand):
    help = '检查所有已安装应用的依赖是否满足'

    def handle(self, *args, **options):
        result = AppManager.check_all_dependencies()
        if not result:
            self.stdout.write(self.style.SUCCESS('全部依赖检测通过'))
            return

        self.stdout.write(self.style.WARNING(
            f'发现 {len(result)} 个应用存在依赖问题：'
        ))
        for app_name, missing in result.items():
            for dep_name, specifier in missing:
                self.stdout.write(self.style.WARNING(
                    f'  {app_name}: {dep_name} {specifier}'
                ))