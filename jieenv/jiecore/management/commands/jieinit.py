"""
项目初始化命令
用法: python manage.py jieinit [--skip-config]

一键初始化项目基础结构：
  1. 从 jiecore 内置模板复制 config/ 目录（替换 SECRET_KEY）  ← --skip-config 跳过
  2. 改写项目根目录 manage.py 中的 DJANGO_SETTINGS_MODULE 引用 ← --skip-config 跳过
  3. 创建项目运行所需目录（apps/ data/ logs/ tmp/ .cache/ 等）
  4. 写入空注册表缓存（确保 bootstrap.py 启动时不会报错退出）
  5. 在独立子进程中执行数据库迁移（让新配置彻底生效）

注意：jiecore 必须已在 INSTALLED_APPS 中才能使用本命令。
本命令不扫描应用注册表，也不刷新缓存——这些由后续运维操作完成。

--skip-config：跳过步骤 1、2（覆盖 config/ 和改写 manage.py），
               适用于 config/ 已存在且无需重置的场景（如 install.bat 首次初始化）。

若项目已格式化（config/ 已存在），需用户输入 'jiecore' 确认后才执行。
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.crypto import get_random_string

# 模板目录：与 jieinit.py 同级的 config_template/
_TEMPLATE_DIR = Path(__file__).resolve().parent / 'config_template'

# 初始化完成后，项目统一使用的配置入口模块
_NEW_SETTINGS_MODULE = 'config.dev'
# 模板中生成 SECRET_KEY 的文件
_NEW_SECURITY_FILE = 'security.py'


# ── 命令实现 ─────────────────────────────────────────────────


class Command(BaseCommand):
    help = '一键初始化项目结构（从内置模板复制 config/、改写 manage.py、创建运行目录、写入空缓存、子进程迁移数据库）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-config',
            action='store_true',
            help='跳过覆盖 config/ 和改写 manage.py（适用于 config 已存在的场景）',
        )

    def handle(self, *args, **options):
        skip_config = options['skip_config']

        self.stdout.write(self.style.NOTICE('=== jiecore 项目初始化 ==='))
        if skip_config:
            self.stdout.write(self.style.WARNING('  （已跳过 config 覆盖步骤）'))
        self.stdout.write('')

        # 0. 检查是否已格式化，已格式化则要求确认（--skip-config 时跳过确认）
        if not skip_config and self._is_already_formatted():
            if not self._confirm_reformat():
                self.stdout.write(self.style.WARNING('已取消操作'))
                return

        # 1. 从内置模板复制 config/ 目录（替换 SECRET_KEY）
        # 2. 改写项目根目录 manage.py 中的配置入口引用
        if not skip_config:
            self._generate_config()
            self._rewrite_manage_py()
        else:
            self.stdout.write(self.style.NOTICE('跳过 config/ 生成和 manage.py 改写'))

        # 3. 创建项目运行所需目录
        self._ensure_directories()

        # 4. 写入空注册表缓存（bootstrap.py 启动时要求缓存文件存在）
        self._write_empty_cache()

        # 5. 在独立子进程中执行数据库迁移
        #    必须新进程，否则 INSTALLED_APPS / USING_AUTH 之类的开关
        #    在 django.setup() 阶段已被消费，改内存设置无法回溯生效。
        self._run_migrations()

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== 初始化完成 ==='))

    # ── 内部步骤 ─────────────────────────────────────────

    def _is_already_formatted(self) -> bool:
        """检查项目是否已被格式化（config/base.py 存在即视为已格式化）"""
        config_dir = Path(settings.BASE_DIR) / 'config'
        return (config_dir / 'base.py').is_file()

    def _confirm_reformat(self) -> bool:
        """已格式化时要求用户输入 'jiecore' 确认"""
        self.stdout.write(self.style.WARNING(
            '⚠ 检测到当前项目配置目录名称已经存在（config/ 目录已存在）。'
        ))
        self.stdout.write('')
        self.stdout.write('重新格式化将覆盖 config/ 目录下的所有文件，包括：')
        self.stdout.write('  - security.py（SECRET_KEY 将被重新生成）')
        self.stdout.write('  - constants.py、bootstrap.py、base.py 等')
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            '如果确认要重新格式化，请输入 \'jiecore\'（完整输入以确认）：'
        ))
        try:
            user_input = input('> ').strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if user_input == 'jiecore':
            self.stdout.write(self.style.SUCCESS('确认通过，开始重新格式化...'))
            return True
        self.stdout.write(self.style.ERROR(
            f'输入不匹配（收到: \'{user_input}\'），操作已取消。'
        ))
        return False

    def _generate_config(self):
        """从 jiecore 内置模板复制 config/ 目录，替换 SECRET_KEY 占位符"""
        self.stdout.write('正在从内置模板生成 config/ 目录...')
        base = Path(settings.BASE_DIR)
        config_dir = base / 'config'
        old_config_folder_name = settings.SETTINGS_MODULE.rsplit(".", 1)[0]
        old_config_dir = base / Path(*old_config_folder_name.split("."))

        # 删除旧 config/ 目录（已加载到内存的模块不受影响）
        if old_config_dir.exists():
            self.stdout.write('删除旧 config/ 目录（已加载到内存的模块不受影响）')
            self._force_rmtree(old_config_dir)

        # 若目标 config/ 目录仍存在（例如旧目录名不是 config 但磁盘有残留），先清掉
        if config_dir.exists():
            self._force_rmtree(config_dir)

        # 复制模板到目标位置
        shutil.copytree(_TEMPLATE_DIR, config_dir)

        # 替换 security.py 中的 SECRET_KEY 占位符
        new_key = get_random_string(50)
        security_file = config_dir / _NEW_SECURITY_FILE
        content = security_file.read_text(encoding='utf-8')
        content = content.replace('{{SECRET_KEY}}', new_key)
        security_file.write_text(content, encoding='utf-8')

        # 列出已生成的文件
        for f in sorted(config_dir.iterdir()):
            if f.is_file():
                self.stdout.write(f'  生成: {f.name}')
        self.stdout.write(self.style.SUCCESS('config/ 目录生成完成'))

    def _rewrite_manage_py(self):
        """改写项目根目录 manage.py 中的 DJANGO_SETTINGS_MODULE 引用。

        模板生成的新 config/ 入口是 config.dev，但项目原来的 manage.py 里
        很可能仍写死为旧入口（如 myproject.settings）。不改写的话，后续
        python manage.py ... 仍会加载旧配置。
        """
        self.stdout.write('正在改写 manage.py 中的配置入口...')
        base = Path(settings.BASE_DIR)
        manage_py = base / 'manage.py'

        if not manage_py.is_file():
            self.stdout.write(self.style.WARNING(
                f'未找到 {manage_py}，跳过 manage.py 改写'
            ))
            return

        original = manage_py.read_text(encoding='utf-8')
        old_module = settings.SETTINGS_MODULE

        # 优先匹配 os.environ.setdefault('DJANGO_SETTINGS_MODULE', '...')
        pattern = re.compile(
            r"""(os\.environ\.setdefault\(\s*['"]DJANGO_SETTINGS_MODULE['"]\s*,\s*['"])([^'"]+)(['"]\s*\))"""
        )
        content, count = pattern.subn(
            lambda m: f'{m.group(1)}{_NEW_SETTINGS_MODULE}{m.group(3)}',
            original,
        )

        # 兜底：任何 "DJANGO_SETTINGS_MODULE", "xxx" 形式
        if count == 0:
            pattern2 = re.compile(
                r"""(['"]DJANGO_SETTINGS_MODULE['"]\s*,\s*['"])([^'"]+)(['"])"""
            )
            content, count = pattern2.subn(
                lambda m: f'{m.group(1)}{_NEW_SETTINGS_MODULE}{m.group(3)}',
                original,
            )

        if count == 0:
            self.stdout.write(self.style.WARNING(
                'manage.py 中未找到 DJANGO_SETTINGS_MODULE 设置，跳过（请手动检查）'
            ))
            return

        if content == original:
            self.stdout.write(self.style.SUCCESS(
                f'manage.py 配置入口已经是 {_NEW_SETTINGS_MODULE}，无需修改'
            ))
            return

        manage_py.write_text(content, encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(
            f'manage.py 配置入口已改写: {old_module} → {_NEW_SETTINGS_MODULE}'
        ))

    @staticmethod
    def _force_rmtree(path: Path, retries: int = 3, delay: float = 0.3):
        """强制删除目录树，处理 Windows 上文件被锁定 / 只读的情况。"""
        def _on_error(func, error_path, exc_info):
            """清除只读属性后重试"""
            try:
                os.chmod(error_path, stat.S_IWRITE)
                func(error_path)
            except OSError:
                pass

        for attempt in range(retries):
            try:
                shutil.rmtree(path, onerror=_on_error)
                return
            except OSError:
                if attempt < retries - 1:
                    time.sleep(delay)
        # 最终回退：逐文件删除，跳过真正被锁定的
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                fp = os.path.join(root, name)
                try:
                    os.chmod(fp, stat.S_IWRITE)
                    os.remove(fp)
                except OSError:
                    pass
            for name in dirs:
                try:
                    os.rmdir(os.path.join(root, name))
                except OSError:
                    pass
        try:
            os.rmdir(path)
        except OSError:
            pass

    def _ensure_directories(self):
        """创建项目运行所需的基础目录（直接用 BASE_DIR 计算，不依赖自定义 settings 属性）"""
        base = Path(settings.BASE_DIR)
        dirs_to_create = [
            base / 'apps',
            base / 'data',
            base / 'logs',
            base / 'tmp',
            base / '.cache',
        ]
        for d in dirs_to_create:
            d.mkdir(parents=True, exist_ok=True)
            self.stdout.write(f'  目录就绪: {d}')
        self.stdout.write(self.style.SUCCESS('目录结构已就绪'))

    def _write_empty_cache(self):
        """写入空注册表缓存文件，确保 bootstrap.py 加载时不会报错退出"""
        self.stdout.write('正在写入初始缓存文件...')
        base = Path(settings.BASE_DIR)
        cache_dir = base / '.cache'
        cache_dir.mkdir(parents=True, exist_ok=True)

        empty_cache = {
            cache_dir / 'all_apps.json': {},
            cache_dir / 'enabled_apps.json': {},
            cache_dir / 'menus.json': {},
        }
        for filepath, data in empty_cache.items():
            filepath.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            self.stdout.write(f'  缓存文件: {filepath.name}')
        self.stdout.write(self.style.SUCCESS('初始缓存文件已写入'))

    def _run_migrations(self):
        """在独立子进程中执行数据库迁移。

        为什么必须新进程：
          1. INSTALLED_APPS 在 django.setup() 阶段就被 app registry 消费，
             内存里改 settings.INSTALLED_APPS 不会回溯生效；
          2. USING_AUTH 之类开关通常用于动态裁剪 INSTALLED_APPS，
             只有在新进程里重新 setup() 才能让 auth/admin 不被注册、不建表；
          3. 新进程从 manage.py 读 DJANGO_SETTINGS_MODULE=config.dev，
             磁盘上的配置是什么就是什么，没有任何内存残留。
        """
        self.stdout.write('正在以独立进程执行数据库迁移...')
        base = Path(settings.BASE_DIR)
        manage_py = base / 'manage.py'

        if not manage_py.is_file():
            self.stdout.write(self.style.ERROR(
                f'未找到 {manage_py}，无法执行迁移'
            ))
            raise SystemExit(1)

        env = os.environ.copy()
        # 显式指定新配置入口，即使 manage.py 改写失败也能保证子进程用新配置
        env['DJANGO_SETTINGS_MODULE'] = _NEW_SETTINGS_MODULE
        # 防止父进程 Python 路径继承造成干扰（一般无影响，加上更稳）
        env.pop('PYTHONSTARTUP', None)

        result = subprocess.run(
            [sys.executable, str(manage_py), 'migrate', '--verbosity', '1'],
            cwd=str(base),
            env=env,
        )

        if result.returncode != 0:
            self.stdout.write(self.style.ERROR(
                f'数据库迁移失败（退出码 {result.returncode}）'
            ))
            raise SystemExit(result.returncode)

        self.stdout.write(self.style.SUCCESS('数据库迁移完成'))