"""
Conda 发行版下载源配置。

Miniconda3 / Miniforge3 各维护官方源 + 国内镜像（清华、中科大、南大），
前端通过 api_list_download_sources 拉取此配置并渲染下载选择表单。
"""

INSTALLERS = {
    'miniconda3': {
        'name': 'Miniconda3',
        'description': '官方发行版，包含 conda + Python 3.x + 基础包',
        'filename': 'Miniconda3-latest-Windows-x86_64.exe',
        'default_subdir': 'Miniconda3',
        'mirrors': [
            {
                'id': 'tsinghua',
                'label': '清华 TUNA',
                'url': 'https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-latest-Windows-x86_64.exe',
            },
            {
                'id': 'ustc',
                'label': '中科大 USTC',
                'url': 'https://mirrors.ustc.edu.cn/anaconda/miniconda/Miniconda3-latest-Windows-x86_64.exe',
            },
            {
                'id': 'nju',
                'label': '南大 NJU',
                'url': 'https://mirrors.nju.edu.cn/anaconda/miniconda/Miniconda3-latest-Windows-x86_64.exe',
            },
            {
                'id': 'official',
                'label': '官方 (repo.anaconda.com)',
                'url': 'https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe',
            },
        ],
    },
    'miniforge3': {
        'name': 'Miniforge3',
        'description': 'conda-forge 社区版，默认源更全、更新更快',
        'filename': 'Miniforge3-Windows-x86_64.exe',
        'default_subdir': 'Miniforge3',
        'mirrors': [
            {
                'id': 'tsinghua',
                'label': '清华 TUNA',
                'url': 'https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/Miniforge3-Windows-x86_64.exe',
            },
            {
                'id': 'ustc',
                'label': '中科大 USTC',
                'url': 'https://mirrors.ustc.edu.cn/github-release/conda-forge/miniforge/LatestRelease/Miniforge3-Windows-x86_64.exe',
            },
            {
                'id': 'nju',
                'label': '南大 NJU',
                'url': 'https://mirrors.nju.edu.cn/github-release/conda-forge/miniforge/LatestRelease/Miniforge3-Windows-x86_64.exe',
            },
            {
                'id': 'official',
                'label': '官方 (GitHub Releases)',
                'url': 'https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe',
            },
        ],
    },
}


def get_installer(key: str) -> dict:
    return INSTALLERS.get(key, {})


def list_installers() -> list:
    return [
        {'key': k, 'name': v['name'], 'description': v['description'],
         'filename': v['filename'], 'default_subdir': v['default_subdir'],
         'mirrors': v['mirrors']}
        for k, v in INSTALLERS.items()
    ]