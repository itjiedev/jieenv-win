import os
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from .config import MIRROR_PRESETS, CHANNEL_PRESETS

logger = __import__('logging').getLogger(__name__)


def get_mirror_list() -> list:
    result = []
    for mid, preset in MIRROR_PRESETS.items():
        item = {'id': mid, 'label': preset['label']}
        if 'default_channels' in preset:
            item['default_channels'] = preset['default_channels']
        if 'custom_channels' in preset:
            item['custom_channels'] = preset['custom_channels']
        result.append(item)
    return result


def get_channel_list() -> list:
    result = []
    for cid, preset in CHANNEL_PRESETS.items():
        item = {'id': cid, 'label': preset['label']}
        if 'channels' in preset:
            item['channels'] = preset['channels']
        if 'channel_priority' in preset:
            item['channel_priority'] = preset['channel_priority']
        result.append(item)
    return result


def _home_conda_dir() -> Path:
    d = Path(os.path.expanduser('~')) / '.conda'
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_condarc_path() -> Path:
    return _home_conda_dir() / '.condarc'


def read_config() -> dict:
    path = get_condarc_path()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding='utf-8')
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as e:
        logger.warning('read_config YAML 解析失败 %s: %s', path, e)
        return {}
    except OSError as e:
        logger.warning('read_config 读取失败 %s: %s', path, e)
        return {}


def write_config(cfg: dict) -> dict:
    path = get_condarc_path()
    try:
        text = yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
        path.write_text(text, encoding='utf-8')
        return {'success': True, 'message': f'已保存到 {path}'}
    except OSError as e:
        return {'success': False, 'message': f'写入失败: {e}'}


def get_value(cfg: dict, key: str, default=None):
    return cfg.get(key, default) if isinstance(cfg, dict) else default


def set_value(cfg: dict, key: str, value) -> dict:
    if not isinstance(cfg, dict):
        cfg = {}
    if value is None or value == '' or value is False:
        if key in cfg:
            del cfg[key]
    else:
        cfg[key] = value
    return cfg


def switch_mirror(mirror_id: str) -> dict:
    preset = MIRROR_PRESETS.get(mirror_id)
    if not preset:
        return {'success': False, 'message': f'未知镜像源: {mirror_id}'}

    cfg = read_config()

    if 'default_channels' in preset:
        cfg['default_channels'] = list(preset['default_channels'])
    else:
        channels = cfg.get('channels', []) if isinstance(cfg, dict) else []
        if 'defaults' not in channels:
            cfg['default_channels'] = []
        elif 'default_channels' in cfg:
            del cfg['default_channels']

    if 'custom_channels' in preset:
        cfg['custom_channels'] = dict(preset['custom_channels'])
    elif 'custom_channels' in cfg:
        del cfg['custom_channels']

    result = write_config(cfg)
    if result.get('success'):
        cache_info = clean_index_cache()
        result['cache_cleaned'] = cache_info.get('removed', 0)
    return result


def apply_full_config(mirror_id: str, channel_id: str) -> dict:
    mirror = MIRROR_PRESETS.get(mirror_id)
    channel = CHANNEL_PRESETS.get(channel_id)
    if not mirror:
        return {'success': False, 'message': f'未知镜像源: {mirror_id}'}
    if not channel:
        return {'success': False, 'message': f'未知频道组合: {channel_id}'}

    cfg = read_config()

    cfg['channels'] = list(channel['channels'])
    if 'channel_priority' in channel:
        cfg['channel_priority'] = channel['channel_priority']
    elif 'channel_priority' in cfg:
        del cfg['channel_priority']

    has_defaults = 'defaults' in channel.get('channels', [])
    if has_defaults and 'default_channels' in mirror:
        cfg['default_channels'] = list(mirror['default_channels'])
    elif 'default_channels' in cfg:
        del cfg['default_channels']

    if 'custom_channels' in mirror:
        cfg['custom_channels'] = dict(mirror['custom_channels'])
    elif 'custom_channels' in cfg:
        del cfg['custom_channels']

    result = write_config(cfg)
    if result.get('success'):
        cache_info = clean_index_cache()
        result['cache_cleaned'] = cache_info.get('removed', 0)
    return result


def backup() -> dict:
    src = get_condarc_path()
    if not src.exists():
        return {'success': False, 'message': '.condarc 不存在，无需备份'}
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = src.with_name(f'.condarc.bak.{ts}')
    try:
        shutil.copy2(src, dst)
        return {'success': True, 'message': f'已备份到 {dst.name}', 'backup_path': str(dst)}
    except OSError as e:
        return {'success': False, 'message': f'备份失败: {e}'}


def list_backups() -> list:
    user_dir = _home_conda_dir()
    if not user_dir.is_dir():
        return []
    backups = []
    for f in sorted(user_dir.glob('.condarc.bak.*')):
        backups.append({
            'name': f.name,
            'path': str(f),
            'mtime': datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            'size': f.stat().st_size,
        })
    backups.reverse()
    return backups


