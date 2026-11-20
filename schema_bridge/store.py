"""SQLite 持久层。

所有写操作以 event_id / 自然键做幂等 upsert；连接默认开启外键约束，
JSON 统一以排序键的规范化文本存储，保证「同输入 = 同存储内容」。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schemas import V1, V2, VERSIONS


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> str:
    """规范化 JSON：排序键、无多余空白，幂等键直接取它。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    source_version  TEXT NOT NULL,
    source_payload  TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 双写：每个事件每个版本一行；canonical 转换结果保证幂等
CREATE TABLE IF NOT EXISTS converted_events (
    event_id        TEXT NOT NULL,
    version         TEXT NOT NULL,
    payload         TEXT NOT NULL,
    converted_at    TEXT NOT NULL,
    PRIMARY KEY (event_id, version),
    FOREIGN KEY (event_id) REFERENCES events(event_id)
);

-- 隔离队列：原文永不丢弃；(event_id, target_version) 唯一
CREATE TABLE IF NOT EXISTS quarantined (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT,
    source_version  TEXT,
    target_version  TEXT NOT NULL,
    raw_payload     TEXT NOT NULL,
    error_message   TEXT NOT NULL,
    error_detail    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open / resolved / reopened
    attempts        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consumers (
    name            TEXT PRIMARY KEY,
    description     TEXT NOT NULL DEFAULT '',
    bound_version   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 版本切换审计（升级/回滚都留痕）
CREATE TABLE IF NOT EXISTS consumer_binding_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consumer_name   TEXT NOT NULL,
    from_version    TEXT,
    to_version      TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    changed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT NOT NULL,          -- all / single / events
    total           INTEGER NOT NULL,
    succeeded       INTEGER NOT NULL,
    failed          INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    quarantine_id   INTEGER NOT NULL,
    event_id        TEXT,
    status          TEXT NOT NULL,          -- success / failed
    message         TEXT NOT NULL,
    corrected       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES replay_runs(id)
);

-- 投递日志：(event_id, consumer) 幂等，重复投递不产生重复行
CREATE TABLE IF NOT EXISTS delivery_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    consumer_name   TEXT NOT NULL,
    version         TEXT NOT NULL,
    status          TEXT NOT NULL,          -- delivered / skipped
    message         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    UNIQUE (event_id, consumer_name)
);

