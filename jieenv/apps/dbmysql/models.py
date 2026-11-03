# dbmysql 应用不使用 Django ORM 模型。
#
# MySQL 实例登记数据采用 JSON 文件持久化，见 storage.py
# （data/dbmysql_instances.json），避免与用户自建的 MySQL 实例产生耦合。
