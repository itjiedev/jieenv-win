"""
快照系统 - 在项目目录侧读写 .jieenv/project.json

用途:
  1. engine.py 创建成功后写快照（S-1）
  2. 详情页渲染时读快照（Phase B）
  3. 未来 scanner.py 导入时校验快照（S-4）
  4. rebuild_from_snapshot() 从零重建环境

快照 schema (v2):
  {
    "schema_version": 2,
    "snapshot_version": "jieenv-1.41.0",
    "project_name": "xxx",
    "template_id": "xxx",              ← 仅供追溯，恢复时不依赖
    "project_dir": "D:\\workspace\\xxx",
    "bindings": [
      {
        "provider_key": "runtime.python",
        "binding_type": "provider",    ← v2 新增，区分 provider / action
        "label": "Python 运行时",
        "config_snapshot": { ... },
        "config_missing_keys": [],
        "depends_on": []
      },
      {
        "provider_key": "runtime.python",
        "binding_type": "action",      ← v2 新增
        "action_type": "lang_packages", ← v2 新增
        "invoke_action": "install_packages", ← v2 新增，恢复时直接调
        "label": "安装 Django 包",
        "config_snapshot": { ... },    ← v2: action 全量保留，跳过 schema 过滤
        "action_result": {...}         ← v2 新增，执行后结果
      }
    ],
    "env_hints": { "runtime.python": {...} },
    "created_at": "2026-10-24T15:30:00"
  }

自包含原则（v2 铁律）:
  - 快照里存的 = 恢复所需的全部数据
  - 恢复时不查 templates.py、不查 action_registry、不做任何推断
  - invoke_action 是什么就调什么，config_snapshot 全量原样传

敏感字段处理: 仅 provider binding 走 schema 过滤
  - provider binding 按 config_schema sensitive 标记过滤（同 v1）
  - action binding 全量保留（params 本身就是 invoke_action 需要的完整参数）
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = '.jieenv'
SNAPSHOT_FILE = 'project.json'

def _is_preserved_key(key):
    """运行时自动生成、必须保留的字段
    
    规则:
      - 以下划线开头（_action_type, _skipped_packages, _fallback_used 等引擎追踪字段）
      - 以 _version 结尾（python_exe_version, instance_id_version 等贡献应用精确版本号）
      - 以 _sub 结尾（venv_dir_sub 等前端 dir_with_base widget 附加的子目录名）
    """
    if key.startswith('_'):
        return True
    if key.endswith('_version'):
        return True
    if key.endswith('_sub'):
        return True
    return False


def _get_platform_version():
    try:
        from jiecore.version_manager import _get_platform_version as _v
        return _v() or 'unknown'
    except Exception:
        return 'unknown'


def _collect_all_schema_keys(provider_def):
    if not provider_def:
        return {}
    schema_keys = {}
    for section in ('lifecycle', 'capabilities'):
        for action_name, action_def in (provider_def.get(section) or {}).items():
            for field in (action_def.get('config_schema') or []):
                key = field.get('key', '')
                if key:
                    schema_keys[key] = bool(field.get('sensitive', False))
    return schema_keys


def _sanitize_binding_config(config, provider_def=None):
    """
    按 provider config_schema 过滤敏感字段
    
    规则:
      - _IDENTITY_KEYS 里的 key → 保留
      - *_version 结尾的运行时版本字段 → 保留（引擎执行时自动生成）
      - schema 里声明了且没标 sensitive → 存进 config_snapshot
      - schema 里标了 sensitive → 不存 → 记到 config_missing_keys
      - schema 里没声明、也不是运行时版本字段 → 不存
    
    schema 扫描范围: lifecycle 全部 action + capabilities 全部 action（后置动作也要匹配到）
    """
    if not config:
        return {}, []

    if provider_def is None:
        safe_config = {}
        for key, value in config.items():
            if _is_preserved_key(key):
                safe_config[key] = value
        return safe_config, []

    schema_keys = _collect_all_schema_keys(provider_def)

    safe_config = {}
    missing_keys = []
    for key, value in config.items():
        if _is_preserved_key(key):
            safe_config[key] = value
        elif key in schema_keys:
            if schema_keys[key]:
                missing_keys.append(key)
            else:
                safe_config[key] = value
        else:
            missing_keys.append(key)

    return safe_config, missing_keys


def write_snapshot(project_dir, project_record, env_hints=None):
    """
    在 {project_dir}/.jieenv/project.json 写入快照
    
    参数:
      project_dir: str, 项目目录绝对路径
      project_record: dict, storage 里的项目记录（含 bindings）
      env_hints: dict, provider_key → env_hints data 的映射（可选）
    
    返回: bool, 是否写入成功
    """
    if not project_dir:
        logger.warning('write_snapshot: project_dir 为空')
        return False

    snap_dir = os.path.join(project_dir, SNAPSHOT_DIR)
    try:
        os.makedirs(snap_dir, exist_ok=True)
    except OSError as e:
        logger.warning('write_snapshot: 创建目录失败 %s: %s', snap_dir, e)
        return False

    bindings = project_record.get('bindings', []) or []

    snapshot_bindings = []
    for b in bindings:
        pk = b.get('provider_key') or b.get('actual_provider_key', '')
        config = b.get('config', {}) or {}
        binding_type = b.get('binding_type', 'provider')

        if binding_type == 'action':
            action_result = b.get('created_result') or {}
            snapshot_bindings.append({
                'provider_key': pk,
                'binding_type': 'action',
                'action_type': b.get('action_type', ''),
                'invoke_action': b.get('invoke_action', ''),
                'label': b.get('label', '') or pk,
                'config_snapshot': config,
                'config_missing_keys': [],
                'action_result': action_result,
            })
        else:
            provider_def = None
            try:
                from jiecore.provider_catalog import get_provider
                provider_def = get_provider(pk)
            except Exception:
                pass

            safe_config, missing_keys = _sanitize_binding_config(config, provider_def)

            snapshot_bindings.append({
                'provider_key': pk,
                'binding_type': 'provider',
                'label': b.get('label', '') or pk,
                'config_snapshot': safe_config,
                'config_missing_keys': missing_keys,
                'depends_on': b.get('depends_on', []),
            })

    snapshot = {
        'schema_version': 2,
        'snapshot_version': f'jieenv-{_get_platform_version()}',
        'project_name': project_record.get('name', ''),
        'template_id': project_record.get('template_id', ''),
        'project_dir': project_dir,
        'bindings': snapshot_bindings,
        'env_hints': env_hints or {},
        'created_at': _now_iso(),
    }

    snap_path = os.path.join(snap_dir, SNAPSHOT_FILE)
    try:
        with open(snap_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
        logger.info('快照写入: %s', snap_path)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.warning('write_snapshot: 写入失败 %s: %s', snap_path, e)
        return False


def read_snapshot(project_dir):
    """
    读取 {project_dir}/.jieenv/project.json
    
    返回: dict or None（文件不存在或损坏时返回 None）
    """
    if not project_dir:
        return None

    snap_path = os.path.join(project_dir, SNAPSHOT_DIR, SNAPSHOT_FILE)
    if not os.path.isfile(snap_path):
        return None

    try:
        with open(snap_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning('read_snapshot: 读取失败 %s: %s', snap_path, e)
        return None


def snapshot_exists(project_dir):
    """检查快照文件是否存在"""
    if not project_dir:
        return False
    snap_path = os.path.join(project_dir, SNAPSHOT_DIR, SNAPSHOT_FILE)
    return os.path.isfile(snap_path)


def _now_iso():
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def _prepare_rebuild_config(config, project_name, project_dir, snap_project_dir=None):
    """rebuild 前预处理 config_snapshot

    处理链:
      1. 占位符替换（{PROJECT_*} → 目标目录实际值）
      2. 源项目路径 → 目标项目路径替换（快照存的是源目录绝对路径，rebuild 到新目录时要跟着变）
      3. packages list → string
    """
    import os as _os

    sub_dir = _os.path.basename(project_dir) if project_dir else ''
    parent_dir = _os.path.dirname(project_dir) if project_dir else ''
    snap_dir = snap_project_dir or ''

    def _replace(obj):
        if isinstance(obj, str):
            s = obj
            s = (s
                 .replace('{PROJECT_NAME}', project_name or '')
                 .replace('{PROJECT_DIR}', project_dir or '')
                 .replace('{PROJECT_SUB_DIR}', sub_dir)
                 .replace('{PROJECT_PARENT_DIR}', parent_dir))
            if snap_dir and snap_dir != project_dir and snap_dir in s:
                s = s.replace(snap_dir, project_dir)
                s = s.replace(snap_dir.replace('\\', '/'), project_dir.replace('\\', '/'))
            return s
        if isinstance(obj, dict):
            return {k: _replace(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_replace(v) for v in obj]
        return obj

    result = _replace(dict(config or {}))

    pkgs = result.get('packages')
    if isinstance(pkgs, list):
        result['packages'] = '\n'.join(str(p) for p in pkgs if p)

    return result


def rebuild_from_snapshot(project_dir, snap=None):
    """
    从快照零依赖重建环境

    自包含铁律:
      - 不查 templates.py、不查 action_registry、不做任何推断
      - invoke_action 是什么就调什么，config_snapshot 全量原样传
      - provider 未安装时跳过并记录 warning（合理降级）

    参数:
      project_dir: str, 目标项目目录（快照里的 project_dir 可能不同，以参数为准）
      snap: dict or None, 已读好的快照 dict（None 则从 project_dir/.jieenv/project.json 读）

    返回:
      dict: {
        'success': bool,
        'project_name': str,
        'project_dir': str,
        'results': [
          {'provider_key': str, 'binding_type': 'provider'|'action', 'label': str,
           'success': bool, 'error': str|None, 'skipped': bool}
        ],
        'skipped_providers': [str],
      }
    """
    if snap is None:
        snap = read_snapshot(project_dir)
    if not snap:
        return {'success': False, 'error': '快照不存在或无法读取', 'results': [], 'skipped_providers': []}

    project_name = snap.get('project_name', '')
    snap_project_dir = snap.get('project_dir', '') or project_dir
    bindings = snap.get('bindings', [])

    from jiecore.provider_catalog import get_provider, invoke_provider

    results = []
    skipped_providers = []

    for b in bindings:
        pk = b.get('provider_key', '')
        binding_type = b.get('binding_type', 'provider')
        label = b.get('label', '') or pk
        config = _prepare_rebuild_config(
            b.get('config_snapshot', {}) or {},
            project_name, project_dir,
            snap_project_dir=snap_project_dir,
        )

        provider_def = get_provider(pk)
        if not provider_def:
            from jiecore.provider_catalog import find_provider_app
            owner_app = find_provider_app(pk)
            if owner_app:
                from jiecore.app_manager import AppManager
                if owner_app in AppManager.get_all_apps():
                    err_msg = f'provider 已禁用（应用 {owner_app} 未启用）'
                else:
                    err_msg = 'provider 未安装或不存在'
            else:
                err_msg = 'provider 未安装或不存在'
            skipped_providers.append(pk)
            results.append({
                'provider_key': pk,
                'binding_type': binding_type,
                'label': label,
                'success': False,
                'skipped': True,
                'error': err_msg,
            })
            logger.warning('rebuild: 跳过 %s（%s）', pk, err_msg)
            continue

        if binding_type == 'provider':
            capability = 'create'
        elif binding_type == 'action':
            capability = b.get('invoke_action', '')
            if not capability:
                results.append({
                    'provider_key': pk,
                    'binding_type': binding_type,
                    'label': label,
                    'success': False,
                    'skipped': True,
                    'error': 'invoke_action 缺失',
                })
                logger.warning('rebuild: %s invoke_action 缺失，跳过', label)
                continue
        else:
            results.append({
                'provider_key': pk,
                'binding_type': binding_type,
                'label': label,
                'success': False,
                'skipped': True,
                'error': '未知 binding_type: %s' % binding_type,
            })
            continue

        try:
            result = invoke_provider(
                pk, capability,
                project_name=project_name,
                project_dir=project_dir,
                config=config,
            )
            results.append({
                'provider_key': pk,
                'binding_type': binding_type,
                'label': label,
                'success': getattr(result, 'success', bool(result)),
                'skipped': False,
                'error': getattr(result, 'error_msg', None) if not getattr(result, 'success', True) else None,
            })
            status = 'ok' if getattr(result, 'success', bool(result)) else 'fail'
            logger.info('rebuild [%s]: %s → %s.%s', status, label, pk, capability)
        except Exception as e:
            logger.exception('rebuild 异常: %s', label)
            results.append({
                'provider_key': pk,
                'binding_type': binding_type,
                'label': label,
                'success': False,
                'skipped': False,
                'error': str(e),
            })

    all_ok = all(r['success'] or r['skipped'] for r in results)
    return {
        'success': all_ok,
        'project_name': project_name,
        'project_dir': project_dir,
        'results': results,
        'skipped_providers': skipped_providers,
    }