CREATE INDEX IF NOT EXISTS idx_quarantine_status ON quarantined(status);
CREATE INDEX IF NOT EXISTS idx_delivery_consumer ON delivery_log(consumer_name);
"""


class Store:
    """单一持久连接 + 重入锁。

    ``:memory:`` 数据库每个连接是互相隔离的独立内存库，因此不能像文件库那样
    每次请求新开连接；统一持有一个连接并加锁，对内存库/文件库都正确，
    也保证 ThreadingHTTPServer 下的串行化事务安全。
    """

    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self.db_path != ":memory:":
                # 内存库不支持 WAL，文件库开启以提升并发读
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()

    def connect(self) -> "_LockedConnection":
        """返回事务上下文（with 块结束时提交/回滚）。"""
        return _LockedConnection(self._conn, self._lock)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 事件与双写
    # ------------------------------------------------------------------

    def get_event(self, conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()

    def upsert_event(self, conn: sqlite3.Connection, event_id: str,
                     source_version: str, source_payload: str) -> None:
        now = utc_now_iso()
        conn.execute(
            """INSERT INTO events(event_id, source_version, source_payload,
                                  first_seen_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(event_id) DO UPDATE SET updated_at = excluded.updated_at""",
            (event_id, source_version, source_payload, now, now))

    def get_converted(self, conn: sqlite3.Connection, event_id: str,
                      version: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM converted_events WHERE event_id = ? AND version = ?",
            (event_id, version)).fetchone()

    def upsert_converted(self, conn: sqlite3.Connection, event_id: str,
                         version: str, payload: str) -> None:
        now = utc_now_iso()
        conn.execute(
            """INSERT INTO converted_events(event_id, version, payload, converted_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(event_id, version) DO UPDATE SET
                   payload = excluded.payload,
                   converted_at = excluded.converted_at""",
            (event_id, version, payload, now))

    def list_converted(self, conn: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM converted_events WHERE event_id = ? ORDER BY version",
            (event_id,)).fetchall()

    def list_events(self, conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM events ORDER BY first_seen_at DESC, event_id DESC LIMIT ?",
            (limit,)).fetchall()

    # ------------------------------------------------------------------
    # 隔离队列
    # ------------------------------------------------------------------

    def upsert_quarantine(self, conn: sqlite3.Connection, event_id: str | None,
                          source_version: str | None, target_version: str,
                          raw_payload: str, error_message: str,
                          error_detail: str) -> int:
        """失败事件保留原文并进入隔离队列；同一 (event_id, target) 重复失败只累加 attempts。"""
        now = utc_now_iso()
        existing = conn.execute(
            "SELECT id, attempts FROM quarantined WHERE event_id IS ? AND target_version = ?",
            (event_id, target_version)).fetchone() if event_id else None
        if existing:
            conn.execute(
                """UPDATE quarantined
                   SET status = 'reopened', attempts = ?, raw_payload = ?,
                       error_message = ?, error_detail = ?, updated_at = ?
                   WHERE id = ?""",
                (existing["attempts"] + 1, raw_payload, error_message,
                 error_detail, now, existing["id"]))
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO quarantined(event_id, source_version, target_version,
                                       raw_payload, error_message, error_detail,
                                       status, attempts, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)""",
            (event_id, source_version, target_version, raw_payload,
             error_message, error_detail, now, now))
        return int(cur.lastrowid)

    def resolve_quarantine(self, conn: sqlite3.Connection, quarantine_id: int) -> None:
        conn.execute(
            "UPDATE quarantined SET status = 'resolved', updated_at = ? WHERE id = ?",
            (utc_now_iso(), quarantine_id))

    def get_quarantine(self, conn: sqlite3.Connection,
                       quarantine_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM quarantined WHERE id = ?", (quarantine_id,)).fetchone()

    def list_quarantine(self, conn: sqlite3.Connection,
                        status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return conn.execute(
                "SELECT * FROM quarantined WHERE status = ? ORDER BY id DESC",
                (status,)).fetchall()
        return conn.execute(
            "SELECT * FROM quarantined ORDER BY id DESC").fetchall()

    # ------------------------------------------------------------------
    # 消费者与绑定审计
    # ------------------------------------------------------------------

    def upsert_consumer(self, conn: sqlite3.Connection, name: str,
                        bound_version: str, description: str = "") -> None:
        now = utc_now_iso()
        conn.execute(
            """INSERT INTO consumers(name, description, bound_version, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   description = excluded.description,
                   bound_version = excluded.bound_version,
                   updated_at = excluded.updated_at""",
            (name, description, bound_version, now))

    def get_consumer(self, conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM consumers WHERE name = ?", (name,)).fetchone()

    def list_consumers(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM consumers ORDER BY name").fetchall()

    def record_binding(self, conn: sqlite3.Connection, consumer_name: str,
                       from_version: str | None, to_version: str,
                       reason: str) -> None:
        conn.execute(
            """INSERT INTO consumer_binding_history(consumer_name, from_version,
                                                    to_version, reason, changed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (consumer_name, from_version, to_version, reason, utc_now_iso()))

    def list_binding_history(self, conn: sqlite3.Connection,
                             consumer_name: str | None = None) -> list[sqlite3.Row]:
        if consumer_name:
            return conn.execute(
                "SELECT * FROM consumer_binding_history WHERE consumer_name = ? ORDER BY id",
                (consumer_name,)).fetchall()
        return conn.execute(
            "SELECT * FROM consumer_binding_history ORDER BY id DESC").fetchall()

    # ------------------------------------------------------------------
    # 回放
    # ------------------------------------------------------------------

    def create_replay_run(self, conn: sqlite3.Connection, scope: str,
                          total: int, succeeded: int, failed: int) -> int:
        cur = conn.execute(
            """INSERT INTO replay_runs(scope, total, succeeded, failed, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (scope, total, succeeded, failed, utc_now_iso()))
        return int(cur.lastrowid)

    def add_replay_result(self, conn: sqlite3.Connection, run_id: int,
                          quarantine_id: int, event_id: str | None,
                          status: str, message: str, corrected: bool) -> None:
        conn.execute(
            """INSERT INTO replay_results(run_id, quarantine_id, event_id, status,
                                          message, corrected, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, quarantine_id, event_id, status, message,
             1 if corrected else 0, utc_now_iso()))

    def list_replay_runs(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM replay_runs ORDER BY id DESC").fetchall()

    def list_replay_results(self, conn: sqlite3.Connection,
                            run_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM replay_results WHERE run_id = ? ORDER BY id",
            (run_id,)).fetchall()

    # ------------------------------------------------------------------
    # 投递日志
    # ------------------------------------------------------------------

    def record_delivery(self, conn: sqlite3.Connection, event_id: str,
                        consumer_name: str, version: str, status: str,
                        message: str = "") -> bool:
        """幂等记录；返回 True 表示新写入，False 表示重复投递被跳过。"""
        try:
            conn.execute(
                """INSERT INTO delivery_log(event_id, consumer_name, version,
                                            status, message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event_id, consumer_name, version, status, message,
                 utc_now_iso()))
            return True
        except sqlite3.IntegrityError:
            return False

    def list_delivery_log(self, conn: sqlite3.Connection,
                          consumer_name: str | None = None) -> list[sqlite3.Row]:
        if consumer_name:
            return conn.execute(
                "SELECT * FROM delivery_log WHERE consumer_name = ? ORDER BY id DESC",
                (consumer_name,)).fetchall()
        return conn.execute(
            "SELECT * FROM delivery_log ORDER BY id DESC").fetchall()

    def has_delivery(self, conn: sqlite3.Connection, event_id: str,
                     consumer_name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM delivery_log WHERE event_id = ? AND consumer_name = ?",
            (event_id, consumer_name)).fetchone() is not None

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def count(self, conn: sqlite3.Connection, table: str,
              where: str = "", params: Iterable[Any] = ()) -> int:
        assert table in {
            "events", "converted_events", "quarantined", "consumers",
            "replay_runs", "delivery_log"}
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} {where}", tuple(params)).fetchone()
        return int(row["c"])


class _LockedConnection:
    """连接代理：进入时加锁，最外层 with 退出时提交；异常时回滚。

    服务方法存在嵌套 ``with``（如回放内部调用摄入），用线程本地深度计数，
    仅最外层负责提交与释放锁，保证嵌套事务语义正确。
    事务本身交给 sqlite3 模块的隐式事务管理（首个 DML 自动开启），
    避免与显式 BEGIN 冲突。
    """

    _depth = threading.local()

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        depth = getattr(self._depth, "value", 0)
        if depth == 0:
            self._lock.acquire()
        self._depth.value = depth + 1
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self._depth.value -= 1
        if self._depth.value == 0:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
            self._lock.release()

    def __getattr__(self, item):
        return getattr(self._conn, item)
