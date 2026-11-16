def register_devtool():
    from apps.devtools.services.dbeaver.installer import get_dbeaver_options

    info = {
        'tool_id': 'dbeaver',
        'label': 'DBeaver',
        'category': 'db',
        'sort': 30,
        'icon_static': 'devtools/dbeaver-beaver.png',
        'official_url': 'https://dbeaver.io/download/',
        'detail_viewname': 'devtools:dbeaver_detail',
        'install_viewname': 'devtools:dbeaver_versions',
        'start_viewname': 'devtools:api_start_dbeaver',
        'install_check_fn': get_dbeaver_options,
    }

    try:
        from jiecore.provider_catalog import register_provider
        register_provider({
            'category': 'ide',
            'provider_id': 'dbeaver',
            'app_name': 'devtools',
            'label': 'DBeaver',
            'icon': 'fa-solid fa-database',
            'info': info,
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('DBeaver provider 注册失败: %s', e)

    return info