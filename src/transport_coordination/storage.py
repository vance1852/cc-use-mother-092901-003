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
CREATE TABLE IF NOT EXISTS road_segments (
    segment_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    base_amount_fen INTEGER NOT NULL CHECK(base_amount_fen >= 0),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    kind TEXT NOT NULL CHECK(kind IN ('rate','cap','exemption','eligibility')),
    priority INTEGER NOT NULL,
    name TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    effect_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    published_by TEXT NOT NULL REFERENCES actors(actor_id),
    published_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdrawn_by TEXT,
    PRIMARY KEY(policy_id, version)
);
CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    vehicle_class TEXT NOT NULL,
    vehicle_tags_json TEXT NOT NULL,
    entry_segment_id TEXT,
    entry_plaza TEXT NOT NULL,
    entry_at TEXT NOT NULL,
    exit_at TEXT,
    exit_evidence_ref TEXT,
    path_json TEXT,
    is_holiday INTEGER NOT NULL DEFAULT 0 CHECK(is_holiday IN (0, 1)),
    evidence_ref TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending_evidence','billed','settled')),
    current_version INTEGER NOT NULL DEFAULT 0 CHECK(current_version >= 0),
    has_late_event INTEGER NOT NULL DEFAULT 0 CHECK(has_late_event IN (0, 1)),
    settlement_period_id TEXT,
    settled_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trip_events (
    event_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    event_occurred_at TEXT NOT NULL,
    event_local_at TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(trip_id, sequence)
);
CREATE TABLE IF NOT EXISTS billing_facts (
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    version INTEGER NOT NULL,
    base_total_fen INTEGER NOT NULL,
    final_total_fen INTEGER NOT NULL,
    trace_json TEXT NOT NULL,
    policy_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(trip_id, version)
);
CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    kind TEXT NOT NULL CHECK(kind IN ('refund','recovery')),
    amount_fen INTEGER NOT NULL CHECK(amount_fen > 0),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    fact_version INTEGER,
    segment_id TEXT REFERENCES road_segments(segment_id),
    adjustment_id TEXT REFERENCES adjustments(adjustment_id),
    operator_id TEXT NOT NULL REFERENCES organizations(organization_id),
    kind TEXT NOT NULL CHECK(kind IN ('charge','charge_reversal','refund','recovery')),
    amount_fen INTEGER NOT NULL,
    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
    period_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_trip ON ledger_entries(trip_id);
CREATE INDEX IF NOT EXISTS idx_ledger_operator ON ledger_entries(operator_id);
CREATE TABLE IF NOT EXISTS settlement_periods (
    period_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','closed')),
    cutoff_at TEXT,
    closed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    trip_id TEXT PRIMARY KEY REFERENCES trips(trip_id),
    period_id TEXT REFERENCES settlement_periods(period_id),
    fact_version INTEGER NOT NULL,
    total_fen INTEGER NOT NULL,
    settled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    reason TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_disputes_trip ON disputes(trip_id);
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
        self._migrate()

    def _migrate(self) -> None:
        """对旧版本数据库补充新增列，保持向前兼容。"""

        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(trip_events)")
        }
        if "event_local_at" not in columns:
            self.connection.execute("ALTER TABLE trip_events ADD COLUMN event_local_at TEXT")

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