def restore(backup_path: str) -> dict:
    src = Path(backup_path)
    dst = get_condarc_path()
    if not src.exists():
        return {'success': False, 'message': f'备份文件不存在: {src}'}
    try:
        shutil.copy2(src, dst)
        cache_info = clean_index_cache()
        return {'success': True, 'message': '已恢复', 'cache_cleaned': cache_info.get('removed', 0)}
    except OSError as e:
        return {'success': False, 'message': f'恢复失败: {e}'}


def reset() -> dict:
    path = get_condarc_path()
    try:
        path.write_text('', encoding='utf-8')
        cache_info = clean_index_cache()
        return {'success': True, 'message': '已重置为空配置', 'cache_cleaned': cache_info.get('removed', 0)}
    except OSError as e:
        return {'success': False, 'message': f'重置失败: {e}'}


def delete_backup(backup_path: str) -> dict:
    src = Path(backup_path).resolve()
    expected_parent = _home_conda_dir().resolve()
    if src.parent != expected_parent or not src.name.startswith('.condarc.bak.'):
        return {'success': False, 'message': '非法的备份路径'}
    if not src.exists():
        return {'success': False, 'message': '备份文件不存在'}
    try:
        src.unlink()
        return {'success': True, 'message': f'已删除 {src.name}'}
    except OSError as e:
        return {'success': False, 'message': f'删除失败: {e}'}


def detect_compliance(cfg: dict) -> dict:
    channels = cfg.get('channels', []) if isinstance(cfg, dict) else []
    default_channels = cfg.get('default_channels', []) if isinstance(cfg, dict) else []
    custom_channels = cfg.get('custom_channels', {}) if isinstance(cfg, dict) else {}

    all_channels = list(channels) + list(default_channels)
    if isinstance(custom_channels, dict):
        all_channels.extend(custom_channels.keys())

    commercial = False
    commercial_channels = []
    for ch in all_channels:
        if not isinstance(ch, str):
            continue
        ch_lower = ch.lower().strip()
        is_commercial = False

        if ch_lower == 'defaults':
            is_commercial = True
        elif 'repo.anaconda.com' in ch_lower:
            is_commercial = True
        elif '/anaconda/pkgs/' in ch_lower or 'anaconda.com/pkgs/' in ch_lower:
            is_commercial = True

        if is_commercial:
            commercial = True
            commercial_channels.append(ch)

    conda_forge_only = False
    if not commercial:
        cf_count = sum(1 for ch in all_channels
                       if isinstance(ch, str) and 'conda-forge' in ch.lower())
        conda_forge_only = cf_count > 0

    return {
        'commercial_use': commercial,
        'commercial_channels': commercial_channels,
        'conda_forge_available': conda_forge_only,
        'message': (
            '⚠️ 当前包含 Anaconda 官方频道或其镜像，商业使用需购买许可。' if commercial
            else '✅ 当前无商业限制频道，可自由使用。'
        ),
    }


def get_raw_yaml() -> str:
    path = get_condarc_path()
    if not path.exists():
        return ''
    try:
        return path.read_text(encoding='utf-8')
    except OSError as e:
        logger.warning('get_raw_yaml 读取失败 %s: %s', path, e)
        return ''


def save_raw_yaml(text: str) -> dict:
    path = get_condarc_path()
    try:
        parsed = yaml.safe_load(text) if text.strip() else {}
        if parsed is not None and not isinstance(parsed, dict):
            return {'success': False, 'message': 'YAML 顶层必须是一个映射（字典）'}
    except yaml.YAMLError as e:
        return {'success': False, 'message': f'YAML 语法错误: {e}'}

    try:
        path.write_text(text if text.strip() else '', encoding='utf-8')
        return {'success': True, 'message': '保存成功'}
    except OSError as e:
        return {'success': False, 'message': f'写入失败: {e}'}


def _resolve_pkgs_dirs() -> list:
    result = []
    cfg = read_config()
    custom = cfg.get('pkgs_dirs') if isinstance(cfg, dict) else None
    if isinstance(custom, list):
        for d in custom:
            if isinstance(d, str) and d.strip():
                result.append(Path(d.strip()))
    default = _home_conda_dir() / 'pkgs'
    if default.exists() and default not in result:
        result.append(default)
    return result


def clean_index_cache() -> dict:
    pkgs_dirs = _resolve_pkgs_dirs()
    if not pkgs_dirs:
        return {'success': True, 'removed': 0, 'message': '未找到 pkgs 目录'}

    removed = 0
    errors = []
    for pkgs_dir in pkgs_dirs:
        cache_dir = pkgs_dir / 'cache'
        if not cache_dir.is_dir():
            continue
        for f in cache_dir.glob('*.json'):
            try:
                f.unlink()
                removed += 1
            except OSError as e:
                errors.append(f'{f.name}: {e}')
    return {
        'success': True,
        'removed': removed,
        'errors': errors,
        'message': f'已清理 {removed} 个索引缓存文件' + (f'（{len(errors)} 个失败）' if errors else ''),
    }