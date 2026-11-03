# port_config.py
r"""
端口配置：server.py 与 desktop.py 共用的端口读取约定。

端口由根目录 refreshport.bat 随机选取（保证 >=8000 且当前不冲突）后写入
port.txt；两个启动入口在运行时读取该文件。文件缺失、内容非法或越界时回退到
DEFAULT_PORT，保证未执行刷新脚本时行为与以往一致（仍使用 8000）。
"""
import os

PRODUCT_DIR = os.path.dirname(os.path.abspath(__file__))
PORT_FILE = os.path.join(PRODUCT_DIR, 'port.txt')

DEFAULT_PORT = 8000     # port.txt 缺失/非法时的回退端口
MIN_PORT = 8000         # 与 refreshport.bat 的选取下限保持一致
MAX_PORT = 65535        # 可用端口上限


def read_port(default: int = DEFAULT_PORT) -> int:
    """读取 port.txt 中的端口号；文件缺失、内容非法或越界时回退 default。"""
    try:
        with open(PORT_FILE, 'r', encoding='utf-8') as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        return default
    if MIN_PORT <= port <= MAX_PORT:
        return port
    return default
