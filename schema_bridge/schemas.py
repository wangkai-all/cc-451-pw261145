"""v1 / v2 事件结构定义、字段文档与双向映射表。

事件以订单领域事件（OrderEvent）为载体：

v1（legacy，扁平）::

    {
      "event_id": "evt-1", "event_type": "ORDER_CREATED",
      "occurred_at": "2026-11-20T10:00:00Z",
      "order_id": "ord-100", "customer_id": "cus-7",
      "amount": 1999, "currency": "USD", "status": "created",
      "items": [{"sku": "sku-1", "name": "Keyboard", "qty": 2, "unit_price": 5000}],
      "remark": "gift wrap"
    }

v2（信封 + 嵌套 money 对象）::

    {
      "schema_version": "v2",
      "event": {
        "id": "evt-1", "type": "ORDER_CREATED",
        "occurred_at": "2026-11-20T10:00:00Z",
        "data": {"order": {
          "id": "ord-100", "status": "created",
          "money": {"amount_cents": 1999, "currency": "USD"},
          "customer": {"id": "cus-7"},
          "items": [{"sku": "sku-1", "name": "Keyboard", "quantity": 2,
                     "unit_money": {"amount_cents": 5000, "currency": "USD"}}],
          "note": "gift wrap"
        }}
      }
    }
"""

from __future__ import annotations

V1 = "v1"
V2 = "v2"
VERSIONS = (V1, V2)

# ---------------------------------------------------------------------------
# 字段文档：(路径, 类型, 是否必填, 说明) —— 驱动 /diff 页面的结构对比
# ---------------------------------------------------------------------------

V1_FIELDS = [
    ("event_id", "string", True, "事件唯一标识"),
    ("event_type", "string", True, "事件类型，如 ORDER_CREATED"),
    ("occurred_at", "string(ISO8601)", True, "事件发生时间"),
    ("order_id", "string", True, "订单号"),
    ("customer_id", "string", True, "客户号"),
    ("amount", "int(分)", True, "订单总金额，单位分，非负整数"),
    ("currency", "string(3)", True, "ISO 三位货币码"),
    ("status", "string", True, "订单状态"),
    ("remark", "string", False, "备注，可选"),
    ("items", "array", True, "订单明细，至少 1 条"),
    ("items[].sku", "string", True, "商品 SKU"),
    ("items[].name", "string", True, "商品名称"),
    ("items[].qty", "int", True, "数量，正整数"),
    ("items[].unit_price", "int(分)", True, "单品单价，单位分，非负整数"),
]

V2_FIELDS = [
    ("schema_version", "string", True, '结构版本常量，固定为 "v2"'),
    ("event.id", "string", True, "事件唯一标识"),
    ("event.type", "string", True, "事件类型"),
    ("event.occurred_at", "string(ISO8601)", True, "事件发生时间"),
    ("event.data.order.id", "string", True, "订单号"),
    ("event.data.order.status", "string", True, "订单状态"),
    ("event.data.order.money.amount_cents", "int(分)", True, "订单总金额，单位分"),
    ("event.data.order.money.currency", "string(3)", True, "ISO 三位货币码"),
    ("event.data.order.customer.id", "string", False, "客户号（v2 中可选）"),
    ("event.data.order.note", "string", False, "备注，由 v1.remark 重命名而来"),
    ("event.data.order.items", "array", True, "订单明细，至少 1 条"),
    ("event.data.order.items[].sku", "string", True, "商品 SKU"),
    ("event.data.order.items[].name", "string", True, "商品名称"),
    ("event.data.order.items[].quantity", "int", True, "数量，正整数，由 qty 重命名"),
    ("event.data.order.items[].unit_money.amount_cents", "int(分)", True, "单品单价，单位分"),
    ("event.data.order.items[].unit_money.currency", "string(3)", True, "单品货币，继承订单头 currency"),
]

# ---------------------------------------------------------------------------
# v1 -> v2 字段映射：(v1 路径, v2 路径, 变化类型, 说明)
# 变化类型：重命名移动 / 移入嵌套 / 合并对象 / 重命名 / 新增 / 原样移动
# ---------------------------------------------------------------------------

MAPPINGS_V1_TO_V2 = [
    ("event_id", "event.id", "重命名移动", "重命名为 id 并移入 event 信封"),
    ("event_type", "event.type", "重命名移动", "重命名为 type 并移入 event 信封"),
    ("occurred_at", "event.occurred_at", "原样移动", "移入 event 信封，字段名不变"),
    ("order_id", "event.data.order.id", "移入嵌套", "移入 event.data.order"),
    ("status", "event.data.order.status", "移入嵌套", "移入 event.data.order"),
    ("customer_id", "event.data.order.customer.id", "移入嵌套", "包装为 customer 对象"),
    ("amount", "event.data.order.money.amount_cents", "合并对象", "与 currency 合并为 money 对象"),
    ("currency", "event.data.order.money.currency", "合并对象", "与 amount 合并为 money 对象"),
    ("remark", "event.data.order.note", "重命名", "可选字段，重命名为 note"),
    ("items[].sku", "event.data.order.items[].sku", "移入嵌套", "随 items 移入 order"),
    ("items[].name", "event.data.order.items[].name", "移入嵌套", "随 items 移入 order"),
    ("items[].qty", "event.data.order.items[].quantity", "重命名", "qty 重命名为 quantity"),
    ("items[].unit_price",
     "event.data.order.items[].unit_money.amount_cents",
     "合并对象", "包装为 unit_money 对象并改名"),
    ("(无)", "schema_version", "新增", 'v2 信封版本号，常量 "v2"'),
    ("(无)", "event.data.order.items[].unit_money.currency",
     "新增", "v2 新增；转换时继承订单头 currency"),
]

# v2 -> v1 回放时被丢弃的 v2 字段
DROPPED_WHEN_V2_TO_V1 = [
    "schema_version（信封版本号，v1 无此概念）",
    "event.data.order.items[].unit_money.currency（拍平后复用订单头 currency）",
]


def schema_diff() -> dict:
    """返回结构差异视图，供 /diff 页面与 /api/diff 使用。"""
    v2_only = [v2 for v1, v2, kind, _ in MAPPINGS_V1_TO_V2 if kind == "新增"]
    return {
        "direction": "v1 -> v2（v2 -> v1 为反向映射）",
        "v1_fields": V1_FIELDS,
        "v2_fields": V2_FIELDS,
        "mappings": MAPPINGS_V1_TO_V2,
        "v2_only_fields": v2_only,
        "dropped_on_reverse": DROPPED_WHEN_V2_TO_V1,
        "summary": (
            "v2 将扁平字段收进 event/data/order 信封，金额类字段合并为 money 对象，"
            "qty 重命名为 quantity、remark 重命名为 note，并新增 schema_version。"
        ),
    }
