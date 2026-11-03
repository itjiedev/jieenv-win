# regutil.py
"""Windows 注册表读写小工具：语义明确、类型保留、权限最小化、带日志。"""

import winreg
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------- 自定义异常 ----------

class RegError(OSError):
    """注册表操作失败（权限、句柄、API 等）。"""


# ---------- 内部：统一 open 的封装 ----------

_HIVE_NAME = {
    winreg.HKEY_LOCAL_MACHINE: 'HKLM',
    winreg.HKEY_CURRENT_USER: 'HKCU',
    winreg.HKEY_CLASSES_ROOT: 'HKCR',
    winreg.HKEY_USERS: 'HKU',
    winreg.HKEY_CURRENT_CONFIG: 'HKCC',
}


def _hive_name(hive: int) -> str:
    return _HIVE_NAME.get(hive, f'0x{hive:08X}')


def _loc(hive: int, subkey: str, name: Optional[str] = None) -> str:
    base = f'{_hive_name(hive)}\\{subkey}'
    return f'{base}\\{name}' if name else base


def _brief(value: Any, limit: int = 60) -> str:
    """把值摘要成短字符串，避免日志里出现超长 PATH。"""
    s = repr(value)
    return s if len(s) <= limit else s[:limit] + f'...({len(s)} chars)'


def _open(hive: int, subkey: str, access: int):
    """打开注册表键，失败抛 RegError，并保留原始 WinError。"""
    try:
        return winreg.OpenKey(hive, subkey, 0, access)
    except OSError as e:
        logger.error('打开注册表键失败 %s: %s', _loc(hive, subkey), e)
        raise RegError(f'打开注册表键失败: {_loc(hive, subkey)}: {e}') from e


# ---------- 读 ----------

def read_value(hive: int, subkey: str, name: str,
               default: Any = None) -> tuple[Any, Optional[int]]:
    """读取注册表值，返回 (value, vtype)。

    - 值不存在：返回 (default, None)
    - 键不存在 / 权限不足 / 其他 API 失败：抛 RegError
    - 值存在但为空串：返回 ('', vtype)——与“不存在”可区分
    """
    with _open(hive, subkey, winreg.KEY_READ) as key:
        try:
            value, vtype = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            logger.debug('注册表值不存在 %s，返回默认值 %s',
                         _loc(hive, subkey, name), _brief(default))
            return default, None
        except OSError as e:
            logger.error('读取注册表值失败 %s: %s',
                         _loc(hive, subkey, name), e)
            raise RegError(
                f'读取值失败: {_loc(hive, subkey, name)}: {e}') from e
        logger.debug('读取注册表值 %s = %s (type=0x%X)',
                     _loc(hive, subkey, name), _brief(value), vtype)
        return value, vtype


def read_str(hive: int, subkey: str, name: str,
             default: str = '') -> str:
    """read_value 的便捷版：只关心字符串值，不存在时返回 default。"""
    value, _ = read_value(hive, subkey, name, default=default)
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def exists(hive: int, subkey: str, name: Optional[str] = None) -> bool:
    """判断键（name=None）或值是否存在。不存在返回 False，权限不足抛 RegError。"""
    try:
        with _open(hive, subkey, winreg.KEY_READ) as key:
            if name is None:
                logger.debug('注册表键存在 %s', _loc(hive, subkey))
                return True
            try:
                winreg.QueryValueEx(key, name)
                logger.debug('注册表值存在 %s', _loc(hive, subkey, name))
                return True
            except FileNotFoundError:
                logger.debug('注册表值不存在 %s', _loc(hive, subkey, name))
                return False
    except RegError:
        raise


# ---------- 写 ----------

def write_value(hive: int, subkey: str, name: str, value: Any,
                vtype: Optional[int] = None) -> bool:
    """写入注册表值，返回是否真的发生了改变（幂等写入）。"""
    if vtype is None:
        if isinstance(value, str):
            vtype = winreg.REG_EXPAND_SZ
        elif isinstance(value, int):
            vtype = winreg.REG_DWORD
        else:
            raise ValueError(f'无法推断值类型: {type(value).__name__}')

    # 幂等判断：相同值不写，避免无谓的 WM_SETTINGCHANGE 广播
    try:
        old, old_type = read_value(hive, subkey, name, default=(None, None))
    except RegError:
        old, old_type = None, None
    if old == value and old_type == vtype:
        logger.debug('注册表值未变化，跳过写入 %s', _loc(hive, subkey, name))
        return False

    with _open(hive, subkey, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.SetValueEx(key, name, 0, vtype, value)
        except OSError as e:
            logger.error('写入注册表值失败 %s: %s',
                         _loc(hive, subkey, name), e)
            raise RegError(
                f'写入值失败: {_loc(hive, subkey, name)}: {e}') from e

    logger.info('注册表值已更新 %s (type=0x%X, old=%s → new=%s)',
                _loc(hive, subkey, name), vtype,
                _brief(old), _brief(value))
    return True


def delete_value(hive: int, subkey: str, name: str) -> bool:
    """删除注册表值，返回是否删除成功（不存在返回 False，不抛异常）。"""
    with _open(hive, subkey, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            logger.debug('删除时值不存在，忽略 %s', _loc(hive, subkey, name))
            return False
        except OSError as e:
            logger.error('删除注册表值失败 %s: %s',
                         _loc(hive, subkey, name), e)
            raise RegError(
                f'删除值失败: {_loc(hive, subkey, name)}: {e}') from e

    logger.info('注册表值已删除 %s', _loc(hive, subkey, name))
    return True


# ---------- PATH 专用高层封装 ----------

def read_path(hive: int, subkey: str,
              default: str = '') -> tuple[str, int]:
    """读 PATH 值，返回 (raw, vtype)。

    值不存在时返回 (default, REG_EXPAND_SZ)——PATH 的默认类型。
    """
    value, vtype = read_value(hive, subkey, 'Path', default=default)
    if not isinstance(value, str):
        value = str(value) if value is not None else default
    if vtype is None:
        logger.warning('PATH 值不存在，使用默认类型 REG_EXPAND_SZ: %s',
                       _loc(hive, subkey, 'Path'))
    return value or default, vtype or winreg.REG_EXPAND_SZ


def write_path(hive: int, subkey: str, raw: str,
               vtype: Optional[int] = None) -> bool:
    """写 PATH 值，返回是否发生改变。"""
    return write_value(hive, subkey, 'Path', raw,
                       vtype=vtype or winreg.REG_EXPAND_SZ)