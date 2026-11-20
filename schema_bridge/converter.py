"""v1 <-> v2 双向转换器与结构版本探测。

转换是纯函数、确定性的：同一输入永远产出同一输出，这是幂等性的基础。
校验失败时抛出 :class:`ConversionError`，错误中带全部字段级错误，
由上层保留原文并写入隔离队列。
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .schemas import V1, V2, VERSIONS

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# 宽松 ISO-8601 校验：日期 T 时间，可选毫秒与 Z/时区偏移
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$"
)


class ConversionError(Exception):
    """转换/校验失败；:attr:`errors` 收集全部字段级错误。"""

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or [message]

    def to_dict(self) -> dict:
        return {"message": str(self), "errors": self.errors}


class InvalidEvent(ConversionError):
    """事件原文无法被识别为任何受支持版本（连 event_id 都无法提取）。"""


# ---------------------------------------------------------------------------
# 基础校验工具
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    # bool 是 int 的子类，金额/数量不允许布尔
    return isinstance(value, int) and not isinstance(value, bool)


def _check_type(holder: Any, key: str, expected: str, errors: list[str]) -> bool:
    """校验 holder[key] 是否符合 expected 描述；失败时记录错误。"""
    if key not in holder:
        return False  # 必填性由调用方单独处理
    value = holder[key]
    path = f"{key}"
    ok = True
    if expected == "string":
        ok = isinstance(value, str) and bool(value.strip())
    elif expected == "int":
        ok = _is_int(value)
    elif expected == "array":
        ok = isinstance(value, list)
    elif expected == "object":
        ok = isinstance(value, dict)
    if not ok:
        errors.append(f"字段 {path} 类型错误：期望 {expected}，实际 {type(value).__name__}")
    return ok


def _require(holder: dict, key: str, expected: str, errors: list[str]) -> None:
    if key not in holder:
        errors.append(f"字段 {key} 缺失（必填）")
        return
    _check_type(holder, key, expected, errors)


def _check_iso8601(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _ISO_RE.match(value):
        errors.append(f"字段 {path} 不是合法的 ISO-8601 时间：{value!r}")


def _check_currency(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _CURRENCY_RE.match(value):
        errors.append(f"字段 {path} 不是合法的 3 位大写货币码：{value!r}")


def _check_money(amount: Any, currency: Any, amount_path: str,
                 currency_path: str, errors: list[str]) -> None:
    if _is_int(amount) and amount < 0:
        errors.append(f"字段 {amount_path} 必须是非负整数（单位：分），实际 {amount}")
    _check_currency(currency, currency_path, errors)


# ---------------------------------------------------------------------------
# 版本探测
# ---------------------------------------------------------------------------

def detect_version(raw: Any) -> str | None:
    """识别事件结构版本。

    - v2 信封带 ``schema_version == "v2"``
    - 含 v1 标志性扁平字段（event_id 等）识别为 v1
    - 无法识别返回 None
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") == V2 and isinstance(raw.get("event"), dict):
        return V2
    if "event_id" in raw or "event_type" in raw or "order_id" in raw:
        return V1
    return None


def extract_event_id(raw: Any) -> str | None:
    """从任意结构的原文中尽力提取 event_id，供隔离与幂等记录使用。"""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("event_id"), str) and raw["event_id"].strip():
        return raw["event_id"]
    event = raw.get("event")
    if isinstance(event, dict):
        value = event.get("id")
        if isinstance(value, str) and value.strip():
            return value
    return None


# ---------------------------------------------------------------------------
# v1 校验 + v1 -> v2
# ---------------------------------------------------------------------------

