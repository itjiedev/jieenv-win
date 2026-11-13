"""
conda 应用服务

通过 jiecore 服务注册表向其他应用暴露功能，应用之间不直接 import。
在 apps.py 的 ready() 中注册服务，其他应用通过 call_service() 调用。

使用方式（其他应用调用本应用服务）：
    from jiecore.app_services import call_service
    result = call_service('conda', 'service_name', ...)
"""
import logging

logger = logging.getLogger(__name__)


# ── 服务函数示例（按需修改或新增）──────────────────────────

# def get_example_data(item_id):
#     """示例服务：根据 ID 获取数据"""
#     # ... 业务逻辑
#     return {'id': item_id, 'name': '示例数据'}
#
#
# def list_items(category=None):
#     """示例服务：获取列表"""
#     # ... 业务逻辑
#     return []
