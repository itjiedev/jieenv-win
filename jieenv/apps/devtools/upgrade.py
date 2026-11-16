import logging

logger = logging.getLogger(__name__)


def _parse_version(version: str) -> tuple:
    return tuple(int(x) for x in version.split('.') if x.isdigit())


def upgrade(old_version: str, new_version: str) -> None:
    logger.info('devtools 升级: %s -> %s', old_version, new_version)