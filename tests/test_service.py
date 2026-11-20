"""服务编排测试：双写、幂等、隔离、回放、消费者升级/回滚、影响报告。"""

import copy
import unittest

from schema_bridge.schemas import V1, V2
from schema_bridge.service import NotFoundError, SchemaBridgeService

V1_EVENT = {
    "event_id": "evt-1", "event_type": "ORDER_CREATED",
    "occurred_at": "2026-11-20T10:00:00Z",
    "order_id": "ord-1", "customer_id": "cus-1",
    "amount": 1999, "currency": "USD", "status": "created",
    "items": [{"sku": "sku-1", "name": "Keyboard", "qty": 2,
               "unit_price": 5000}],
}

V2_EVENT = {
    "schema_version": "v2",
    "event": {
        "id": "evt-2", "type": "ORDER_PAID",
        "occurred_at": "2026-11-20T11:00:00Z",
        "data": {"order": {
            "id": "ord-2", "status": "paid",
            "money": {"amount_cents": 900, "currency": "EUR"},
            "customer": {"id": "cus-2"},
            "items": [{"sku": "sku-9", "name": "Cable", "quantity": 1,
                       "unit_money": {"amount_cents": 900, "currency": "EUR"}}],
        }},
    },
}

BAD_V1 = {
    "event_id": "evt-bad", "event_type": "ORDER_CREATED",
    "occurred_at": "2026-11-20T12:00:00Z",
    "order_id": "ord-bad", "customer_id": "cus-9",
    "amount": -5, "currency": "USD", "status": "created",
    "items": [{"sku": "sku-x", "name": "Bad", "qty": 1, "unit_price": 10}],
}

UNRECOGNIZED = {"hello": "world"}


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.svc = SchemaBridgeService(":memory:")

    def tearDown(self):
        self.svc.close()


class IngestDualWriteTests(ServiceTestBase):
    def test_v1_event_dual_writes_both_versions(self):
        result = self.svc.ingest(V1_EVENT)
        self.assertEqual(result["status"], "processed")
        versions = sorted(r["version"] for r in result["results"])
        self.assertEqual(versions, [V1, V2])
        detail = self.svc.get_event_detail("evt-1")
        self.assertEqual({r["version"] for r in detail["converted"]},
                         {V1, V2})

    def test_v2_event_dual_writes_to_v1_too(self):
        result = self.svc.ingest(V2_EVENT)
        self.assertTrue(all(r["ok"] for r in result["results"]))
        detail = self.svc.get_event_detail("evt-2")
        v1 = next(r for r in detail["converted"] if r["version"] == V1)
        self.assertEqual(v1["payload_obj"]["order_id"], "ord-2")

    def test_ingest_is_idempotent_same_payload(self):
        first = self.svc.ingest(copy.deepcopy(V1_EVENT))
        second = self.svc.ingest(copy.deepcopy(V1_EVENT))
        self.assertEqual(first["status"], "processed")
        self.assertEqual(second["status"], "duplicate")
        stats = self.svc.stats()
        self.assertEqual(stats["events_total"], 1)
        self.assertEqual(stats["converted_rows"], 2)  # 每个版本仅一行

    def test_changed_payload_same_id_is_reprocessed(self):
        self.svc.ingest(copy.deepcopy(V1_EVENT))
        changed = copy.deepcopy(V1_EVENT)
        changed["amount"] = 2000
        result = self.svc.ingest(changed)
        self.assertEqual(result["status"], "processed")
        detail = self.svc.get_event_detail("evt-1")
        v1 = next(r for r in detail["converted"] if r["version"] == V1)
        self.assertEqual(v1["payload_obj"]["amount"], 2000)


