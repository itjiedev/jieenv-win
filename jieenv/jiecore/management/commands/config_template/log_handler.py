"""
按应用自动分流的日志 Handler
根据 logger 名称自动提取应用名，将日志路由到 logs/{app_name}.log。

应用名提取规则：
- apps.home.views       → home       → logs/home.log
- apps.dataprovider.models → dataprovider → logs/dataprovider.log
- apps.mainapp.views    → mainapp    → logs/mainapp.log
- jiecore.app_manager   → jiecore    → logs/jiecore.log
- django.request        → django     → logs/django.log
- 其他未知来源           → 默认        → logs/app.log
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class PerAppFileHandler(logging.Handler):
    """
    按应用名自动分流日志到不同文件。
    首次遇到某个应用名时自动创建对应的 RotatingFileHandler。
    """

    def __init__(self, log_dir: str, maxBytes: int = 10 * 1024 * 1024,
                 backupCount: int = 5, encoding: str = 'utf-8',
                 level=logging.NOTSET):
        super().__init__(level)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = maxBytes
        self.backup_count = backupCount
        self.encoding = encoding
        self._handlers: dict[str, RotatingFileHandler] = {}

    def _extract_app_name(self, name: str) -> str:
        """从 logger 名称提取应用名"""
        if not name:
            return 'app'
        parts = name.split('.')
        # apps.xxx.yyy → xxx
        if parts[0] == 'apps' and len(parts) >= 2:
            return parts[1]
        # jiecore.xxx → jiecore
        if parts[0] == 'jiecore':
            return parts[0]
        # django.xxx → django
        if parts[0] == 'django':
            return 'django'
        # config.xxx → config
        if parts[0] == 'config':
            return 'config'
        # 未知顶级名称，归入 app.log
        return 'app'

    def _get_handler(self, app_name: str) -> RotatingFileHandler:
        """获取或创建指定应用的文件 handler"""
        if app_name not in self._handlers:
            filename = self.log_dir / f'{app_name}.log'
            handler = RotatingFileHandler(
                str(filename),
                maxBytes=self.max_bytes,
                backupCount=self.backup_count,
                encoding=self.encoding,
            )
            # self.formatter 由 Django dictConfig 在创建后设置
            if self.formatter:
                handler.setFormatter(self.formatter)
            self._handlers[app_name] = handler
        return self._handlers[app_name]

    def emit(self, record: logging.LogRecord):
        app_name = self._extract_app_name(record.name)
        handler = self._get_handler(app_name)
        handler.emit(record)

    def close(self):
        for handler in self._handlers.values():
            handler.close()
        self._handlers.clear()
        super().close()
