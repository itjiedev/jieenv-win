from django.db import models

class BaseNoPermissionModel(models.Model):
    class Meta:
        abstract = True
        default_permissions = ()


class BaseUpdateTimeModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        abstract = True


class AppRegistry(models.Model):
    """
    应用注册表（仅数据库模式使用）。
    记录所有已安装应用的元信息、版本、状态和依赖。
    """
    app_name = models.CharField('应用包名', max_length=100, unique=True)
    display_name = models.CharField('显示名称', max_length=200, default='')
    version = models.CharField('版本号', max_length=50, default='0.0.1')
    is_enabled = models.BooleanField('已启用', default=False)
    is_root = models.BooleanField('根应用', default=False)
    url_prefix = models.CharField('URL前缀', max_length=100, blank=True, default='')
    dependencies = models.JSONField('依赖应用', default=dict, blank=True)
    description = models.CharField('应用描述', max_length=500, blank=True, default='')
    changelog = models.TextField('更新日志', blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '应用注册表'
        verbose_name_plural = '应用注册表'
        ordering = ['app_name']

    def __str__(self):
        return f'{self.display_name}({self.app_name}) v{self.version}'

    def to_dict(self):
        return {
            'app_name': self.app_name,
            'display_name': self.display_name,
            'version': self.version,
            'is_enabled': self.is_enabled,
            'is_root': self.is_root,
            'url_prefix': self.url_prefix,
            'dependencies': self.dependencies,
            'description': self.description,
            'changelog': self.changelog,
        }


class AppMenuItem(models.Model):
    """
    聚合菜单表（仅数据库模式使用）。
    由 menu_aggregator 在刷新时写入，运行时读取。
    """
    menu_id = models.CharField('菜单ID', max_length=64, unique=True)
    parent_id = models.CharField('父菜单ID', max_length=64, blank=True, default='')
    app_name = models.CharField('所属应用', max_length=100)
    name = models.CharField('菜单名称', max_length=64)
    icon = models.CharField('图标', max_length=128, blank=True, default='')
    sort = models.IntegerField('排序', default=0)
    is_show = models.BooleanField('显示', default=True)
    perm_code = models.CharField('权限码', max_length=128, blank=True, default='')
    path_config = models.JSONField('路径配置', default=dict)
    link = models.CharField('链接地址', max_length=500, blank=True, default='#')
    created_at = models.DateTimeField('创建时间', auto_now=True)

    class Meta:
        verbose_name = '菜单项'
        verbose_name_plural = '菜单项'
        ordering = ['sort']

    def __str__(self):
        return f'{self.name}({self.menu_id})'

    def to_dict(self):
        return {
            'menu_id': self.menu_id,
            'parent_id': self.parent_id,
            'app_name': self.app_name,
            'name': self.name,
            'icon': self.icon,
            'sort': self.sort,
            'is_show': self.is_show,
            'perm_code': self.perm_code,
            'path': self.path_config,
            'link': self.link,
        }


class OperationLog(models.Model):
    """
    通用操作日志（仅数据库模式使用）。
    各应用通过 jiecore.op_log 模块记录关键操作，
    首页"最近操作"板块从此表读取。
    """
    app_name = models.CharField('应用标识', max_length=100)
    action = models.CharField('操作描述', max_length=255)
    success = models.BooleanField('是否成功', default=True)
    detail = models.TextField('补充信息', blank=True, default='')
    created_at = models.DateTimeField('操作时间', auto_now_add=True)

    class Meta:
        verbose_name = '操作日志'
        verbose_name_plural = '操作日志'
        ordering = ['-created_at']
