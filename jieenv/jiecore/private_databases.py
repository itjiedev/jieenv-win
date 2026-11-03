"""
jiecore 应用私有数据库连接支持（核心框架能力，供任意扩展应用复用）。

一、设计说明
============
- 应用的私有数据库连接配置完全定义在应用自身的 databases.py 中，
  不需要在项目的 settings 里写任何配置。
- 应用在 AppConfig.ready() 中调用 register_private_databases()
  把私有连接注入 Django 连接池（settings.DATABASES），
  因此应用内可以继续使用完整的 Django ORM。
- 全局 PrivateDatabaseRouter 做隔离控制：
  1. 只有声明了 PRIVATE_DB_ALIAS 的模型才会被路由到私有连接；
  2. 私有连接上只允许归属应用的私有模型建表（其他应用的迁移在私有库上全部被拒绝）；
  3. 私有模型不允许在主库建表，也不允许与其他库的模型建立关联关系。

二、应用侧如何接入
==================
1) 在应用内新建 databases.py，定义 PRIVATE_DATABASES 字典：
   每个条目 = 一个私有连接，键为连接别名（建议带应用前缀避免冲突，
   如 'si_db01'），值为标准 Django DATABASES 配置格式（ENGINE / NAME / USER 等）。
2) 在 apps.py 的 ready() 中注册：
       from jiecore.private_databases import register_private_databases
       from . import databases
       register_private_databases('<app_name>', databases.PRIVATE_DATABASES)
3) 模型继承 PrivateDBModel 并声明 PRIVATE_DB_ALIAS 即可把表放入对应私有库，
   ORM 操作自动走该私有连接；执行原生 SQL 可用 private_cursor(alias)。

三、各数据库配置示例
====================
【SQLite（Django 内置，无需安装驱动）】
    'xx_db01': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(DB_DIR, 'db01.sqlite3'),   # 数据库文件绝对/相对路径
    },

【MySQL】需先安装驱动：pip install mysqlclient（Windows 可用 pip install pymysql，
    需在项目入口执行 pymysql.install_as_MySQLdb()；Django 5.x 要求 MySQL 8.0.11+）
    'xx_db01': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'my_db01',               # 数据库名（需提前在 MySQL 中建好库）
        'USER': 'root',
        'PASSWORD': 'your_password',
        'HOST': '127.0.0.1',
        'PORT': '3306',
        'OPTIONS': {'charset': 'utf8mb4'},   # 可选，避免中文乱码
    },

【PostgreSQL】需先安装驱动：pip install psycopg[binary]（或 psycopg2-binary）
    'xx_db01': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'my_db01',               # 数据库名（需提前在 PostgreSQL 中建好库）
        'USER': 'postgres',
        'PASSWORD': 'your_password',
        'HOST': '127.0.0.1',
        'PORT': '5432',
    },

【Oracle】需先安装驱动：pip install oracledb（Django 5.x 官方支持 python-oracledb，
    不再支持旧版 cx_Oracle）
    'xx_db01': {
        'ENGINE': 'django.db.backends.oracle',
        # thin 模式（默认）：纯 Python 实现，无需安装 Oracle Instant Client；
        # thick 模式需在 OPTIONS 中加 {'driver': 'thick'} 并安装客户端库
        'NAME': '192.168.1.10:1521/ORCLPDB1',   # 完整 DSN（主机:端口/服务名）
        'USER': 'your_user',
        'PASSWORD': 'your_password',
        # 也可以拆开写：'NAME': 'ORCLPDB1', 'HOST': '192.168.1.10', 'PORT': '1521'
    },

【SQL Server】Django 官方不内置，需第三方后端：pip install mssql-django
    'xx_db01': {
        'ENGINE': 'mssql',
        'NAME': 'my_db01',
        'USER': 'sa',
        'PASSWORD': 'your_password',
        'HOST': '127.0.0.1',
        'PORT': '1433',
        'OPTIONS': {'driver': 'ODBC Driver 17 for SQL Server'},  # 需已安装对应 ODBC 驱动
    },

四、各数据库注意事项
====================
- MySQL：建库时建议 utf8mb4 字符集；OPTIONS 中设置 charset 可避免乱码。
- PostgreSQL / MySQL：Django 不会自动创建数据库本身，需要先手动建库。
- Oracle：未加引号的表名/列名默认按大写存储，Django 映射已有表
  （db_table='users'）可正常匹配 USERS 表；TextField 会映射为 CLOB。
  若只读已有表，模型建议 managed = False，避免迁移需要建表权限。
- SQL Server：必须使用 mssql-django 后端并安装对应版本的 ODBC 驱动。
- SQLite：不支持部分 ALTER TABLE 操作，复杂模型变更可能需要重建表
  （Django 会自动处理大多数场景）。

五、引用示例（应用内使用，与数据库类型无关）
============================================
    from apps.myapp.models import MyPrivateRecord
    from apps.myapp.databases import XX_DB01_ALIAS
    from jiecore.private_databases import private_cursor

    # 1) 默认管理器已自动绑定私有连接，直接像平常一样用 ORM
    MyPrivateRecord.objects.create(name='测试数据')          # 写入私有库
    rows = MyPrivateRecord.objects.all()                     # 读取私有库

    # 2) 也可以显式指定连接（效果相同）
    MyPrivateRecord.objects.using(XX_DB01_ALIAS).count()

    # 3) 需要执行原生 SQL 时，通过 private_cursor 获取私有连接的游标
    with private_cursor(XX_DB01_ALIAS) as cursor:
        cursor.execute('SELECT username FROM users')
        data = cursor.fetchall()

六、新增私有模型的迁移方式
==========================
    # 1) 在 models.py 中定义模型（继承 PrivateDBModel，声明 PRIVATE_DB_ALIAS）
    # 2) 生成迁移
    python manage.py makemigrations <app_name>
    # 3) 到对应私有库执行建表（表建在哪个库，就指定哪个别名）
    python manage.py migrate <app_name> --database=<私有连接别名>
    # 说明：映射已有表的模型设置 managed = False，无需迁移
"""
import logging