def validate_v1(raw: dict, errors: list[str]) -> None:
    for key, expected in [
        ("event_id", "string"), ("event_type", "string"),
        ("occurred_at", "string"), ("order_id", "string"),
        ("customer_id", "string"), ("amount", "int"),
        ("currency", "string"), ("status", "string"),
    ]:
        _require(raw, key, expected, errors)
    if "occurred_at" in raw:
        _check_iso8601(raw["occurred_at"], "occurred_at", errors)
    if "currency" in raw:
        _check_currency(raw["currency"], "currency", errors)
    if "amount" in raw:
        _check_money(raw["amount"], raw.get("currency"),
                     "amount", "currency", errors)
    if "remark" in raw and not isinstance(raw["remark"], str):
        errors.append("字段 remark 类型错误：期望 string")

    if "items" not in raw:
        errors.append("字段 items 缺失（必填）")
    elif not isinstance(raw["items"], list) or not raw["items"]:
        errors.append("字段 items 必须是非空数组")
    else:
        for index, item in enumerate(raw["items"]):
            prefix = f"items[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} 必须是对象")
                continue
            for key, expected in [("sku", "string"), ("name", "string"),
                                  ("qty", "int"), ("unit_price", "int")]:
                if key not in item:
                    errors.append(f"字段 {prefix}.{key} 缺失（必填）")
                elif not _check_type(item, key, expected, errors):
                    pass
            if "qty" in item and _is_int(item["qty"]) and item["qty"] <= 0:
                errors.append(f"{prefix}.qty 必须是正整数，实际 {item['qty']}")
            if "unit_price" in item:
                _check_money(item["unit_price"], raw.get("currency"),
                             f"{prefix}.unit_price", "currency", errors)


def v1_to_v2(raw: dict) -> dict:
    errors: list[str] = []
    validate_v1(raw, errors)
    if errors:
        raise ConversionError("v1 事件校验失败，无法转换为 v2", errors)

    items = [
        {
            "sku": item["sku"],
            "name": item["name"],
            "quantity": item["qty"],
            "unit_money": {
                "amount_cents": item["unit_price"],
                "currency": raw["currency"],
            },
        }
        for item in raw["items"]
    ]
    order: dict[str, Any] = {
        "id": raw["order_id"],
        "status": raw["status"],
        "money": {"amount_cents": raw["amount"], "currency": raw["currency"]},
        "customer": {"id": raw["customer_id"]},
        "items": items,
    }
    if "remark" in raw:
        order["note"] = raw["remark"]

    return {
        "schema_version": V2,
        "event": {
            "id": raw["event_id"],
            "type": raw["event_type"],
            "occurred_at": raw["occurred_at"],
            "data": {"order": order},
        },
    }


# ---------------------------------------------------------------------------
# v2 校验 + v2 -> v1
# ---------------------------------------------------------------------------

