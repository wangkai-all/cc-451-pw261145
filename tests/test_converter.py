"""转换器单元测试：双向转换、校验与版本探测。"""

import unittest

from schema_bridge.converter import (ConversionError, InvalidEvent, convert,
                                     detect_version, extract_event_id,
                                     v1_to_v2, v2_to_v1)
from schema_bridge.schemas import V1, V2

V1_EVENT = {
    "event_id": "evt-1", "event_type": "ORDER_CREATED",
    "occurred_at": "2026-11-20T10:00:00Z",
    "order_id": "ord-1", "customer_id": "cus-1",
    "amount": 1999, "currency": "USD", "status": "created",
    "remark": "gift",
    "items": [{"sku": "sku-1", "name": "Keyboard", "qty": 2,
               "unit_price": 5000}],
}


class DetectTests(unittest.TestCase):
    def test_detect_versions(self):
        self.assertEqual(detect_version(V1_EVENT), V1)
        self.assertEqual(detect_version(v1_to_v2(V1_EVENT)), V2)
        self.assertIsNone(detect_version({"foo": 1}))
        self.assertIsNone(detect_version("not-a-dict"))

    def test_extract_event_id(self):
        self.assertEqual(extract_event_id(V1_EVENT), "evt-1")
        self.assertEqual(extract_event_id(v1_to_v2(V1_EVENT)), "evt-1")
        self.assertIsNone(extract_event_id({"x": 1}))


class ConvertRoundTripTests(unittest.TestCase):
    def test_v1_to_v2_envelope(self):
        v2 = v1_to_v2(V1_EVENT)
        self.assertEqual(v2["schema_version"], "v2")
        order = v2["event"]["data"]["order"]
        self.assertEqual(order["id"], "ord-1")
        self.assertEqual(order["money"],
                         {"amount_cents": 1999, "currency": "USD"})
        self.assertEqual(order["customer"], {"id": "cus-1"})
        self.assertEqual(order["note"], "gift")
        item = order["items"][0]
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["unit_money"],
                         {"amount_cents": 5000, "currency": "USD"})

    def test_round_trip_v1_v2_v1(self):
        v2 = v1_to_v2(V1_EVENT)
        back = v2_to_v1(v2)
        back.pop("_degraded", None)
        self.assertEqual(back, V1_EVENT)

    def test_convert_is_deterministic(self):
        a = v1_to_v2(V1_EVENT)
        b = v1_to_v2(V1_EVENT)
        self.assertEqual(a, b)
        # 同版本转换返回独立拷贝，修改不影响输入
        same = convert(V1_EVENT, V1)
        same["amount"] = 1
        self.assertEqual(V1_EVENT["amount"], 1999)

    def test_v2_optional_customer_degrades(self):
        v2 = v1_to_v2(V1_EVENT)
        del v2["event"]["data"]["order"]["customer"]
        v1 = v2_to_v1(v2)
        self.assertEqual(v1["customer_id"], "")
        self.assertIn("_degraded", v1)

    def test_invalid_target_version(self):
        with self.assertRaises(ConversionError):
            convert(V1_EVENT, "v9")

    def test_unrecognized_raises_invalid_event(self):
        with self.assertRaises(InvalidEvent):
            convert({"unknown": True}, V2)


class ValidationTests(unittest.TestCase):
    def _assert_error_contains(self, payload, direction, needle):
        with self.assertRaises(ConversionError) as ctx:
            direction(payload)
        self.assertTrue(
            any(needle in e for e in ctx.exception.errors),
            msg=f"{needle} 未出现在 {ctx.exception.errors}")

    def test_v1_bad_amount_currency_qty(self):
        bad = {
            **V1_EVENT,
            "amount": -10, "currency": "usd",
            "items": [{"sku": "s", "name": "n", "qty": 0, "unit_price": 10}],
        }
        with self.assertRaises(ConversionError) as ctx:
            v1_to_v2(bad)
        joined = " ".join(ctx.exception.errors)
        for needle in ("非负", "货币码", "正整数"):
            self.assertIn(needle, joined)

    def test_v1_missing_required_and_empty_items(self):
        payload = dict(V1_EVENT)
        del payload["order_id"]
        payload["items"] = []
        with self.assertRaises(ConversionError) as ctx:
            v1_to_v2(payload)
        joined = " ".join(ctx.exception.errors)
        self.assertIn("order_id", joined)
        self.assertIn("非空数组", joined)

    def test_v1_bad_timestamp_and_bool_amount(self):
        payload = {**V1_EVENT, "occurred_at": "2026/11/20", "amount": True}
        with self.assertRaises(ConversionError) as ctx:
            v1_to_v2(payload)
        joined = " ".join(ctx.exception.errors)
        self.assertIn("ISO-8601", joined)
        self.assertIn("int", joined)

    def test_v2_bad_structure(self):
        v2 = v1_to_v2(V1_EVENT)
        del v2["event"]["data"]["order"]["money"]
        v2["event"]["data"]["order"]["items"][0]["quantity"] = -3
        with self.assertRaises(ConversionError) as ctx:
            v2_to_v1(v2)
        joined = " ".join(ctx.exception.errors)
        self.assertIn("money", joined)
        self.assertIn("正整数", joined)


if __name__ == "__main__":
    unittest.main()