from django.apps import apps
from django.conf import settings
from django.db import connections, models

logger = logging.getLogger(__name__)

# 全局私有连接注册表：alias -> 归属应用名（app_label）
_PRIVATE_REGISTRY = {}

ROUTER_PATH = f'{__name__}.PrivateDatabaseRouter'


def register_private_databases(app_label, private_databases):
    """
    注册应用的私有数据库连接（在应用 AppConfig.ready() 中调用）。

    把 private_databases 中定义的所有私有连接注入 Django 连接池，
    并注册全局隔离路由器（仅注册一次）。在任何数据库连接被真正建立之前
    执行，因此无需修改项目 settings 即可生效。

    参数:
        app_label: 应用名（与 app_meta.py 中的 app_name 一致），用于归属隔离
        private_databases: {别名: 标准 Django DATABASES 配置} 字典
    """
    for alias, config in (private_databases or {}).items():
        owner = _PRIVATE_REGISTRY.get(alias)
        if owner:
            if owner == app_label:
                # 同一应用重复注册（如进程内多次 ready），幂等处理
                settings.DATABASES.setdefault(alias, config)
                continue
            logger.error('私有数据库别名 %s 冲突：已归属应用 %s，忽略应用 %s 的重复注册',
                         alias, owner, app_label)
            continue
        _PRIVATE_REGISTRY[alias] = app_label
        settings.DATABASES.setdefault(alias, config)

    routers = list(getattr(settings, 'DATABASE_ROUTERS', []))
    if ROUTER_PATH not in routers:
        routers.append(ROUTER_PATH)
    settings.DATABASE_ROUTERS = routers
    # 若连接处理器已提前初始化（例如其他应用先触发了数据库访问），
    # 重新规范化一次配置，补全 Django 要求的默认键（TIME_ZONE 等）
    if 'settings' in connections.__dict__:
        connections.__dict__['settings'] = connections.configure_settings(dict(settings.DATABASES))


def get_private_aliases(app_label=None):
    """
    获取已注册的私有连接别名。
    app_label 为 None 时返回全部别名；否则只返回归属该应用的别名。
    """
    if app_label is None:
        return tuple(_PRIVATE_REGISTRY)
    return tuple(a for a, owner in _PRIVATE_REGISTRY.items() if owner == app_label)


def private_cursor(alias):
    """
    获取私有连接的数据库游标（支持 with 语法），用于执行原生 SQL。
    仅接受已注册的私有连接别名，其他别名一律拒绝。
    """
    if alias not in _PRIVATE_REGISTRY:
        raise ValueError(f'非法的私有数据库别名: {alias}')
    return connections[alias].cursor()


class PrivateDatabaseRouter:
    """
    私有连接隔离路由器（全局，与数据库类型无关）：
    - 读写：只有声明了 PRIVATE_DB_ALIAS 的模型才路由到私有连接；
    - 迁移：私有库只允许归属应用的私有模型建表，其他模型一律拒绝；
    - 关系：私有模型禁止与不同库的模型建立关联。
    """

    @staticmethod
    def _model_alias(model):
        if model is None:
            return None
        alias = getattr(model, 'PRIVATE_DB_ALIAS', None)
        return alias if alias in _PRIVATE_REGISTRY else None

    def db_for_read(self, model, **hints):
        return self._model_alias(model)

    def db_for_write(self, model, **hints):
        return self._model_alias(model)

    def allow_relation(self, obj1, obj2, **hints):
        alias1 = self._model_alias(type(obj1))
        alias2 = self._model_alias(type(obj2))
        if alias1 or alias2:
            # 涉及私有库模型时，只允许同一私有库内的模型建立关系
            return alias1 == alias2
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        model = hints.get('model')
        alias = self._model_alias(model)
        if alias is None:
            # 迁移执行时传入的是历史状态模型，不保留 PRIVATE_DB_ALIAS 类属性，
            # 需要按名称反查当前真实模型才能识别私有模型
            name = model_name or (
                model._meta.model_name if getattr(model, '_meta', None) else None
            )
            if name:
                try:
                    alias = self._model_alias(apps.get_model(app_label, name))
                except LookupError:
                    alias = None
        if db in _PRIVATE_REGISTRY:
            # 私有库上只允许建属于本库的私有模型
            return alias == db
        if alias is not None:
            # 私有模型不允许在主库或其他库上建表
            return False
        return None


class PrivateDBManager(models.Manager):
    """私有库模型的默认管理器：ORM 查询自动使用模型对应的私有连接。"""

    def get_queryset(self):
        return super().get_queryset().using(self.model.PRIVATE_DB_ALIAS)


class PrivateDBModel(models.Model):
    """
    私有库模型抽象基类（与数据库类型无关）。

    子类只需声明 PRIVATE_DB_ALIAS（已注册的任意私有连接别名）即可把表
    放入对应的私有数据库，且 ORM 操作自动走该私有连接。
    app_label 由 Django 根据子类所在应用自动推断，无需显式声明。
    """
    objects = PrivateDBManager()

    class Meta:
        abstract = True