class QuarantineTests(ServiceTestBase):
    def test_failed_conversion_keeps_raw_payload(self):
        result = self.svc.ingest(BAD_V1)
        # v1 同版本校验失败 + v2 转换失败，两条隔离
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(all(not r["ok"] for r in result["results"]))
        records = self.svc.list_quarantine()
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertIn("evt-bad", record["raw_payload"])
            self.assertIn("非负", record["error_detail"])
            self.assertIn(record["status"], ("open", "reopened"))

    def test_unrecognized_event_quarantined(self):
        result = self.svc.ingest(UNRECOGNIZED)
        self.assertEqual(result["status"], "invalid")
        records = self.svc.list_quarantine()
        self.assertEqual(records[0]["target_version"], "unknown")
        self.assertIn("hello", records[0]["raw_payload"])

    def test_repeated_failure_increments_attempts(self):
        self.svc.ingest(BAD_V1)
        self.svc.ingest(copy.deepcopy(BAD_V1))
        records = self.svc.list_quarantine()
        for record in records:
            self.assertEqual(record["attempts"], 2)
            self.assertEqual(record["status"], "reopened")


class ReplayTests(ServiceTestBase):
    def test_replay_original_still_fails_then_corrected_succeeds(self):
        self.svc.ingest(BAD_V1)
        q = next(r for r in self.svc.list_quarantine()
                 if r["target_version"] == V2)

        first = self.svc.replay(quarantine_id=q["id"])
        self.assertEqual(first["succeeded"], 0)
        self.assertEqual(first["failed"], 1)
        still_open = self.svc.get_quarantine_record(q["id"])
        self.assertIn(still_open["status"], ("open", "reopened"))

        fixed = copy.deepcopy(BAD_V1)
        fixed["amount"] = 500
        second = self.svc.replay(quarantine_id=q["id"], corrected=fixed)
        self.assertEqual(second["succeeded"], 1)
        resolved_q = self.svc.get_quarantine_record(q["id"])
        self.assertEqual(resolved_q["status"], "resolved")

        # 成功后两个版本都可查
        detail = self.svc.get_event_detail("evt-bad")
        self.assertEqual({r["version"] for r in detail["converted"]},
                         {V1, V2})

    def test_replay_rejects_correction_with_changed_event_id(self):
        self.svc.ingest(BAD_V1)
        q = self.svc.list_quarantine()[0]
        fixed = copy.deepcopy(BAD_V1)
        fixed["event_id"] = "evt-different"
        result = self.svc.replay(quarantine_id=q["id"], corrected=fixed)
        self.assertEqual(result["failed"], 1)
        self.assertIn("不一致", result["items"][0]["message"])

    def test_replay_all_open_and_by_event_ids(self):
        self.svc.ingest(BAD_V1)
        self.svc.ingest(UNRECOGNIZED)
        open_count = len([q for q in self.svc.list_quarantine()
                          if q["status"] != "resolved"])
        run = self.svc.replay(all_open=True)
        self.assertEqual(run["total"], open_count)
        self.assertEqual(run["succeeded"], 0)

        # 按 event_id 批量回放：原原文仍失败
        run2 = self.svc.replay(event_ids=["evt-bad"])
        self.assertGreaterEqual(run2["total"], 1)
        self.assertEqual(run2["failed"], run2["total"])

        # 修正 BAD_V1 后单条回放成功，该隔离记录 resolved
        fixed = copy.deepcopy(BAD_V1)
        fixed["amount"] = 500
        bad_q = next(q for q in self.svc.list_quarantine()
                     if q["event_id"] == "evt-bad"
                     and q["target_version"] == V2)
        fixed_run = self.svc.replay(quarantine_id=bad_q["id"], corrected=fixed)
        self.assertEqual(fixed_run["succeeded"], 1)
        self.assertEqual(
            self.svc.get_quarantine_record(bad_q["id"])["status"], "resolved")

        # 同 event_id 仍可能有其它未解决记录（v1 同版本校验失败那条）
        remaining = [q for q in self.svc.list_quarantine()
                     if q["event_id"] == "evt-bad"
                     and q["status"] != "resolved"]
        if remaining:
            run3 = self.svc.replay(event_ids=["evt-bad"])
            self.assertEqual(run3["total"], len(remaining))

        # 不存在的 id 找不到回放目标
        with self.assertRaises(NotFoundError):
            self.svc.replay(event_ids=["evt-not-exist"])

    def test_replay_run_history_persisted(self):
        self.svc.ingest(BAD_V1)
        self.svc.replay(all_open=True)
        runs = self.svc.list_replay_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0]["results"]), 2)