def validate_v2(raw: dict, errors: list[str]) -> None:
    if raw.get("schema_version") != V2:
        errors.append('字段 schema_version 必须等于 "v2"')
    if "event" not in raw:
        errors.append("字段 event 缺失（必填）")
        return
    if not isinstance(raw["event"], dict):
        errors.append("字段 event 类型错误：期望 object")
        return
    event = raw["event"]
    for key, expected in [("id", "string"), ("type", "string"),
                          ("occurred_at", "string")]:
        _require(event, key, expected, errors)
    if "occurred_at" in event:
        _check_iso8601(event["occurred_at"], "event.occurred_at", errors)

    data = event.get("data")
    if not isinstance(data, dict):
        errors.append("字段 event.data 缺失或类型错误：期望 object")
        return
    order = data.get("order")
    if not isinstance(order, dict):
        errors.append("字段 event.data.order 缺失或类型错误：期望 object")
        return

    for key, expected in [("id", "string"), ("status", "string"),
                          ("items", "array")]:
        _require(order, key, expected, errors)

    money = order.get("money")
    if not isinstance(money, dict):
        errors.append("字段 event.data.order.money 缺失或类型错误：期望 object")
    else:
        if "amount_cents" not in money:
            errors.append("字段 money.amount_cents 缺失（必填）")
        elif not _check_type(money, "amount_cents", "int", errors):
            pass
        elif money["amount_cents"] < 0:
            errors.append(
                f"money.amount_cents 必须非负，实际 {money['amount_cents']}")
        if "currency" not in money:
            errors.append("字段 money.currency 缺失（必填）")
        else:
            _check_currency(money["currency"], "money.currency", errors)

    if "customer" in order:
        customer = order["customer"]
        if not isinstance(customer, dict) or not isinstance(
                customer.get("id"), str) or not customer["id"].strip():
            errors.append("字段 order.customer.id 缺失或不是非空字符串")

    if "note" in order and not isinstance(order["note"], str):
        errors.append("字段 order.note 类型错误：期望 string")

    if isinstance(order.get("items"), list):
        if not order["items"]:
            errors.append("字段 order.items 必须是非空数组")
        for index, item in enumerate(order["items"]):
            prefix = f"order.items[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} 必须是对象")
                continue
            for key, expected in [("sku", "string"), ("name", "string"),
                                  ("quantity", "int")]:
                if key not in item:
                    errors.append(f"字段 {prefix}.{key} 缺失（必填）")
                else:
                    _check_type(item, key, expected, errors)
            if "quantity" in item and _is_int(item["quantity"]) \
                    and item["quantity"] <= 0:
                errors.append(
                    f"{prefix}.quantity 必须是正整数，实际 {item['quantity']}")
            unit_money = item.get("unit_money")
            if not isinstance(unit_money, dict):
                errors.append(f"{prefix}.unit_money 缺失或类型错误：期望 object")
            else:
                if "amount_cents" not in unit_money:
                    errors.append(f"{prefix}.unit_money.amount_cents 缺失（必填）")
                elif not _check_type(unit_money, "amount_cents", "int", errors):
                    pass
                elif unit_money["amount_cents"] < 0:
                    errors.append(
                        f"{prefix}.unit_money.amount_cents 必须非负")
                if "currency" in unit_money:
                    _check_currency(unit_money["currency"],
                                    f"{prefix}.unit_money.currency", errors)


def v2_to_v1(raw: dict) -> dict:
    errors: list[str] = []
    validate_v2(raw, errors)
    if errors:
        raise ConversionError("v2 事件校验失败，无法转换为 v1", errors)

    order = raw["event"]["data"]["order"]
    money = order["money"]
    currency = money["currency"]
    items = [
        {
            "sku": item["sku"],
            "name": item["name"],
            "qty": item["quantity"],
            "unit_price": item["unit_money"]["amount_cents"],
        }
        for item in order["items"]
    ]
    result: dict[str, Any] = {
        "event_id": raw["event"]["id"],
        "event_type": raw["event"]["type"],
        "occurred_at": raw["event"]["occurred_at"],
        "order_id": order["id"],
        # v2 中 customer 可选；v1 必填，缺失时回退为空串以便隔离/显式表达
        "customer_id": order.get("customer", {}).get("id", ""),
        "amount": money["amount_cents"],
        "currency": currency,
        "status": order["status"],
        "items": items,
    }
    if "note" in order:
        result["remark"] = order["note"]
    # customer 缺失属于语义降级：不阻断转换，但在结果上留痕
    if "customer" not in order:
        result["_degraded"] = "v2 缺少 customer.id，v1 customer_id 置空"
    return result


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def convert(raw: Any, target_version: str) -> dict:
    """把任意受支持版本的事件转换到 ``target_version``。

    同版本转换：校验后返回深拷贝，保证调用方拿到独立对象。
    无法识别结构时抛 :class:`InvalidEvent`；校验失败抛 :class:`ConversionError`。
    """
    if target_version not in VERSIONS:
        raise ConversionError(f"不支持的目标版本：{target_version!r}")
    source_version = detect_version(raw)
    if source_version is None:
        raise InvalidEvent("无法识别事件结构：既不是 v1 扁平结构，也不是 v2 信封结构")
    if source_version == V1 and target_version == V2:
        return v1_to_v2(raw)
    if source_version == V2 and target_version == V1:
        return v2_to_v1(raw)
    # 同版本：先校验再返回拷贝
    errors: list[str] = []
    if source_version == V1:
        validate_v1(raw, errors)
    else:
        validate_v2(raw, errors)
    if errors:
        raise ConversionError(f"{source_version} 事件校验失败", errors)
    return deepcopy(raw)
