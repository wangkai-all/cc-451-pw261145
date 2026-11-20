"""演示数据播种与端到端场景脚本。

运行：``python -m schema_bridge.seed [db.sqlite3]``
覆盖：v1/v2 正常事件双写、两类坏事件进隔离、三个消费者、
部分升级（仅 search 升 v2）、回滚、幂等摄入与投递、回放与影响报告。
"""

from __future__ import annotations

import copy
import json
import sys

from .schemas import V1, V2
from .service import SchemaBridgeService

V1_GOOD_1 = {
    "event_id": "evt-1001", "event_type": "ORDER_CREATED",
    "occurred_at": "2026-11-20T09:15:00Z",
    "order_id": "ord-77001", "customer_id": "cus-100",
    "amount": 12999, "currency": "USD", "status": "created",
    "remark": "次日达",
    "items": [
        {"sku": "sku-kb-01", "name": "机械键盘", "qty": 1, "unit_price": 8999},
        {"sku": "sku-mc-02", "name": "鼠标垫", "qty": 2, "unit_price": 2000},
    ],
}

V1_GOOD_2 = {
    "event_id": "evt-1002", "event_type": "ORDER_PAID",
    "occurred_at": "2026-11-20T10:05:30Z",
    "order_id": "ord-77002", "customer_id": "cus-101",
    "amount": 5400, "currency": "EUR", "status": "paid",
    "items": [
        {"sku": "sku-usb-c", "name": "Type-C 线缆", "qty": 3, "unit_price": 1800},
    ],
}

V2_GOOD_1 = {
    "schema_version": "v2",
    "event": {
        "id": "evt-2001", "type": "ORDER_SHIPPED",
        "occurred_at": "2026-11-20T11:20:00Z",
        "data": {"order": {
            "id": "ord-77003", "status": "shipped",
            "money": {"amount_cents": 20500, "currency": "USD"},
            "customer": {"id": "cus-102"},
            "items": [
                {"sku": "sku-mon-27", "name": "27 寸显示器", "quantity": 1,
                 "unit_money": {"amount_cents": 20500, "currency": "USD"}},
            ],
            "note": "含底座",
        }},
    },
}

# 坏样本 A：v1 金额为负数、qty 为 0、currency 非法、缺 items
V1_BAD = {
    "event_id": "evt-9001", "event_type": "ORDER_CREATED",
    "occurred_at": "2026-11-20T12:00:00Z",
    "order_id": "ord-bad-1", "customer_id": "cus-9",
    "amount": -1500, "currency": "usd", "status": "created",
    "items": [{"sku": "sku-x", "name": "坏商品", "qty": 0, "unit_price": 100}],
}

# 坏样本 B：结构无法识别（无 v1 标志字段，也不是 v2 信封）
UNRECOGNIZED = {"foo": "bar", "ts": "not-an-event"}


def seed(db_path: str) -> SchemaBridgeService:
    svc = SchemaBridgeService(db_path)

    print("== 1. 摄入正常事件（双写 v1/v2） ==")
    for payload in (V1_GOOD_1, V1_GOOD_2, V2_GOOD_1):
        result = svc.ingest(payload)
        print(f"  {result['event_id']}: {result['status']} -> "
              f"{[r['version'] + ('✓' if r['ok'] else '✗') for r in result['results']]}")

    print("== 2. 重复摄入必须幂等 ==")
    again = svc.ingest(copy.deepcopy(V1_GOOD_1))
    print(f"  evt-1001 再次摄入状态：{again['status']}（duplicate 即幂等命中）")

    print("== 3. 坏事件保留原文进入隔离队列 ==")
    bad_a = svc.ingest(V1_BAD)
    print(f"  evt-9001：{[(r['version'], r['ok']) for r in bad_a['results']]}")
    bad_b = svc.ingest(UNRECOGNIZED)
    print(f"  无法识别样本：{bad_b['status']}，隔离 #{bad_b['quarantine_id']}")

    print("== 4. 注册消费者并按版本投递 ==")
    svc.register_consumer("billing-service", V1, "账单服务，依赖旧版扁平字段")
    svc.register_consumer("search-indexer", V1, "搜索索引构建器")
    svc.register_consumer("analytics-hub", V2, "数据仓库分析平台")
    for event_id in ("evt-1001", "evt-1002", "evt-2001"):
        for outcome in svc.deliver_to_all(event_id):
            print(f"  投递 {event_id} -> {outcome['consumer']} "
                  f"[{outcome['version']}]: {outcome['status']}")
    dup = svc.deliver("evt-1001", "billing-service")
    print(f"  重复投递 evt-1001 -> billing-service：{dup['status']}（幂等跳过）")

    print("== 5. 部分消费者升级：仅 search-indexer v1 -> v2 ==")
    svc.bind_consumer("search-indexer", V2, "灰度升级：搜索服务先行迁移 v2")
    upgraded = svc.deliver("evt-1001", "search-indexer")
    print(f"  升级后投递：{upgraded['status']}，取数版本 {upgraded['version']}")
    billing = svc.deliver("evt-1002", "billing-service")
    print(f"  未升级的 billing-service 仍取：{billing['version']}")

    print("== 6. 回滚：search-indexer v2 -> v1 ==")
    svc.bind_consumer("search-indexer", V1, "回滚：v2 消费端发现兼容问题")
    rolled = svc.deliver("evt-1002", "search-indexer")
    print(f"  回滚后投递：{rolled['status']}，取数版本 {rolled['version']}")

    print("== 7. 回放：先按原原文重试（仍失败），再用修正原文回放成功 ==")
    open_records = svc.list_quarantine("open") + svc.list_quarantine("reopened")
    target = next(q for q in open_records if q["event_id"] == "evt-9001"
                  and q["target_version"] == V2)
    first_try = svc.replay(quarantine_id=target["id"])
    print(f"  原原文回放：成功 {first_try['succeeded']} / 失败 {first_try['failed']}")
    fixed = copy.deepcopy(V1_BAD)
    fixed.update(amount=1500, currency="USD",
                 items=[{"sku": "sku-x", "name": "坏商品",
                         "qty": 1, "unit_price": 100}])
    second_try = svc.replay(quarantine_id=target["id"], corrected=fixed)
    print(f"  修正原文回放：成功 {second_try['succeeded']} / 失败 {second_try['failed']}")

    print("== 8. v1 下线影响报告 ==")
    report = svc.impact_report(V1)
    print(json.dumps(
        {"verdict": report["verdict"], "reasons": report["reasons"],
         "affected_consumers": [c["name"] for c in report["affected_consumers"]],
         "alternative_missing": report["alternative_missing"],
         "blocking_quarantine": len(report["blocking_quarantine"])},
        ensure_ascii=False, indent=2))

    print("== 9. 全部消费者升级 v2 后再次评估 ==")
    for name in ("billing-service", "search-indexer"):
        svc.bind_consumer(name, V2, "全量迁移 v2")
    # 无法识别的隔离事件仍然阻塞 v2 就绪度
    report2 = svc.impact_report(V1)
    print(json.dumps(
        {"verdict": report2["verdict"], "reasons": report2["reasons"],
         "affected_consumers": [c["name"] for c in report2["affected_consumers"]],
         "blocking_quarantine": len(report2["blocking_quarantine"])},
        ensure_ascii=False, indent=2))

    return svc


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "schema_bridge.sqlite3"
    svc = seed(db_path)
    print(f"\n播种完成，数据库：{db_path}")
    print("启动页面：python -m schema_bridge.cli serve --db "
          f"{db_path} --host 127.0.0.1 --port 8000")
    svc.close()


if __name__ == "__main__":
    main()
