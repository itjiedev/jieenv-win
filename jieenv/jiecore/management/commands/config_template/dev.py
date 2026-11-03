from .base import *
from .logging import apply_log_level  # noqa: F401

DEBUG = True
ALLOWED_HOSTS = ['*']

# 开发环境：DEBUG 级别日志 + 控制台输出
apply_log_level('DEBUG')