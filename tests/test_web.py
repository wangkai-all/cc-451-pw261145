"""Web/API 端到端测试：启动真实 HTTP 服务，用 urllib 走完整请求链路。"""

import json
import threading
import unittest
import urllib.request
import urllib.error

from schema_bridge.web import build_server

V1_EVENT = {
    "event_id": "evt-web-1", "event_type": "ORDER_CREATED",
    "occurred_at": "2026-11-20T10:00:00Z",
    "order_id": "ord-web-1", "customer_id": "cus-1",
    "amount": 1999, "currency": "USD", "status": "created",
    "items": [{"sku": "sku-1", "name": "Keyboard", "qty": 2,
               "unit_price": 5000}],
}

BAD_EVENT = {
    "event_id": "evt-web-bad", "event_type": "ORDER_CREATED",
    "occurred_at": "2026-11-20T10:00:00Z",
    "order_id": "ord-x", "customer_id": "cus-1",
    "amount": -1, "currency": "USD", "status": "created",
    "items": [{"sku": "sku-1", "name": "X", "qty": 1, "unit_price": 1}],
}


class WebTestBase(unittest.TestCase):
    def setUp(self):
        self.server = build_server(":memory:", "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.service.close()
        self.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, resp.read()

    def post_json(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class PageRenderTests(WebTestBase):
    def test_all_pages_render(self):
        for path in ("/", "/diff", "/events", "/quarantine", "/replay",
                     "/consumers", "/impact", "/impact?version=v2"):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("Schema Bridge".encode(), body)

    def test_unknown_page_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)


class ApiFlowTests(WebTestBase):
    def test_full_api_flow(self):
        # 摄入 -> 双写
        status, result = self.post_json("/api/ingest", V1_EVENT)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "processed")

        # 幂等
        status, again = self.post_json("/api/ingest", V1_EVENT)
        self.assertEqual(again["status"], "duplicate")

        # 事件详情
        status, detail = self.get("/api/events/evt-web-1")
        detail = json.loads(detail)
        self.assertEqual({c["version"] for c in detail["converted"]},
                         {"v1", "v2"})

        # 注册消费者 + 投递
        self.post_json("/api/consumers/register",
                       {"name": "billing", "version": "v1"})
        self.post_json("/api/consumers/register",
                       {"name": "analytics", "version": "v2",
                        "description": "数仓"})
        status, delivery = self.post_json(
            "/api/deliver", {"event_id": "evt-web-1", "consumer": "billing"})
        self.assertEqual(delivery["status"], "delivered")
        status, dup = self.post_json(
            "/api/deliver", {"event_id": "evt-web-1", "consumer": "billing"})
        self.assertEqual(dup["status"], "duplicate")

        # 部分升级 + 回滚
        status, bound = self.post_json(
            "/api/consumers/bind",
            {"name": "billing", "version": "v2", "reason": "升级"})
        self.assertEqual(bound["bound_version"], "v2")
        status, bound = self.post_json(
            "/api/consumers/bind",
            {"name": "billing", "version": "v1", "reason": "回滚"})
        self.assertEqual(bound["bound_version"], "v1")

        # 影响报告
        status, report = self.get("/api/impact?version=v1")
        report = json.loads(report)
        self.assertIn(report["verdict"], ("可下线", "不可下线"))
        self.assertEqual(len(report["affected_consumers"]), 1)

        # diff / stats
        status, diff = self.get("/api/diff")
        diff = json.loads(diff)
        self.assertTrue(diff["mappings"])
        status, stats = self.get("/api/stats")
        stats = json.loads(stats)
        self.assertEqual(stats["events_total"], 1)

    def test_bad_event_quarantine_and_replay_flow(self):
        status, result = self.post_json("/api/ingest", BAD_EVENT)
        self.assertEqual(status, 200)
        self.assertTrue(all(not r["ok"] for r in result["results"]))

        status, payload = self.get("/api/quarantine")
        records = json.loads(payload)["quarantine"]
        self.assertEqual(len(records), 2)
        self.assertIn("evt-web-bad", records[0]["raw_payload"])

        # 原原文回放失败
        qid = next(q["id"] for q in records if q["target_version"] == "v2")
        status, run = self.post_json(
            "/api/replay", {"quarantine_id": qid})
        self.assertEqual(run["failed"], 1)

        # 修正后回放成功
        fixed = dict(BAD_EVENT, amount=42)
        status, run = self.post_json(
            "/api/replay", {"quarantine_id": qid, "corrected": fixed})
        self.assertEqual(run["succeeded"], 1)

        status, payload = self.get("/api/replay")
        runs = json.loads(payload)["runs"]
        self.assertEqual(len(runs), 2)

    def test_api_error_handling(self):
        status, payload = self.post_json("/api/ingest", {"foo": "bar"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "invalid")

        status, payload = self.post_json(
            "/api/consumers/bind", {"name": "ghost", "version": "v1"})
        self.assertEqual(status, 404)

        status, payload = self.post_json(
            "/api/consumers/register", {"name": "x", "version": "v9"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
