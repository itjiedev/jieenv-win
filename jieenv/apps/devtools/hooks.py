import logging

logger = logging.getLogger(__name__)


def on_enable(app):
    logger.info('devtools on_enable: 工具已由 apps.py ready() + register_devtool() 完成注册')


def on_disable(app):
    pass


def on_uninstall(app):
    try:
        from jiecore.provider_catalog import unregister_providers_by_app
        unregister_providers_by_app('devtools')
        logger.info('devtools on_uninstall: 已注销 provider_catalog')
    except Exception as e:
        logger.warning('devtools on_uninstall: provider 注销失败: %s', e)