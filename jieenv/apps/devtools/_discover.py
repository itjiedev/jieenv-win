import importlib
import logging
import pkgutil

logger = logging.getLogger(__name__)


def discover_and_register():
    from . import tool_registry

    services_pkg = importlib.import_module('apps.devtools.services')
    services_path = services_pkg.__path__

    for mod_info in pkgutil.iter_modules(services_path):
        if mod_info.ispkg:
            full_mod_name = f'apps.devtools.services.{mod_info.name}'
            try:
                mod = importlib.import_module(full_mod_name)
                register_fn = getattr(mod, 'register_devtool', None)
                if callable(register_fn):
                    info = register_fn()
                    if info:
                        tool_registry.register(info)
                        logger.info('devtools discover: registered tool %s (%s)',
                                    info.get('label'), info.get('tool_id'))
                else:
                    logger.debug('devtools discover: %s has no register_devtool', full_mod_name)
            except Exception as e:
                logger.warning('devtools discover: failed to load %s: %s', full_mod_name, e)