"""
应用信息重扫命令
用法: python manage.py apprescan <app_name> [<app_name> ...]
  或: python manage.py apprescan --all
重新读取磁盘 app_meta.py 刷新已注册应用的信息，并按需自动安装缺失的
pip 第三方包、自动执行数据库迁移。
"""
from django.core.management.base import BaseCommand, CommandError
from jiecore.app_manager import AppManager, scan_disk_apps


class Command(BaseCommand):
    help = ('重新扫描磁盘 app_meta 刷新指定应用的注册信息'
            '（含 pip 依赖自动安装与数据库自动迁移）')

    def add_arguments(self, parser):
        parser.add_argument('app_name', nargs='*', type=str,
                            help='应用包名，可一次指定多个')
        parser.add_argument('--all', action='store_true',
                            help='重扫 apps/ 下全部应用（不含框架内核 jiecore）')
        parser.add_argument('--dry-run', action='store_true',
                            help='只报告将发生的变更，不安装、不迁移、不写库')
        parser.add_argument('--no-deps', action='store_true',
                            help='跳过 pip 第三方包检测与安装')
        parser.add_argument('--no-migrate', action='store_true',
                            help='跳过数据库迁移')
        parser.add_argument('--makemigrations', action='store_true',
                            help='迁移前先生成迁移文件（默认遵循"应用自带迁移文件"'
                                 '约定，不生成；仅开发环境手工改模型后使用）')

    def handle(self, *args, **options):
        names = list(options['app_name'])
        if options['all']:
            names = sorted(set(names) | set(scan_disk_apps()))
        # 去重并保持用户给定的顺序
        seen, targets = set(), []
        for name in names:
            if name not in seen:
                seen.add(name)
                targets.append(name)
        if not targets:
            self.stdout.write(self.style.ERROR('请指定应用名，或使用 --all'))
            return

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('预演模式：不会安装依赖、不会迁移、不会写库'))

        failed = []
        for name in targets:
            self.stdout.write(f'── 重扫应用: {name}')
            result = AppManager.rescan_app(
                name,
                install_deps=not options['no_deps'],
                do_migrate=not options['no_migrate'],
                makemigrations=options['makemigrations'],
                dry_run=options['dry_run'],
            )
            for warn in result.get('warnings', []):
                self.stdout.write(self.style.WARNING(f'   警告: {warn}'))
            for field, (old, new) in result.get('changes', {}).items():
                self.stdout.write(f'   {field}: {self._short(old)} -> {self._short(new)}')
            if result['success']:
                self.stdout.write(self.style.SUCCESS(f"   {result['message']}"))
            else:
                self.stdout.write(self.style.ERROR(f"   {result['message']}"))
                failed.append(name)

        if failed:
            raise CommandError(f'以下应用重扫失败: {", ".join(failed)}')
        self.stdout.write(self.style.SUCCESS(f'完成，共处理 {len(targets)} 个应用'))

    @staticmethod
    def _short(value, limit: int = 60) -> str:
        """变更值可能是长文本（如 changelog）或 None，统一截断为单行便于阅读"""
        if value is None:
            return '(无)'
        if isinstance(value, bool):
            return '是' if value else '否'
        text = ' '.join(str(value).split())
        return text if len(text) <= limit else text[:limit] + '…'
