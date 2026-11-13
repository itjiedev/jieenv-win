"""
conda 应用配置

定义应用常量、数据目录、临时目录、镜像源预设、频道预设等。
以后修改镜像源地址、频道组合、目录名都改这里。
"""
import os
import json
import logging

from django.conf import settings
from config.constants import ENV_ROOT

logger = logging.getLogger(__name__)

APP_LABEL = 'conda'
DATA_DIR = settings.DATA_DIR / APP_LABEL
TMP_DIR = settings.TMP_DIR / APP_LABEL
DOWNLOAD_DIR = TMP_DIR / 'conda_download'
CONDA_INSTALL_ROOT = ENV_ROOT / 'lang_envs'

VERSIONS_CACHE_FILE = DATA_DIR / 'versions.json'
ENVS_CACHE_FILE = DATA_DIR / 'envs.json'
VERSIONS_CACHE_TTL = 7 * 24 * 3600
ENVS_CACHE_TTL = 600

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

CORE_DEPENDENCIES = {
    'python', 'pip', 'setuptools', 'wheel',
    'conda', 'conda-build', 'conda-package-handling', 'conda-package-streaming',
    'libconda', 'conda-content-trust',
    'requests', 'urllib3', 'charset-normalizer', 'idna', 'certifi',
    'pycosat', 'ruamel.yaml', 'ruamel.yaml.clib',
    'bzip2', 'lz4-c', 'zstandard',
}

MIRROR_PRESETS = {
    'tsinghua': {
        'label': '清华大学',
        'default_channels': [
            'https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main',
            'https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r',
            'https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/msys2',
        ],
        'custom_channels': {
            'conda-forge': 'https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud',
            'bioconda': 'https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud',
            'pytorch': 'https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud',
        },
    },
    'ustc': {
        'label': '中国科学技术大学',
        'default_channels': [
            'https://mirrors.ustc.edu.cn/anaconda/pkgs/main',
            'https://mirrors.ustc.edu.cn/anaconda/pkgs/r',
            'https://mirrors.ustc.edu.cn/anaconda/pkgs/msys2',
        ],
        'custom_channels': {
            'conda-forge': 'https://mirrors.ustc.edu.cn/anaconda/cloud',
            'bioconda': 'https://mirrors.ustc.edu.cn/anaconda/cloud',
            'pytorch': 'https://mirrors.ustc.edu.cn/anaconda/cloud',
        },
    },
    'nju': {
        'label': '南京大学',
        'default_channels': [
            'https://mirror.nju.edu.cn/anaconda/pkgs/main',
            'https://mirror.nju.edu.cn/anaconda/pkgs/r',
            'https://mirror.nju.edu.cn/anaconda/pkgs/msys2',
        ],
        'custom_channels': {
            'conda-forge': 'https://mirror.nju.edu.cn/anaconda/cloud',
            'bioconda': 'https://mirror.nju.edu.cn/anaconda/cloud',
            'pytorch': 'https://mirror.nju.edu.cn/anaconda/cloud',
        },
    },
    'official': {
        'label': '官方默认（海外）',
    },
}

CHANNEL_PRESETS = {
    'defaults': {
        'label': 'defaults（Anaconda 官方 · 商业）',
        'channels': ['defaults'],
    },
    'conda_forge': {
        'label': 'conda-forge（免费）',
        'channels': ['conda-forge'],
        'channel_priority': 'flexible',
    },
    'forge_bioconda': {
        'label': 'conda-forge + bioconda（免费）',
        'channels': ['conda-forge', 'bioconda'],
        'channel_priority': 'flexible',
    },
    'forge_pytorch': {
        'label': 'conda-forge + pytorch（免费）',
        'channels': ['conda-forge', 'pytorch'],
        'channel_priority': 'flexible',
    },
}