class ConsumerDeliveryTests(ServiceTestBase):
    def _setup(self):
        self.svc.ingest(V1_EVENT)
        self.svc.register_consumer("billing", V1, "账单")
        self.svc.register_consumer("analytics", V2, "分析")

    def test_deliver_uses_bound_version(self):
        self._setup()
        v1_out = self.svc.deliver("evt-1", "billing")
        v2_out = self.svc.deliver("evt-1", "analytics")
        self.assertEqual(v1_out["status"], "delivered")
        self.assertEqual(v1_out["version"], V1)
        self.assertEqual(v2_out["version"], V2)
        self.assertEqual(v2_out["payload"]["event"]["id"], "evt-1")

    def test_delivery_is_idempotent(self):
        self._setup()
        first = self.svc.deliver("evt-1", "billing")
        second = self.svc.deliver("evt-1", "billing")
        self.assertTrue(first["delivered"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.svc.delivery_log("billing")), 1)

    def test_partial_upgrade_and_rollback(self):
        self._setup()
        # 部分升级：只有 billing 升 v2，analytics 本来就是 v2
        self.svc.bind_consumer("billing", V2, "灰度升级")
        upgraded = self.svc.deliver("evt-1", "billing")
        self.assertEqual(upgraded["version"], V2)
        # 回滚
        self.svc.bind_consumer("billing", V1, "回滚")
        # 同一消费者此前已投过 evt-1，回滚后再投是幂等跳过而非重发
        again = self.svc.deliver("evt-1", "billing")
        self.assertEqual(again["status"], "duplicate")
        history = self.svc.binding_history("billing")
        reasons = [h["reason"] for h in history]
        self.assertIn("灰度升级", reasons)
        self.assertIn("回滚", reasons)
        versions = [(h["from_version"], h["to_version"]) for h in history]
        self.assertIn((V1, V2), versions)
        self.assertIn((V2, V1), versions)

    def test_bind_same_version_is_noop(self):
        self._setup()
        before = len(self.svc.binding_history("billing"))
        self.svc.bind_consumer("billing", V1)
        self.assertEqual(len(self.svc.binding_history("billing")), before)

    def test_deliver_unavailable_when_version_missing(self):
        # 坏事件没有任何成功版本
        self.svc.ingest(BAD_V1)
        self.svc.register_consumer("billing", V1, "账单")
        out = self.svc.deliver("evt-bad", "billing")
        self.assertEqual(out["status"], "unavailable")

    def test_unknown_consumer_and_validation(self):
        with self.assertRaises(NotFoundError):
            self.svc.bind_consumer("ghost", V1)
        with self.assertRaises(ValueError):
            self.svc.register_consumer(" ", V1)
        with self.assertRaises(ValueError):
            self.svc.register_consumer("x", "v9")


class ImpactReportTests(ServiceTestBase):
    def test_report_blocks_when_consumers_bound_and_quarantine_open(self):
        self.svc.ingest(V1_EVENT)
        self.svc.ingest(BAD_V1)
        self.svc.register_consumer("billing", V1)
        report = self.svc.impact_report(V1)
        self.assertEqual(report["verdict"], "不可下线")
        self.assertEqual(len(report["affected_consumers"]), 1)
        self.assertEqual(report["affected_consumers"][0]["name"], "billing")
        self.assertGreaterEqual(len(report["blocking_quarantine"]), 1)
        self.assertTrue(any("消费者" in r for r in report["reasons"]))

    def test_report_allows_retire_after_full_upgrade_and_replay(self):
        self.svc.ingest(V1_EVENT)
        self.svc.register_consumer("billing", V1)
        self.svc.register_consumer("analytics", V2)
        self.svc.bind_consumer("billing", V2, "全量升级")
        report = self.svc.impact_report(V1)
        self.assertEqual(report["verdict"], "可下线")
        self.assertEqual(report["affected_consumers"], [])
        self.assertEqual(report["alternative_missing"], 0)
        self.assertEqual(report["blocking_quarantine"], [])

    def test_report_tracks_alternative_missing_events(self):
        # 无法识别的事件永远没有替代版本数据
        self.svc.ingest(UNRECOGNIZED)
        report = self.svc.impact_report(V1)
        self.assertEqual(report["verdict"], "不可下线")
        self.assertGreaterEqual(report["alternative_missing"], 0)


if __name__ == "__main__":
    unittest.main()
