"""
通用操作日志辅助模块

各应用通过本模块记录关键操作（安装/卸载/启停服务等），
首页"最近操作"板块从 OperationLog 表读取最近 N 条记录展示。

使用示例::

    from jiecore.op_log import log_operation

    # 记录成功操作
    log_operation('python_installer', '安装 Python 3.13.15')

    # 记录失败操作
    log_operation('dbmysql', '启动 MySQL 服务', success=False, detail='端口 3306 被占用')
"""
import logging

logger = logging.getLogger(__name__)


def log_operation(app_name: str, action: str, success: bool = True, detail: str = ''):
    """记录一条操作日志。

    参数:
        app_name: 应用标识（与 app_meta.py 中的 app_name 一致）
        action:   操作描述（如"安装 Python 3.13.15"）
        success:  是否成功，默认 True
        detail:   补充信息（可选）

    写入失败只记日志不抛异常，避免影响主流程。
    """
    try:
        from .models import OperationLog
        OperationLog.objects.create(
            app_name=app_name,
            action=action,
            success=success,
            detail=detail,
        )
    except Exception as e:
        logger.warning('写入操作日志失败: %s', e)


def get_recent_operations(limit: int = 20) -> list:
    """获取最近 N 条操作日志，返回 dict 列表。

    字段: id, app_name, action, success, detail, created_at
    """
    try:
        from .models import OperationLog
        qs = OperationLog.objects.order_by('-created_at')[:limit]
        return [
            {
                'id': r.id,
                'app_name': r.app_name,
                'action': r.action,
                'success': r.success,
                'detail': r.detail,
                'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            }
            for r in qs
        ]
    except Exception as e:
        logger.warning('读取操作日志失败: %s', e)
        return []


def cleanup_old_operations(keep: int = 1000):
    """清理超出保留数量的旧记录，防止表无限增长。"""
    try:
        from .models import OperationLog
        total = OperationLog.objects.count()
        if total <= keep:
            return
        # 取第 keep 条的 created_at 作为截断点
        cutoff = OperationLog.objects.order_by('-created_at').values_list(
            'created_at', flat=True
        )[keep - 1]
        deleted, _ = OperationLog.objects.filter(created_at__lt=cutoff).delete()
        if deleted:
            logger.info('清理操作日志: 删除 %d 条旧记录', deleted)
    except Exception as e:
        logger.warning('清理操作日志失败: %s', e)
