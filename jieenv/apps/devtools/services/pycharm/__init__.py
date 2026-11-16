def register_devtool():
    from apps.devtools.services.pycharm.installer import get_pycharm_options

    info = {
        'tool_id': 'pycharm',
        'label': 'PyCharm',
        'category': 'ide',
        'sort': 10,
        'icon_static': 'devtools/pycharm.svg',
        'official_url': 'https://www.jetbrains.com/zh-cn/pycharm/download/other/',
        'detail_viewname': 'devtools:pycharm_detail',
        'install_viewname': 'devtools:pycharm_versions',
        'start_viewname': 'devtools:api_start_pycharm',
        'install_check_fn': get_pycharm_options,
    }

    try:
        from jiecore.provider_catalog import register_provider
        register_provider({
            'category': 'ide',
            'provider_id': 'pycharm',
            'app_name': 'devtools',
            'label': 'PyCharm',
            'icon': 'fa-brands fa-jetbrains',
            'info': info,
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('PyCharm provider 注册失败: %s', e)

    return info