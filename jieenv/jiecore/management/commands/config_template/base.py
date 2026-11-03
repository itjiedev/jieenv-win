# Settings 模块加载顺序：
#   security.py  → SECRET_KEY 等安全配置
#   constants.py → 路径、数据库、功能开关等纯常量
#   bootstrap.py → INSTALLED_APPS（启动期引导逻辑）
#   extra.py     → SITE_SKIN、SITE_TITLE 等站点配置
#   logging.py   → LOGGING、LOG_LEVEL
from .security import *
from .constants import *
from .bootstrap import INSTALLED_APPS
from .extra import *
from .logging import LOGGING, LOG_LEVEL

DEBUG = False

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
# USING_AUTH=False 时移除认证中间件
if not USING_AUTH:
    _DB_MIDDLEWARES = {
        'django.contrib.auth.middleware.AuthenticationMiddleware',
    }
    MIDDLEWARE = [m for m in MIDDLEWARE if m not in _DB_MIDDLEWARES]

# 上下文处理器：按 USING_AUTH 条件加载 auth 相关处理器
_context_processors = ['django.template.context_processors.request']
if USING_AUTH:
    _context_processors.append('django.contrib.auth.context_processors.auth')
_context_processors.append('django.contrib.messages.context_processors.messages')
_context_processors += [
    'jiecore.context_processors.global_site',
    'jiecore.context_processors.global_site_skin',
]

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': _context_processors,
        },
    },
]

if USING_AUTH:
    AUTH_PASSWORD_VALIDATORS = [
        {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
        {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
        {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
        {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
    ]
else:
    AUTH_PASSWORD_VALIDATORS = []
