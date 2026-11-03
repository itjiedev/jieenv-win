"""
日志配置
- 文件输出：按应用自动分流到 logs/{app_name}.log
- 错误日志：所有 ERROR 及以上统一记录到 logs/error.log
- 控制台输出：开发环境同时输出到命令行
- 日志级别：通过 LOG_LEVEL 配置项控制（DEBUG/INFO/WARNING/ERROR）
"""
from .constants import LOG_DIR

LOG_LEVEL = 'DEBUG'  # 默认日志级别，可在 dev.py / production.py 中覆盖

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] [{levelname}] [{name}] {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        'simple': {
            'format': '[{levelname}] {message}',
            'style': '{',
        },
    },
    'filters': {
        'require_debug_true': {
            '()': 'django.utils.log.RequireDebugTrue',
        },
    },
    'handlers': {
        # 按应用自动分流的日志文件
        'per_app': {
            '()': 'config.log_handler.PerAppFileHandler',
            'log_dir': str(LOG_DIR),
            'maxBytes': 10 * 1024 * 1024,  # 10MB
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
        # 错误日志文件（仅 ERROR 及以上，所有应用合并）
        'file_error': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(LOG_DIR / 'error.log'),
            'maxBytes': 10 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
        # 控制台输出
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'loggers': {
        # 根日志：所有日志（含项目应用 + 未分类来源）
        '': {
            'handlers': ['per_app', 'file_error', 'console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        # Django 框架日志
        'django': {
            'handlers': ['per_app', 'file_error', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
        # Django 请求日志
        'django.request': {
            'handlers': ['per_app', 'file_error'],
            'level': 'WARNING',
            'propagate': False,
        },
        # Django 数据库日志
        'django.db.backends': {
            'handlers': ['file_error'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}


def apply_log_level(level: str):
    """动态设置日志级别（供 dev.py / production.py 调用）"""
    level = level.upper()
    LOGGING['loggers']['']['level'] = level
    LOGGING['loggers']['']['handlers'] = ['per_app', 'file_error', 'console']


def disable_console():
    """移除控制台 handler（生产环境调用）"""
    for logger_conf in LOGGING['loggers'].values():
        handlers = logger_conf.get('handlers', [])
        if 'console' in handlers:
            handlers.remove('console')
