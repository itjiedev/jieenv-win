from .base import *
from .logging import apply_log_level, disable_console  # noqa: F401

DEBUG = False
# server.py 以 0.0.0.0 监听并承诺局域网访问，此处若只列 127.0.0.1，局域网用户用
# IP/主机名访问会全部被 Django 拦成 DisallowedHost(400)；与桌面壳走的 config.dev
# （'*'）保持一致。如需收紧访问范围，请同时修改 server.py 的 listen 地址
ALLOWED_HOSTS = ['*']

# 生产环境：WARNING 级别日志 + 仅文件输出（无控制台）
# 可修改为 'INFO' / 'WARNING' / 'ERROR' 控制日志详细程度
apply_log_level('WARNING')
disable_console()