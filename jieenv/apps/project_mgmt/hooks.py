import logging

logger = logging.getLogger(__name__)


def on_enable(context):
    logger.info('project_mgmt enabled')


def on_disable(context):
    logger.info('project_mgmt disabled')