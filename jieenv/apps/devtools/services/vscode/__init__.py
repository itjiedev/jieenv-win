def register_devtool():
    from apps.devtools.services.vscode.installer import get_vscode_options

    info = {
        'tool_id': 'vscode',
        'label': 'VSCode',
        'category': 'ide',
        'sort': 20,
        'icon_static': 'devtools/vscode.svg',
        'official_url': 'https://code.visualstudio.com/Download',
        'detail_viewname': 'devtools:vscode_detail',
        'install_viewname': 'devtools:vscode_versions',
        'start_viewname': 'devtools:api_start_vscode',
        'install_check_fn': get_vscode_options,
    }

    try:
        from jiecore.provider_catalog import register_provider
        register_provider({
            'category': 'ide',
            'provider_id': 'vscode',
            'app_name': 'devtools',
            'label': 'VSCode',
            'icon': 'fa-solid fa-code',
            'info': info,
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('VSCode provider 注册失败: %s', e)

    return info