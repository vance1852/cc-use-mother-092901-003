"""收费政策执行与清算领域的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TollSegment:
    """表示归属于某一经营主体的收费路段。"""

    segment_id: str
    organization_id: str
    name: str
    rate_fen_per_km: int
    length_m: int
    version: int


@dataclass(frozen=True)
class PolicyVersion:
    """表示一条政策在某个版本上的不可变内容。"""

    policy_id: str
    version: int
    kind: str
    priority: int
    scope: dict[str, Any]
    params: dict[str, Any]
    effective_at: str
    expires_at: str | None
    status: str
    withdrawn_at: str | None
    published_by: str
    published_at: str


@dataclass(frozen=True)
class TollTrip:
    """表示一次通行及其补证状态。"""

    trip_id: str
    vehicle_plate: str
    vehicle_class: str
    declared_tags: tuple[str, ...]
    status: str
    entry_event_id: str | None
    exit_event_id: str | None
    opened_at: str
    updated_at: str


@dataclass(frozen=True)
class BillingFact:
    """表示按输入版本生成的不可重复计费事实。"""

    fact_id: str
    trip_id: str
    version: int
    status: str
    input_hash: str
    total_fen: int
    detail: dict[str, Any]
    basis_time: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class LedgerEntry:
    """表示守恒账簿中的一条分录。"""

    entry_id: str
    voucher_type: str
    voucher_id: str
    trip_id: str
    organization_id: str | None
    account: str
    direction: str
    amount_fen: int
    frozen: bool
    dispute_id: str | None
    created_at: str
