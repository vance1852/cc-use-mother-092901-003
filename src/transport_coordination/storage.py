"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 收费域：路段登记（归属于经营主体）
CREATE TABLE IF NOT EXISTS toll_segments (
    segment_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    rate_fen_per_km INTEGER NOT NULL CHECK(rate_fen_per_km >= 0),
    length_m INTEGER NOT NULL CHECK(length_m > 0),
    version INTEGER NOT NULL CHECK(version >= 1),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 收费域：路段不可变历史版本，供历史时点计费重放
CREATE TABLE IF NOT EXISTS toll_segment_versions (
    segment_id TEXT NOT NULL REFERENCES toll_segments(segment_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    organization_id TEXT NOT NULL,
    name TEXT NOT NULL,
    rate_fen_per_km INTEGER NOT NULL CHECK(rate_fen_per_km >= 0),
    length_m INTEGER NOT NULL CHECK(length_m > 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(segment_id, version)
);
-- 收费域：政策头（版本指针）与不可变政策版本
CREATE TABLE IF NOT EXISTS toll_policies (
    policy_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL,
    published_org TEXT NOT NULL,
    withdrawn_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS toll_policy_versions (
    policy_id TEXT NOT NULL REFERENCES toll_policies(policy_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    kind TEXT NOT NULL,
    priority INTEGER NOT NULL,
    scope_json TEXT NOT NULL,
    params_json TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL,
    withdrawn_at TEXT,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY(policy_id, version)
);
-- 收费域：行程与其经过的路段（计费时锁定路段版本）
CREATE TABLE IF NOT EXISTS toll_trips (
    trip_id TEXT PRIMARY KEY,
    vehicle_plate TEXT NOT NULL,
    vehicle_class TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_event_id TEXT,
    exit_event_id TEXT,
    current_fact_id TEXT,
    closed_at TEXT,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS toll_trip_segments (
    trip_id TEXT NOT NULL REFERENCES toll_trips(trip_id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    segment_id TEXT NOT NULL,
    segment_version INTEGER NOT NULL,
    PRIMARY KEY(trip_id, ordinal)
);
-- 收费域：入口/出口事件，只追加；补录使旧事件失活而不删除
CREATE TABLE IF NOT EXISTS toll_events (
    event_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES toll_trips(trip_id),
    kind TEXT NOT NULL CHECK(kind IN ('entry', 'exit', 'free_pass', 'closure_detour')),
    event_time TEXT NOT NULL,
    location TEXT,
    detail_json TEXT NOT NULL,
    evidence_hash TEXT,
    received_at TEXT NOT NULL,
    supersedes_event_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_toll_events_active_exit
    ON toll_events(trip_id) WHERE active = 1 AND kind = 'exit';
-- 收费域：不可重复的计费事实（输入版本摘要唯一）
CREATE TABLE IF NOT EXISTS toll_billing_facts (
    fact_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES toll_trips(trip_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL UNIQUE,
    total_fen INTEGER NOT NULL CHECK(total_fen >= 0),
    detail_json TEXT NOT NULL,
    basis_time TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(trip_id, version)
);
-- 收费域：守恒账簿分录（结算/退款/追缴/分账）
CREATE TABLE IF NOT EXISTS toll_ledger_entries (
    entry_id TEXT PRIMARY KEY,
    voucher_type TEXT NOT NULL,
    voucher_id TEXT NOT NULL,
    trip_id TEXT NOT NULL REFERENCES toll_trips(trip_id),
    organization_id TEXT,
    account TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('debit', 'credit')),
    amount_fen INTEGER NOT NULL CHECK(amount_fen >= 0),
    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
    dispute_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_toll_ledger_trip ON toll_ledger_entries(trip_id);
CREATE INDEX IF NOT EXISTS idx_toll_ledger_voucher ON toll_ledger_entries(voucher_type, voucher_id);
-- 收费域：争议只冻结相关分录
CREATE TABLE IF NOT EXISTS toll_disputes (
    dispute_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES toll_trips(trip_id),
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved', 'rejected')),
    reason TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
