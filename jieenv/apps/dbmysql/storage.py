"""
MySQL 实例记录存储。

本应用不使用 Django ORM 模型，而是把 MySQL 实例记录持久化到
data/dbmysql_instances.json，避免与用户自建的 MySQL 实例产生耦合。

记录结构:
    {
        'id':             uuid 字符串
        'name':           显示名称（服务名/目录名或自定义名）
        'host':           主机地址（默认 127.0.0.1）
        'port':           端口（int）
        'version':        MySQL 版本号（解析/安装得到，可为空）
        'source':         来源: manual(用户按目录手动添加) / installed(本应用安装)
        'install_dir':    安装目录（可为空）
        'data_dir':       数据目录（可为空）
        'service_name':   Windows 服务名（可为空）
        'run_mode':       service(Windows服务) / process(普通进程) / unknown
        'default_user':   默认登录用户提示（如 root）
        'created_at':     记录创建时间
        'remark':         备注
        'install_method': 安装方式: zip / msi / 空(远程连接或未知)
    }

出于安全考虑，不保存任何密码；登录时由用户输入。
"""
import uuid
import logging
from datetime import datetime

from jiecore.json_storage import JsonStore
from .config import STORAGE_FILE

logger = logging.getLogger(__name__)

_store = JsonStore(STORAGE_FILE, default=[])


def list_instances() -> list:
    """返回全部实例记录（浅拷贝列表）"""
    return _store.all()


def get_instance(instance_id: str) -> dict:
    """按 id 查找实例，不存在返回 None"""
    return _store.get(instance_id)


def find_instance(**match) -> dict:
    """按字段精确匹配查找第一个实例（空值字段忽略），不存在返回 None"""
    return _store.find(**match)


def add_instance(record: dict) -> dict:
    """新增实例记录，自动补全 id 与 created_at，返回完整记录"""
    record = dict(record)
    record.setdefault('id', uuid.uuid4().hex[:12])
    record.setdefault('host', '127.0.0.1')
    record['port'] = int(record.get('port') or 3306)
    record.setdefault('source', 'manual')
    record.setdefault('install_dir', '')
    record.setdefault('data_dir', '')
    record.setdefault('service_name', '')
    record.setdefault('run_mode', 'unknown')
    record.setdefault('default_user', 'root')
    record.setdefault('version', '')
    record.setdefault('remark', '')
    record.setdefault('install_method', '')
    record['created_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _store.add(record)
    logger.info('登记 MySQL 实例: %s(%s:%s)', record.get('name'), record['host'], record['port'])
    return dict(record)


def update_instance(instance_id: str, **fields) -> dict:
    """更新实例字段，返回更新后的记录；不存在返回 None"""
    return _store.update(instance_id, **fields)


def remove_instance(instance_id: str) -> bool:
    """删除实例记录，返回是否删除成功"""
    result = _store.remove(instance_id)
    if result:
        logger.info('移除 MySQL 实例记录: %s', instance_id)
    return result
