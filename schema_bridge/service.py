"""Schema Bridge 编排服务：摄入双写、隔离、回放、消费者投递与影响报告。

幂等保证：
1. 转换是确定性纯函数（见 :mod:`schema_bridge.converter`），结果以规范化 JSON 存储；
2. 事件/双写/隔离均以自然键 upsert，重复摄入不产生重复数据；
3. 投递日志带 ``UNIQUE(event_id, consumer_name)`` 约束，重复投递只跳过不重发。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .converter import (ConversionError, InvalidEvent, convert,
                        detect_version, extract_event_id)
from .schemas import V1, V2, VERSIONS, schema_diff
from .store import Store, canonical_json, utc_now_iso


class NotFoundError(LookupError):
    """引用的资源（消费者/隔离记录/事件）不存在。"""


class SchemaBridgeService:
    def __init__(self, db_path: str = ":memory:"):
        self.store = Store(db_path)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # 内部：对一份原文执行全版本转换并落库（双写）
    # ------------------------------------------------------------------

    def _convert_and_store(self, conn: sqlite3.Connection, raw: Any) -> dict:
        source_version = detect_version(raw)
        event_id = extract_event_id(raw)
        raw_text = canonical_json(raw)

        if source_version is None:
            error = InvalidEvent("无法识别事件结构：既不是 v1 扁平结构，也不是 v2 信封结构")
            qid = self.store.upsert_quarantine(
                conn, event_id=event_id, source_version=None,
                target_version="unknown", raw_payload=raw_text,
                error_message=str(error),
                error_detail=canonical_json(error.errors))
            return {"status": "invalid", "event_id": event_id,
                    "source_version": None, "quarantine_id": qid, "results": []}

        self.store.upsert_event(conn, event_id, source_version, raw_text)

        results = []
        for target in VERSIONS:
            try:
                converted = convert(raw, target)
                self.store.upsert_converted(
                    conn, event_id, target, canonical_json(converted))
                results.append({"version": target, "ok": True})
            except ConversionError as exc:
                qid = self.store.upsert_quarantine(
                    conn, event_id=event_id, source_version=source_version,
                    target_version=target, raw_payload=raw_text,
                    error_message=str(exc),
                    error_detail=canonical_json(exc.errors))
                results.append({"version": target, "ok": False,
                                "error": exc.to_dict(), "quarantine_id": qid})
        return {"status": "processed", "event_id": event_id,
                "source_version": source_version, "results": results}

    # ------------------------------------------------------------------
    # 摄入（双写 + 幂等）
    # ------------------------------------------------------------------

    def ingest(self, raw: Any) -> dict:
        """摄入一个事件；同 event_id + 同原文重复调用直接返回既有结果。"""
        event_id = extract_event_id(raw)
        with self.store.connect() as conn:
            if event_id is not None:
                existing = self.store.get_event(conn, event_id)
                if existing is not None \
                        and existing["source_payload"] == canonical_json(raw):
                    has_success = len(
                        self.store.list_converted(conn, event_id)) > 0
                    open_quarantine = conn.execute(
                        "SELECT 1 FROM quarantined WHERE event_id = ? "
                        "AND status != 'resolved' LIMIT 1",
                        (event_id,)).fetchone()
                    # 仅当已成功双写且无未解决隔离时才幂等跳过；
                    # 仍处于失败态的事件要重新走转换以累加尝试次数
                    if has_success and open_quarantine is None:
                        return {
                            "status": "duplicate",
                            "event_id": event_id,
                            "source_version": existing["source_version"],
                            "results": [
                                {"version": row["version"], "ok": True,
                                 "payload": json.loads(row["payload"])}
                                for row in
                                self.store.list_converted(conn, event_id)
                            ],
                            "message": "事件已存在且原文一致，跳过重复转换（幂等）",
                        }
            return self._convert_and_store(conn, raw)

    # ------------------------------------------------------------------
    # 隔离队列
    # ------------------------------------------------------------------

    def list_quarantine(self, status: str | None = None) -> list[dict]:
        with self.store.connect() as conn:
            return [dict(row) for row in
                    self.store.list_quarantine(conn, status)]

    def get_quarantine_record(self, quarantine_id: int) -> dict:
        with self.store.connect() as conn:
            row = self.store.get_quarantine(conn, quarantine_id)
            if row is None:
                raise NotFoundError(f"隔离记录 #{quarantine_id} 不存在")
            return dict(row)

    # ------------------------------------------------------------------
    # 回放
    # ------------------------------------------------------------------

    def replay(self, *, quarantine_id: int | None = None,
               all_open: bool = False, event_ids: list[str] | None = None,
               corrected: Any = None) -> dict:
        """回放隔离事件。

        - ``quarantine_id``：单条回放；
        - ``all_open``：回放全部 open/reopened 记录；
        - ``event_ids``：按事件 ID 批量回放；
        - ``corrected``：人工修正后的事件原文（仅单条回放允许），
          修正原文的 event_id 必须与隔离记录一致（或原记录本就没有 ID）。
        """
        if corrected is not None and quarantine_id is None:
            raise ValueError("修正原文只能用于单条回放")
        with self.store.connect() as conn:
            rows = self._select_replay_targets(
                conn, quarantine_id, all_open, event_ids)
            if not rows:
                raise NotFoundError("没有匹配到可回放的隔离记录")

            run_items: list[dict] = []
            for row in rows:
                record = dict(row)
                use_corrected = corrected is not None and record["id"] == quarantine_id
                try:
                    payload = corrected if use_corrected \
                        else json.loads(record["raw_payload"])
                except json.JSONDecodeError as exc:
                    run_items.append(self._replay_failure(
                        conn, record, f"原文不是合法 JSON：{exc}", use_corrected))
                    continue

                if use_corrected:
                    mismatch = self._validate_correction(record, payload)
                    if mismatch:
                        run_items.append(self._replay_failure(
                            conn, record, mismatch, True))
                        continue

                target = record["target_version"]
                try:
                    if target == "unknown":
                        # 原结构无法识别：修正后重新走完整双写
                        outcome = self._convert_and_store(conn, payload)
                        ok = outcome["status"] == "processed" and all(
                            r["ok"] for r in outcome["results"])
                        if not ok:
                            raise ConversionError(
                                "修正后的事件仍无法通过全部版本校验")
                    else:
                        convert(payload, target)  # 先验证原失败目标
                        outcome = self._convert_and_store(conn, payload)
                        ok = any(r["version"] == target and r["ok"]
                                 for r in outcome["results"])
                        if not ok:
                            raise ConversionError(
                                f"目标版本 {target} 转换仍然失败")
                    self.store.resolve_quarantine(conn, record["id"])
                    run_items.append({
                        "quarantine_id": record["id"],
                        "event_id": extract_event_id(payload),
                        "status": "success",
                        "message": "转换成功并已双写，隔离记录标记 resolved",
                        "corrected": use_corrected})
                except ConversionError as exc:
                    run_items.append(self._replay_failure(
                        conn, record,
                        f"{exc}；{' / '.join(exc.errors)}", use_corrected))

            succeeded = sum(1 for i in run_items if i["status"] == "success")
            scope = ("single" if quarantine_id is not None
                     else "events" if event_ids else "all")
            run_id = self.store.create_replay_run(
                conn, scope=scope, total=len(run_items),
                succeeded=succeeded, failed=len(run_items) - succeeded)
            for item in run_items:
                self.store.add_replay_result(
                    conn, run_id, item["quarantine_id"], item["event_id"],
                    item["status"], item["message"], item["corrected"])
            return {"run_id": run_id, "scope": scope, "total": len(run_items),
                    "succeeded": succeeded,
                    "failed": len(run_items) - succeeded, "items": run_items}

    def _select_replay_targets(self, conn: sqlite3.Connection,
                               quarantine_id: int | None, all_open: bool,
                               event_ids: list[str] | None) -> list[sqlite3.Row]:
        if quarantine_id is not None:
            row = self.store.get_quarantine(conn, quarantine_id)
            return [row] if row is not None else []
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            return conn.execute(
                f"SELECT * FROM quarantined WHERE event_id IN ({placeholders}) "
                f"AND status != 'resolved' ORDER BY id", tuple(event_ids)).fetchall()
        if all_open:
            return conn.execute(
                "SELECT * FROM quarantined WHERE status != 'resolved' ORDER BY id"
            ).fetchall()
        return []

    @staticmethod
    def _validate_correction(record: dict, payload: Any) -> str | None:
        if detect_version(payload) is None:
            return "修正后的原文仍无法识别结构版本"
        new_id = extract_event_id(payload)
        if record["event_id"] and new_id != record["event_id"]:
            return (f"修正原文的 event_id={new_id!r} 与隔离记录 "
                    f"{record['event_id']!r} 不一致，拒绝回放")
        return None

    def _replay_failure(self, conn: sqlite3.Connection, record: dict,
                        message: str, corrected: bool) -> dict:
        # 重新走一次 upsert，让 attempts 累加、状态置为 reopened
        self.store.upsert_quarantine(
            conn, event_id=record["event_id"],
            source_version=record["source_version"],
            target_version=record["target_version"],
            raw_payload=record["raw_payload"],
            error_message=message[:500],
            error_detail=record["error_detail"])
        return {"quarantine_id": record["id"], "event_id": record["event_id"],
                "status": "failed", "message": message,
                "corrected": corrected}

    def list_replay_runs(self) -> list[dict]:
        with self.store.connect() as conn:
            runs = [dict(r) for r in self.store.list_replay_runs(conn)]
            for run in runs:
                run["results"] = [
                    dict(r) for r in
                    self.store.list_replay_results(conn, run["id"])]
            return runs

    # ------------------------------------------------------------------
    # 消费者与版本绑定（升级 / 回滚）
    # ------------------------------------------------------------------

    def register_consumer(self, name: str, bound_version: str,
                          description: str = "") -> dict:
        if bound_version not in VERSIONS:
            raise ValueError(f"不支持的版本：{bound_version!r}")
        if not name.strip():
            raise ValueError("消费者名称不能为空")
        with self.store.connect() as conn:
            existing = self.store.get_consumer(conn, name)
            self.store.upsert_consumer(conn, name.strip(), bound_version,
                                       description)
            if existing is None:
                self.store.record_binding(
                    conn, name.strip(), None, bound_version, "注册消费者")
            row = self.store.get_consumer(conn, name.strip())
            return dict(row)

    def bind_consumer(self, name: str, version: str, reason: str = "") -> dict:
        """切换消费者绑定版本；from==to 时幂等返回，不写审计。"""
        if version not in VERSIONS:
            raise ValueError(f"不支持的版本：{version!r}")
        with self.store.connect() as conn:
            consumer = self.store.get_consumer(conn, name)
            if consumer is None:
                raise NotFoundError(f"消费者 {name!r} 不存在")
            if consumer["bound_version"] == version:
                return dict(consumer)
            self.store.record_binding(
                conn, name, consumer["bound_version"], version,
                reason or ("消费者升级" if self._is_upgrade(
                    consumer["bound_version"], version) else "消费者回滚"))
            self.store.upsert_consumer(
                conn, name, version, consumer["description"])
            return dict(self.store.get_consumer(conn, name))

    @staticmethod
    def _is_upgrade(frm: str, to: str) -> bool:
        return VERSIONS.index(to) > VERSIONS.index(frm)

    def list_consumers(self) -> list[dict]:
        with self.store.connect() as conn:
            return [dict(r) for r in self.store.list_consumers(conn)]

    def binding_history(self, consumer_name: str | None = None) -> list[dict]:
        with self.store.connect() as conn:
            return [dict(r) for r in
                    self.store.list_binding_history(conn, consumer_name)]

    # ------------------------------------------------------------------
    # 按消费者绑定版本投递
    # ------------------------------------------------------------------

    def deliver(self, event_id: str, consumer_name: str) -> dict:
        with self.store.connect() as conn:
            consumer = self.store.get_consumer(conn, consumer_name)
            if consumer is None:
                raise NotFoundError(f"消费者 {consumer_name!r} 不存在")
            version = consumer["bound_version"]
            converted = self.store.get_converted(conn, event_id, version)
            if converted is None:
                return {"event_id": event_id, "consumer": consumer_name,
                        "version": version, "status": "unavailable",
                        "delivered": False,
                        "message": f"该事件的 {version} 版本尚未转换成功（见隔离队列）"}
            if self.store.has_delivery(conn, event_id, consumer_name):
                return {"event_id": event_id, "consumer": consumer_name,
                        "version": version, "status": "duplicate",
                        "delivered": False,
                        "message": "已投递过，幂等跳过"}
            self.store.record_delivery(
                conn, event_id, consumer_name, version, "delivered")
            return {"event_id": event_id, "consumer": consumer_name,
                    "version": version, "status": "delivered",
                    "delivered": True,
                    "payload": json.loads(converted["payload"])}

    def deliver_to_all(self, event_id: str) -> list[dict]:
        with self.store.connect() as conn:
            names = [r["name"] for r in self.store.list_consumers(conn)]
        return [self.deliver(event_id, name) for name in names]

    def delivery_log(self, consumer_name: str | None = None) -> list[dict]:
        with self.store.connect() as conn:
            return [dict(r) for r in
                    self.store.list_delivery_log(conn, consumer_name)]

    # ------------------------------------------------------------------
    # 版本下线影响报告
    # ------------------------------------------------------------------

    def impact_report(self, retire_version: str) -> dict:
        """评估下线 ``retire_version`` 前的消费者影响与数据就绪度。"""
        if retire_version not in VERSIONS:
            raise ValueError(f"不支持的版本：{retire_version!r}")
        other = V1 if retire_version == V2 else V2
        with self.store.connect() as conn:
            affected = [dict(r) for r in conn.execute(
                "SELECT * FROM consumers WHERE bound_version = ? ORDER BY name",
                (retire_version,)).fetchall()]
            events = self.store.list_events(conn, limit=100000)
            event_ids = [r["event_id"] for r in events]
            converted_rows = conn.execute(
                "SELECT event_id FROM converted_events WHERE version = ?",
                (other,)).fetchall()
            ready_ids = {r["event_id"] for r in converted_rows}
            missing_ids = [eid for eid in event_ids if eid not in ready_ids]
            blockers = [dict(r) for r in conn.execute(
                "SELECT * FROM quarantined WHERE status != 'resolved' "
                "ORDER BY id").fetchall()]
            history = self.store.list_binding_history(conn)

            consumer_views = []
            for consumer in affected:
                changes = [dict(h) for h in history
                           if h["consumer_name"] == consumer["name"]]
                delivered = self.store.count(
                    conn, "delivery_log",
                    "WHERE consumer_name = ? AND version = ?",
                    (consumer["name"], retire_version))
                consumer_views.append({
                    "name": consumer["name"],
                    "description": consumer["description"],
                    "bound_version": consumer["bound_version"],
                    "delivered_on_retiring_version": delivered,
                    "binding_history": changes,
                })

            verdict = "可下线" if not affected and not blockers else "不可下线"
            reasons = []
            if affected:
                reasons.append(
                    f"{len(affected)} 个消费者仍绑定 {retire_version}，需先升级到 {other}")
            if blockers:
                reasons.append(
                    f"{len(blockers)} 条隔离事件（含无法识别原文）未解决，"
                    f"无法产出 {other} 版本，下线后无法补投")
            if missing_ids:
                reasons.append(
                    f"{len(missing_ids)} 个事件缺少 {other} 版本双写数据")
            return {
                "retire_version": retire_version,
                "alternative_version": other,
                "generated_at": utc_now_iso(),
                "verdict": verdict,
                "reasons": reasons or ["无阻塞项"],
                "affected_consumers": consumer_views,
                "events_total": len(event_ids),
                "alternative_ready": len(ready_ids),
                "alternative_missing": len(missing_ids),
                "missing_event_ids": missing_ids,
                "blocking_quarantine": blockers,
            }

    # ------------------------------------------------------------------
    # 查询与统计
    # ------------------------------------------------------------------

    def list_events(self) -> list[dict]:
        with self.store.connect() as conn:
            events = []
            for row in self.store.list_events(conn):
                item = dict(row)
                item["versions"] = [
                    r["version"] for r in
                    self.store.list_converted(conn, row["event_id"])]
                item["quarantine"] = [
                    {"id": q["id"], "target_version": q["target_version"],
                     "status": q["status"]}
                    for q in conn.execute(
                        "SELECT * FROM quarantined WHERE event_id = ? ORDER BY id",
                        (row["event_id"],)).fetchall()]
                events.append(item)
            return events

    def get_event_detail(self, event_id: str) -> dict:
        with self.store.connect() as conn:
            row = self.store.get_event(conn, event_id)
            if row is None:
                raise NotFoundError(f"事件 {event_id!r} 不存在")
            detail = dict(row)
            detail["converted"] = [
                {**dict(r), "payload_obj": json.loads(r["payload"])}
                for r in self.store.list_converted(conn, event_id)]
            detail["quarantine"] = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM quarantined WHERE event_id = ? ORDER BY id",
                    (event_id,)).fetchall()]
            detail["deliveries"] = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM delivery_log WHERE event_id = ? ORDER BY id",
                    (event_id,)).fetchall()]
            return detail

    def stats(self) -> dict:
        with self.store.connect() as conn:
            q_open = self.store.count(
                conn, "quarantined", "WHERE status != 'resolved'")
            q_resolved = self.store.count(
                conn, "quarantined", "WHERE status = 'resolved'")
            consumers = self.store.list_consumers(conn)
            return {
                "events_total": self.store.count(conn, "events"),
                "converted_rows": self.store.count(conn, "converted_events"),
                "quarantine_open": q_open,
                "quarantine_resolved": q_resolved,
                "consumers_total": len(consumers),
                "consumers_by_version": {
                    v: sum(1 for c in consumers if c["bound_version"] == v)
                    for v in VERSIONS},
                "replay_runs": self.store.count(conn, "replay_runs"),
                "deliveries": self.store.count(conn, "delivery_log"),
            }

    def schema_diff(self) -> dict:
        return schema_diff()
