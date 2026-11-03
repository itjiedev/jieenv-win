"""
通用 JSON 持久化存储工具

为扩展应用提供开箱即用的 JSON 文件存储能力，解决以下共性问题：
- 线程安全的读写操作
- 原子写入（先写临时文件再替换，防止写入中断导致数据损坏）
- 错误处理与日志记录
- 自动创建父目录

支持两种存储模式：
- 列表模式（默认）：JSON 根节点为 list，适用于记录集合（如实例列表）
- 字典模式：JSON 根节点为 dict，适用于配置/键值数据

使用示例：

    # 列表模式（记录集合）
    from jiecore.json_storage import JsonStore

    store = JsonStore(settings.DATA_DIR / 'myapp_records.json')
    store.add({'name': '测试', 'value': 42})
    records = store.all()
    record = store.get(record_id)
    store.update(record_id, value=100)
    store.remove(record_id)

    # 字典模式（配置/键值）
    config_store = JsonStore(settings.DATA_DIR / 'myapp_config.json', default={})
    config_store.data  # -> {}（初始为空字典）
    config_store.write({'install_dir': '/opt'})
"""
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class JsonStore:
    """
    线程安全的 JSON 文件存储。

    参数:
        filepath: JSON 文件路径（自动创建父目录）
        default: 文件不存在或损坏时的默认值，
                 [] 表示列表模式，{} 表示字典模式

    属性:
        filepath: JSON 文件路径（Path 对象）

    列表模式方法（default=[] 时可用）:
        all()           返回全部记录的拷贝列表
        get(id)         按 id 字段查找，返回 dict 或 None
        find(**match)   按字段精确匹配查找第一条，返回 dict 或 None
        add(record)     追加记录（浅拷贝），返回写入的 dict
        update(id, **fields)  按 id 更新字段，返回更新后的 dict 或 None
        remove(id)      按 id 删除，返回是否删除成功

    通用方法:
        read()          读取并返回完整数据（拷贝）
        write(data)     写入数据（原子写入）
    """

    def __init__(self, filepath: Path, default=None):
        self.filepath = Path(filepath)
        self._default = default if default is not None else []
        self._lock = threading.Lock()

    # ── 底层读写 ──────────────────────────────────────────

    def read(self):
        """读取完整数据（返回拷贝），文件不存在或损坏时返回默认值"""
        if not self.filepath.exists():
            return self._copy_default()
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, type(self._default)) else self._copy_default()
        except (json.JSONDecodeError, IOError) as e:
            logger.error('读取 JSON 存储失败 [%s]: %s', self.filepath.name, e)
            return self._copy_default()

    def write(self, data):
        """原子写入数据（先写临时文件再替换），自动创建父目录"""
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.filepath.with_suffix(self.filepath.suffix + '.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.filepath)
        except IOError as e:
            logger.error('写入 JSON 存储失败 [%s]: %s', self.filepath.name, e)
            # 清理可能残留的临时文件
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # ── 列表模式方法 ──────────────────────────────────────

    def all(self) -> list:
        """返回全部记录的拷贝列表"""
        with self._lock:
            data = self.read()
            return [dict(item) if isinstance(item, dict) else item for item in data]

    def get(self, record_id: str, id_field: str = 'id') -> dict | None:
        """按 id 字段查找记录，不存在返回 None"""
        with self._lock:
            for item in self.read():
                if isinstance(item, dict) and item.get(id_field) == record_id:
                    return dict(item)
        return None

    def find(self, id_field: str = 'id', **match) -> dict | None:
        """
        按字段精确匹配查找第一条记录（空值字段忽略），不存在返回 None

        示例:
            store.find('id', name='test', status='active')
        """
        with self._lock:
            for item in self.read():
                if not isinstance(item, dict):
                    continue
                if all(item.get(k) == v for k, v in match.items() if v):
                    return dict(item)
        return None

    def add(self, record: dict) -> dict:
        """追加记录（浅拷贝），返回写入的 dict"""
        record = dict(record)
        with self._lock:
            data = self.read()
            data.append(record)
            self.write(data)
        return dict(record)

    def update(self, record_id: str, id_field: str = 'id', **fields) -> dict | None:
        """按 id 更新字段，返回更新后的 dict；不存在返回 None"""
        with self._lock:
            data = self.read()
            for item in data:
                if isinstance(item, dict) and item.get(id_field) == record_id:
                    item.update(fields)
                    self.write(data)
                    return dict(item)
        return None

    def remove(self, record_id: str, id_field: str = 'id') -> bool:
        """按 id 删除记录，返回是否删除成功"""
        with self._lock:
            data = self.read()
            remain = [item for item in data
                      if not (isinstance(item, dict) and item.get(id_field) == record_id)]
            if len(remain) == len(data):
                return False
            self.write(remain)
        return True

    # ── 内部工具 ──────────────────────────────────────────

    def _copy_default(self):
        """返回默认值的浅拷贝"""
        if isinstance(self._default, list):
            return list(self._default)
        if isinstance(self._default, dict):
            return dict(self._default)
        return self._default
