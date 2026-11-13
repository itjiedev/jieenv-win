"""
conda 数据模型

定义应用的 Django ORM 模型。
定义了模型后必须执行 makemigrations 生成迁移文件，再随应用一起打包。

如果应用不需要 ORM 模型（如使用 JSON 文件存储），可保留此空文件。
"""
from django.db import models

# ── 示例：使用私有数据库的模型（取消注释按需使用）────────
# from jiecore.private_databases import PrivateDBModel
#
# class CondaRecord(PrivateDBModel):
#     PRIVATE_DB_ALIAS = 'conda_db01'  # 与 databases.py 中的别名对应
#
#     name = models.CharField('名称', max_length=100)
#     created_at = models.DateTimeField('创建时间', auto_now_add=True)
#
#     class Meta:
#         verbose_name = 'conda记录'
#         verbose_name_plural = verbose_name
#
#     def __str__(self):
#         return self.name