for _d in [DATA_DIR, TMP_DIR, DOWNLOAD_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

PACKAGE_CATEGORIES = {
    'data_analysis': {
        'label': '数据分析',
        'icon': '📊',
        'packages': [
            {'name': 'anaconda', 'icon': '🅰', 'desc': '科学计算全套（元包·100+包）', 'base_only': True},
            {'name': 'pandas', 'icon': '🐼', 'desc': '数据处理与分析'},
            {'name': 'numpy', 'icon': '🔢', 'desc': '多维数组与数值计算'},
            {'name': 'matplotlib', 'icon': '📈', 'desc': '绑图表绘制'},
            {'name': 'seaborn', 'icon': '🎨', 'desc': '统计数据可视化'},
            {'name': 'openpyxl', 'icon': '📗', 'desc': 'Excel 文件读写'},
            {'name': 'xlrd', 'icon': '📕', 'desc': '旧版 Excel 读取'},
            {'name': 'sqlalchemy', 'icon': '🗄', 'desc': '数据库抽象层'},
            {'name': 'pandas-datareader', 'icon': '💹', 'desc': '金融数据接口'},
        ]
    },
    'ml': {
        'label': '机器学习',
        'icon': '🤖',
        'packages': [
            {'name': 'scikit-learn', 'icon': '🧠', 'desc': '经典机器学习'},
            {'name': 'tensorflow', 'icon': '🔥', 'desc': 'Google 深度学习框架'},
            {'name': 'pytorch', 'icon': '⚡', 'desc': 'Facebook 深度学习框架'},
            {'name': 'xgboost', 'icon': '🌲', 'desc': '极致梯度提升树'},
            {'name': 'lightgbm', 'icon': '🌳', 'desc': '微软高效梯度提升'},
            {'name': 'catboost', 'icon': '🐈', 'desc': 'Yandex 梯度提升'},
            {'name': 'onnx', 'icon': '📦', 'desc': '开放神经网络交换格式'},
        ]
    },
    'science': {
        'label': '科学计算',
        'icon': '🔬',
        'packages': [
            {'name': 'scipy', 'icon': '🧪', 'desc': '科学计算工具箱'},
            {'name': 'statsmodels', 'icon': '📊', 'desc': '统计建模与推断'},
            {'name': 'sympy', 'icon': '🔣', 'desc': '符号计算'},
            {'name': 'biopython', 'icon': '🧬', 'desc': '生物信息学'},
            {'name': 'astropy', 'icon': '🌌', 'desc': '天文计算'},
            {'name': 'shapely', 'icon': '🗺', 'desc': '几何操作'},
            {'name': 'geopandas', 'icon': '🌍', 'desc': '地理数据处理'},
        ]
    },
    'dev_tools': {
        'label': '开发工具',
        'icon': '🛠',
        'packages': [
            {'name': 'anaconda-navigator', 'icon': '🧭', 'desc': 'Anaconda 图形管理器', 'base_only': True},
            {'name': 'jupyterlab', 'icon': '📓', 'desc': '下一代 Jupyter IDE'},
            {'name': 'notebook', 'icon': '📒', 'desc': '经典 Jupyter Notebook'},
            {'name': 'spyder', 'icon': '🔍', 'desc': '科学计算 IDE'},
            {'name': 'jupyter_contrib_nbextensions', 'icon': '🧩', 'desc': 'Jupyter 扩展集合'},
            {'name': 'black', 'icon': '⬛', 'desc': '代码格式化'},
            {'name': 'flake8', 'icon': '🐍', 'desc': '代码检查'},
            {'name': 'pytest', 'icon': '🧪', 'desc': '单元测试'},
        ]
    },
    'web': {
        'label': 'Web 开发',
        'icon': '🕸',
        'packages': [
            {'name': 'django', 'icon': '🎸', 'desc': 'Python Web 框架'},
            {'name': 'flask', 'icon': '🧴', 'desc': '轻量 Web 框架'},
            {'name': 'fastapi', 'icon': '⚡', 'desc': '现代异步 Web 框架'},
            {'name': 'requests', 'icon': '🌐', 'desc': 'HTTP 请求库'},
            {'name': 'beautifulsoup4', 'icon': '🍲', 'desc': 'HTML 解析'},
            {'name': 'selenium', 'icon': '🌆', 'desc': '浏览器自动化'},
        ]
    },
    'r_ecosystem': {
        'label': 'R 生态',
        'icon': '📉',
        'packages': [
            {'name': 'r-base', 'icon': '📉', 'desc': 'R 语言运行时（核心）'},
            {'name': 'r-essentials', 'icon': '📦', 'desc': 'R 常用包集合（tidyverse等）'},
            {'name': 'r-recommended', 'icon': '📋', 'desc': 'R 官方推荐包集'},
            {'name': 'r-irkernel', 'icon': '🐍', 'desc': 'Jupyter R 内核'},
        ]
    },
    'visualization': {
        'label': '高级可视化',
        'icon': '🎨',
        'packages': [
            {'name': 'plotly', 'icon': '📊', 'desc': '交互式图表'},
            {'name': 'bokeh', 'icon': '🌿', 'desc': 'Python 交互式可视化'},
            {'name': 'altair', 'icon': '✨', 'desc': '声明式可视化'},
            {'name': 'pyvista', 'icon': '🏔', 'desc': '3D 科学可视化'},
        ]
    },
}

PACKAGE_BUNDLES = {
    'anaconda_full': {
        'label': 'Anaconda 全家桶',
        'desc': '元包一键装 · 科学计算全套 100+ 包',
        'icon': '🅰',
        'base_only': True,
        'packages': [
            'anaconda', 'anaconda-navigator',
        ]
    },
    'jupyter_stack': {
        'label': 'Jupyter 全家桶',
        'desc': 'Notebook + Lab + 扩展',
        'icon': '📓',
        'packages': [
            'jupyterlab', 'notebook', 'ipykernel', 'ipython',
            'jupyter_contrib_nbextensions', 'nbconvert', 'widgetsnbextension',
        ]
    },
    'data_science': {
        'label': '数据分析必备',
        'desc': 'pandas + numpy + matplotlib + seaborn + jupyter',
        'icon': '📊',
        'packages': [
            'pandas', 'numpy', 'matplotlib', 'seaborn', 'scipy',
            'jupyterlab', 'openpyxl', 'sqlalchemy',
        ]
    },
    'ml_basic': {
        'label': '机器学习入门',
        'desc': 'scikit-learn + pytorch + xgboost',
        'icon': '🤖',
        'packages': [
            'scikit-learn', 'pytorch', 'xgboost', 'numpy', 'pandas', 'matplotlib',
        ]
    },
    'web_stack': {
        'label': 'Python Web 开发',
        'desc': 'Django + Flask + FastAPI + 工具链',
        'icon': '🕸',
        'packages': [
            'django', 'flask', 'fastapi', 'requests', 'beautifulsoup4', 'selenium',
        ]
    },
}