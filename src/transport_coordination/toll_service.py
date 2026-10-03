"""收费政策执行与清算服务。

在基础服务的权限、幂等、事务与哈希审计能力之上实现：

- 政策人员发布带生效区间与优先级的费率/折扣/封顶/豁免规则，版本不可变；
- 行程按锁定的路段版本、入口证据、出口事件生成输入摘要唯一的计费事实；
- 缺失出口进入待补证；迟到事件不得改写已关账收入；
- 政策撤回只影响尚未结算的行程；
- 结算、退款、追缴采用复式分录，借/贷逐分守恒；
- 争议只冻结该行程的分录；并支持按历史时点重放政策与清分结果。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService
from .toll_engine import POLICY_KINDS, build_input_hash, price_trip
from .toll_models import LedgerEntry, PolicyVersion, TollSegment, TollTrip

POLICY_STAFF = ("admin", "reviewer")
OPERATIONS_STAFF = ("admin", "operator")
DISPUTE_STAFF = ("admin", "operator", "reviewer")


def _voucher_action(voucher_type: str) -> str:
    return "refunded" if voucher_type == "refund" else "surcharged"


class TollService(DomainService):
    """承载收费清算业务，复用基础服务的鉴权、幂等与审计设施。"""

    # ----- 路段登记 -------------------------------------------------

    def register_segment(self, *, request_id: str, actor_id: str, segment_id: str,
                         organization_id: str, name: str, rate_fen_per_km: int,
                         length_m: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "segment_id": segment_id, "organization_id": organization_id,
                   "name": name, "rate_fen_per_km": rate_fen_per_km, "length_m": length_m}
        replayed = self._replay_check(request_id, "register_segment", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *OPERATIONS_STAFF)
            if actor.organization_id != organization_id and actor.role != "admin":
                raise PermissionDenied("不能为其他组织登记路段")
            segment_id = self._identifier(segment_id, "segment_id")
            name = self._text(name, "name")
            rate_fen_per_km = self._money(rate_fen_per_km, "rate_fen_per_km")
            length_m = self._positive_int(length_m, "length_m")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("经营主体不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO toll_segments(segment_id,organization_id,name,rate_fen_per_km,length_m,"
                        "version,active,created_at,updated_at) VALUES(?,?,?,?,?,1,1,?,?)",
                        (segment_id, organization_id, name, rate_fen_per_km, length_m, now, now),
                    )
                except Exception as exc:
                    raise ConflictError("路段编号已经存在") from exc
                connection.execute(
                    "INSERT INTO toll_segment_versions(segment_id,version,organization_id,name,"
                    "rate_fen_per_km,length_m,created_at) VALUES(?,?,?,?,?,?,?)",
                    (segment_id, 1, organization_id, name, rate_fen_per_km, length_m, now),
                )
                append_event(connection, actor_id=actor_id, action="toll_segment.registered",
                             resource_type="toll_segment", resource_id=segment_id,
                             detail={"organization_id": organization_id, "rate_fen_per_km": rate_fen_per_km,
                                     "length_m": length_m}, occurred_at=now)
                return "toll_segment", segment_id, {"segment_id": segment_id, "version": 1}

            return self._idem(connection, request_id=request_id, action="register_segment",
                              payload=payload, create=create)

    def update_segment(self, *, request_id: str, actor_id: str, segment_id: str,
                       rate_fen_per_km: int | None = None, length_m: int | None = None,
                       name: str | None = None) -> dict[str, Any]:
        """登记路段新版本；已生成的计费事实仍锁定旧版本。"""

        payload = {"actor_id": actor_id, "segment_id": segment_id, "rate_fen_per_km": rate_fen_per_km,
                   "length_m": length_m, "name": name}
        replayed = self._replay_check(request_id, "update_segment", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *OPERATIONS_STAFF)
            row = connection.execute("SELECT * FROM toll_segments WHERE segment_id=?", (segment_id,)).fetchone()
            if row is None:
                raise NotFoundError("路段不存在")
            if actor.organization_id != row["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能修改其他组织的路段")
            if rate_fen_per_km is None and length_m is None and name is None:
                raise ValidationError("至少提供一个需要更新的字段")
            new_rate = row["rate_fen_per_km"] if rate_fen_per_km is None else self._money(rate_fen_per_km, "rate_fen_per_km")
            new_length = row["length_m"] if length_m is None else self._positive_int(length_m, "length_m")
            new_name = row["name"] if name is None else self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                new_version = row["version"] + 1
                connection.execute(
                    "UPDATE toll_segments SET name=?,rate_fen_per_km=?,length_m=?,version=?,updated_at=? "
                    "WHERE segment_id=?",
                    (new_name, new_rate, new_length, new_version, now, segment_id),
                )
                connection.execute(
                    "INSERT INTO toll_segment_versions(segment_id,version,organization_id,name,"
                    "rate_fen_per_km,length_m,created_at) VALUES(?,?,?,?,?,?,?)",
                    (segment_id, new_version, row["organization_id"], new_name, new_rate, new_length, now),
                )
                append_event(connection, actor_id=actor_id, action="toll_segment.updated",
                             resource_type="toll_segment", resource_id=segment_id,
                             detail={"version": new_version, "rate_fen_per_km": new_rate,
                                     "length_m": new_length}, occurred_at=now)
                return "toll_segment", segment_id, {"segment_id": segment_id, "version": new_version}

            return self._idem(connection, request_id=request_id, action="update_segment",
                              payload=payload, create=create)

    # ----- 政策发布与撤回 -------------------------------------------

    def publish_policy(self, *, request_id: str, actor_id: str, policy_id: str, kind: str,
                       priority: int, scope: dict[str, Any], params: dict[str, Any],
                       effective_at: str, expires_at: str | None = None,
                       published_org: str | None = None) -> dict[str, Any]:
        return self._write_policy(request_id=request_id, actor_id=actor_id, policy_id=policy_id,
                                  kind=kind, priority=priority, scope=scope, params=params,
                                  effective_at=effective_at, expires_at=expires_at,
                                  published_org=published_org, new_policy=True)

    def new_policy_version(self, *, request_id: str, actor_id: str, policy_id: str,
                           priority: int, scope: dict[str, Any], params: dict[str, Any],
                           effective_at: str, expires_at: str | None = None) -> dict[str, Any]:
        return self._write_policy(request_id=request_id, actor_id=actor_id, policy_id=policy_id,
                                  kind=None, priority=priority, scope=scope, params=params,
                                  effective_at=effective_at, expires_at=expires_at,
                                  published_org=None, new_policy=False)

    def _write_policy(self, *, request_id: str, actor_id: str, policy_id: str, kind: str | None,
                      priority: int, scope: dict[str, Any], params: dict[str, Any],
                      effective_at: str, expires_at: str | None, published_org: str | None,
                      new_policy: bool) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "policy_id": policy_id, "kind": kind, "priority": priority,
                   "scope": scope, "params": params, "effective_at": effective_at,
                   "expires_at": expires_at, "published_org": published_org}
        replayed = self._replay_check(request_id, "publish_policy" if new_policy else "new_policy_version", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *POLICY_STAFF)
            policy_id = self._identifier(policy_id, "policy_id")
            header = connection.execute("SELECT * FROM toll_policies WHERE policy_id=?",
                                        (policy_id,)).fetchone()
            if new_policy:
                if header is not None:
                    raise ConflictError("政策编号已经存在")
                if kind not in POLICY_KINDS:
                    raise ValidationError("kind 必须是 rate/discount/cap/exemption")
            else:
                if header is None:
                    raise NotFoundError("政策不存在，应先发布")
                if header["status"] != "active":
                    raise ConflictError("已撤回的政策不能再发新版本")
                kind = header["kind"]
            priority = self._int_range(priority, "priority", -1_000_000, 1_000_000)
            scope = self._scope(scope)
            params = self._params(kind, params)
            effective_at = self._text(effective_at, "effective_at", 40)
            if expires_at is not None:
                expires_at = self._text(expires_at, "expires_at", 40)
                if expires_at <= effective_at:
                    raise ValidationError("expires_at 必须晚于 effective_at")
            if not new_policy and effective_at <= header_effective(connection, policy_id,
                                                                   header["current_version"]):
                raise ValidationError("新版本生效时间不得早于上一版本")
            if published_org is None:
                published_org = actor.organization_id
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (published_org,)).fetchone() is None:
                raise NotFoundError("政策发布机构不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                content_hash = digest({"kind": kind, "priority": priority, "scope": scope,
                                       "params": params, "effective_at": effective_at,
                                       "expires_at": expires_at, "published_org": published_org})
                if new_policy:
                    version = 1
                    connection.execute(
                        "INSERT INTO toll_policies(policy_id,kind,current_version,status,published_org,"
                        "created_by,created_at,updated_at) VALUES(?,?,1,'active',?,?,?,?)",
                        (policy_id, kind, published_org, actor_id, now, now),
                    )
                    action = "toll_policy.published"
                else:
                    version = header["current_version"] + 1
                    # 上一版本在新版本生效时点自动失效，保证同一时点仅一个版本有效
                    connection.execute(
                        "UPDATE toll_policy_versions SET expires_at=? WHERE policy_id=? AND version=? "
                        "AND expires_at IS NULL",
                        (effective_at, policy_id, header["current_version"]),
                    )
                    connection.execute(
                        "UPDATE toll_policies SET current_version=?,updated_at=? WHERE policy_id=?",
                        (version, now, policy_id),
                    )
                    action = "toll_policy.versioned"
                connection.execute(
                    "INSERT INTO toll_policy_versions(policy_id,version,kind,priority,scope_json,params_json,"
                    "effective_at,expires_at,status,withdrawn_at,published_by,published_at,content_hash) "
                    "VALUES(?,?,?,?,?,?,?,?,'active',NULL,?,?,?)",
                    (policy_id, version, kind, priority, canonical_json(scope), canonical_json(params),
                     effective_at, expires_at, actor_id, now, content_hash),
                )
                append_event(connection, actor_id=actor_id, action=action,
                             resource_type="toll_policy", resource_id=policy_id,
                             detail={"version": version, "kind": kind, "priority": priority,
                                     "effective_at": effective_at, "expires_at": expires_at,
                                     "content_hash": content_hash}, occurred_at=now)
                return "toll_policy", policy_id, {"policy_id": policy_id, "version": version}

            return self._idem(connection, request_id=request_id,
                              action="publish_policy" if new_policy else "new_policy_version",
                              payload=payload, create=create)

    def withdraw_policy(self, *, request_id: str, actor_id: str, policy_id: str) -> dict[str, Any]:
        """撤回政策：只对尚未结算的行程生效，已关账事实保持不变。"""

        payload = {"actor_id": actor_id, "policy_id": policy_id}
        replayed = self._replay_check(request_id, "withdraw_policy", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *POLICY_STAFF)
            header = connection.execute("SELECT * FROM toll_policies WHERE policy_id=?",
                                        (policy_id,)).fetchone()
            if header is None:
                raise NotFoundError("政策不存在")
            if header["status"] != "active":
                raise ConflictError("政策已处于撤回状态")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE toll_policies SET status='withdrawn',withdrawn_at=?,updated_at=? WHERE policy_id=?",
                    (now, now, policy_id),
                )
                connection.execute(
                    "UPDATE toll_policy_versions SET status='withdrawn',withdrawn_at=? WHERE policy_id=?",
                    (now, policy_id),
                )
                # 仅重新计费已产生计费事实但尚未关账的行程；
                # 纯开放/待补证行程在出口计费时自然适用新政，已结算收入不改写。
                rows = connection.execute(
                    "SELECT trip_id FROM toll_trips WHERE status != 'settled' "
                    "AND current_fact_id IS NOT NULL"
                ).fetchall()
                for row in rows:
                    self._rebill_open_trip(connection, row["trip_id"], actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="toll_policy.withdrawn",
                             resource_type="toll_policy", resource_id=policy_id,
                             detail={"rebilled_trips": [r["trip_id"] for r in rows]}, occurred_at=now)
                return "toll_policy", policy_id, {"policy_id": policy_id,
                                                  "rebilled_trips": [r["trip_id"] for r in rows]}

            return self._idem(connection, request_id=request_id, action="withdraw_policy",
                              payload=payload, create=create)

    # ----- 行程与事件 -----------------------------------------------

    def open_trip(self, *, request_id: str, actor_id: str, trip_id: str, vehicle_plate: str,
                  vehicle_class: str, segment_ids: list[str], entry_time: str,
                  entry_location: str | None = None, tags: list[str] | None = None,
                  entry_evidence_hash: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "trip_id": trip_id, "vehicle_plate": vehicle_plate,
                   "vehicle_class": vehicle_class, "segment_ids": segment_ids,
                   "entry_time": entry_time, "entry_location": entry_location, "tags": tags or []}
        replayed = self._replay_check(request_id, "open_trip", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *OPERATIONS_STAFF)
            trip_id = self._identifier(trip_id, "trip_id")
            vehicle_plate = self._text(vehicle_plate, "vehicle_plate", 32)
            vehicle_class = self._text(vehicle_class, "vehicle_class", 32)
            entry_time = self._text(entry_time, "entry_time", 40)
            tags = [self._text(t, "tag", 40) for t in (tags or [])]
            if not segment_ids:
                raise ValidationError("跨路段行程至少包含一个路段")
            locked: list[tuple[str, int]] = []
            for segment_id in segment_ids:
                row = connection.execute("SELECT * FROM toll_segments WHERE segment_id=? AND active=1",
                                         (segment_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"路段不存在或已停用: {segment_id}")
                if any(segment_id == s for s, _ in locked):
                    raise ValidationError("行程内路段不能重复")
                locked.append((segment_id, row["version"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                event_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO toll_trips(trip_id,vehicle_plate,vehicle_class,tags_json,status,"
                        "entry_event_id,exit_event_id,current_fact_id,closed_at,opened_at,updated_at) "
                        "VALUES(?,?,?,?,'open',?,NULL,NULL,NULL,?,?)",
                        (trip_id, vehicle_plate, vehicle_class, canonical_json(tags),
                         event_id, entry_time, now),
                    )
                except Exception as exc:
                    raise ConflictError("行程编号已经存在") from exc
                for ordinal, (segment_id, version) in enumerate(locked):
                    connection.execute(
                        "INSERT INTO toll_trip_segments(trip_id,ordinal,segment_id,segment_version) "
                        "VALUES(?,?,?,?)",
                        (trip_id, ordinal, segment_id, version),
                    )
                connection.execute(
                    "INSERT INTO toll_events(event_id,trip_id,kind,event_time,location,detail_json,"
                    "evidence_hash,received_at,supersedes_event_id,active) "
                    "VALUES(?,?,'entry',?,?,?,?,?,NULL,1)",
                    (event_id, trip_id, entry_time, entry_location,
                     canonical_json({"at_open": True}), entry_evidence_hash, now),
                )
                append_event(connection, actor_id=actor_id, action="toll_trip.opened",
                             resource_type="toll_trip", resource_id=trip_id,
                             detail={"segments": [{"segment_id": s, "version": v} for s, v in locked],
                                     "entry_event_id": event_id}, occurred_at=now)
                return "toll_trip", trip_id, {"trip_id": trip_id, "entry_event_id": event_id}

            return self._idem(connection, request_id=request_id, action="open_trip",
                              payload=payload, create=create)

    def mark_pending_evidence(self, *, request_id: str, actor_id: str, trip_id: str) -> dict[str, Any]:
        """入口已有、出口缺失，进入待补证状态。"""

        payload = {"actor_id": actor_id, "trip_id": trip_id}
        replayed = self._replay_check(request_id, "mark_pending_evidence", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor, trip = self._load_trip_for_ops(connection, actor_id, trip_id)
            if trip["status"] != "open":
                raise ConflictError("只有缺少出口证据的开放行程可以转入待补证")
            if self._active_event(connection, trip_id, "exit") is not None:
                raise ConflictError("已有出口事件，无需补证")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE toll_trips SET status='pending_evidence',updated_at=? WHERE trip_id=?",
                    (now, trip_id),
                )
                append_event(connection, actor_id=actor_id, action="toll_trip.pending_evidence",
                             resource_type="toll_trip", resource_id=trip_id, detail={}, occurred_at=now)
                return "toll_trip", trip_id, {"trip_id": trip_id, "status": "pending_evidence"}

            return self._idem(connection, request_id=request_id, action="mark_pending_evidence",
                              payload=payload, create=create)

    def record_exit(self, *, request_id: str, actor_id: str, trip_id: str, event_time: str,
                    event_id: str | None = None, location: str | None = None,
                    evidence_hash: str | None = None, supersedes_event_id: str | None = None) -> dict[str, Any]:
        """登记出口事件并触发计费；已关账行程的迟到出口不得改写收入。"""

        payload = {"actor_id": actor_id, "trip_id": trip_id, "event_time": event_time,
                   "event_id": event_id, "supersedes_event_id": supersedes_event_id}
        replayed = self._replay_check(request_id, "record_exit", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor, trip = self._load_trip_for_ops(connection, actor_id, trip_id)
            if trip["status"] == "settled":
                raise ConflictError("行程已关账，迟到出口事件不能改写收入；请走退款/追缴凭证")
            event_time = self._text(event_time, "event_time", 40)
            entry = self._active_event(connection, trip_id, "entry")
            if entry is not None and event_time < entry["event_time"]:
                raise ValidationError("出口时间不能早于入口时间")
            old_exit = self._active_event(connection, trip_id, "exit")
            if old_exit is not None and trip["status"] == "billed" and supersedes_event_id is None:
                raise ConflictError("已存在有效出口事件；补录必须声明被取代的事件")
            if supersedes_event_id is not None:
                old = connection.execute("SELECT * FROM toll_events WHERE event_id=? AND trip_id=?",
                                         (supersedes_event_id, trip_id)).fetchone()
                if old is None or old["kind"] != "exit":
                    raise NotFoundError("被取代的出口事件不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                new_event_id = self._add_event(connection, trip_id=trip_id, kind="exit",
                                               event_time=event_time, location=location,
                                               detail={}, evidence_hash=evidence_hash,
                                               received_at=now, event_id=event_id,
                                               supersedes_event_id=supersedes_event_id)
                fact = self._bill(connection, trip_id, actor_id=actor_id)
                connection.execute(
                    "UPDATE toll_trips SET status='billed',exit_event_id=?,current_fact_id=?,"
                    "updated_at=? WHERE trip_id=?",
                    (new_event_id, fact["fact_id"], now, trip_id),
                )
                append_event(connection, actor_id=actor_id, action="toll_trip.exit_recorded",
                             resource_type="toll_trip", resource_id=trip_id,
                             detail={"event_id": new_event_id, "fact_id": fact["fact_id"],
                                     "total_fen": fact["total_fen"]}, occurred_at=now)
                return "toll_trip", trip_id, {"trip_id": trip_id, "exit_event_id": new_event_id,
                                              "fact_id": fact["fact_id"], "total_fen": fact["total_fen"]}

            return self._idem(connection, request_id=request_id, action="record_exit",
                              payload=payload, create=create)

    def record_free_pass(self, *, request_id: str, actor_id: str, trip_id: str, event_time: str,
                         borne_organization_id: str, reason: str,
                         event_id: str | None = None, evidence_hash: str | None = None) -> dict[str, Any]:
        """登记临时免费事件（优惠由指定机构承担），随后按事件重算。"""

        payload = {"actor_id": actor_id, "trip_id": trip_id, "event_time": event_time,
                   "borne_organization_id": borne_organization_id, "reason": reason}
        replayed = self._replay_check(request_id, "record_free_pass", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor, trip = self._load_trip_for_ops(connection, actor_id, trip_id)
            if trip["status"] == "settled":
                raise ConflictError("行程已关账，临时免费事件不能改写收入；请走退款凭证")
            event_time = self._text(event_time, "event_time", 40)
            reason = self._text(reason, "reason")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (borne_organization_id,)).fetchone() is None:
                raise NotFoundError("优惠承担机构不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                new_event_id = self._add_event(connection, trip_id=trip_id, kind="free_pass",
                                               event_time=event_time, location=None,
                                               detail={"borne_organization_id": borne_organization_id,
                                                       "reason": reason},
                                               evidence_hash=evidence_hash, received_at=now,
                                               event_id=event_id)
                fact = self._bill(connection, trip_id, actor_id=actor_id)
                connection.execute(
                    "UPDATE toll_trips SET current_fact_id=?,updated_at=? WHERE trip_id=?",
                    (fact["fact_id"], now, trip_id),
                )
                append_event(connection, actor_id=actor_id, action="toll_trip.free_pass_recorded",
                             resource_type="toll_trip", resource_id=trip_id,
                             detail={"event_id": new_event_id, "fact_id": fact["fact_id"],
                                     "borne_organization_id": borne_organization_id}, occurred_at=now)
                return "toll_trip", trip_id, {"trip_id": trip_id, "event_id": new_event_id,
                                              "fact_id": fact["fact_id"], "total_fen": fact["total_fen"]}

            return self._idem(connection, request_id=request_id, action="record_free_pass",
                              payload=payload, create=create)

    def record_detour(self, *, request_id: str, actor_id: str, trip_id: str, event_time: str,
                      add_segment_ids: list[str] | None = None,
                      remove_segment_ids: list[str] | None = None, reason: str = "",
                      evidence_hash: str | None = None) -> dict[str, Any]:
        """封路绕行：在计费前增删行程路段（版本按登记时刻锁定）。"""

        payload = {"actor_id": actor_id, "trip_id": trip_id, "event_time": event_time,
                   "add_segment_ids": add_segment_ids or [], "remove_segment_ids": remove_segment_ids or [],
                   "reason": reason}
        replayed = self._replay_check(request_id, "record_detour", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor, trip = self._load_trip_for_ops(connection, actor_id, trip_id)
            if trip["status"] == "settled":
                raise ConflictError("行程已关账，封路绕行不能改写收入；请走退款/追缴凭证")
            event_time = self._text(event_time, "event_time", 40)
            reason = self._text(reason, "reason")
            add_segment_ids = add_segment_ids or []
            remove_segment_ids = remove_segment_ids or []
            if not add_segment_ids and not remove_segment_ids:
                raise ValidationError("绕行调整必须声明增加或移除的路段")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                rows = connection.execute(
                    "SELECT segment_id,segment_version FROM toll_trip_segments WHERE trip_id=? ORDER BY ordinal",
                    (trip_id,)).fetchall()
                current = [(r["segment_id"], r["segment_version"]) for r in rows]
                for segment_id in remove_segment_ids:
                    if not any(segment_id == s for s, _ in current):
                        raise NotFoundError(f"行程中不存在该路段: {segment_id}")
                    current = [(s, v) for s, v in current if s != segment_id]
                additions = []
                for segment_id in add_segment_ids:
                    row = connection.execute("SELECT * FROM toll_segments WHERE segment_id=? AND active=1",
                                             (segment_id,)).fetchone()
                    if row is None:
                        raise NotFoundError(f"路段不存在或已停用: {segment_id}")
                    if any(segment_id == s for s, _ in current):
                        raise ConflictError(f"路段已在行程中: {segment_id}")
                    current.append((segment_id, row["version"]))
                    additions.append({"segment_id": segment_id, "version": row["version"]})
                if not current:
                    raise ValidationError("绕行后行程至少保留一个路段")
                connection.execute("DELETE FROM toll_trip_segments WHERE trip_id=?", (trip_id,))
                for ordinal, (segment_id, version) in enumerate(current):
                    connection.execute(
                        "INSERT INTO toll_trip_segments(trip_id,ordinal,segment_id,segment_version) "
                        "VALUES(?,?,?,?)", (trip_id, ordinal, segment_id, version),
                    )
                event_id = self._add_event(connection, trip_id=trip_id, kind="closure_detour",
                                           event_time=event_time, location=None,
                                           detail={"add": additions, "remove": remove_segment_ids,
                                                   "reason": reason},
                                           evidence_hash=evidence_hash, received_at=now)
                fact = None
                if trip["status"] == "billed":
                    fact = self._bill(connection, trip_id, actor_id=actor_id)
                    connection.execute(
                        "UPDATE toll_trips SET current_fact_id=?,updated_at=? WHERE trip_id=?",
                        (fact["fact_id"], now, trip_id),
                    )
                else:
                    connection.execute("UPDATE toll_trips SET updated_at=? WHERE trip_id=?", (now, trip_id))
                append_event(connection, actor_id=actor_id, action="toll_trip.detour_recorded",
                             resource_type="toll_trip", resource_id=trip_id,
                             detail={"event_id": event_id, "add": additions,
                                     "remove": remove_segment_ids,
                                     "fact_id": fact["fact_id"] if fact else None}, occurred_at=now)
                return "toll_trip", trip_id, {"trip_id": trip_id, "event_id": event_id,
                                              "fact_id": fact["fact_id"] if fact else None}

            return self._idem(connection, request_id=request_id, action="record_detour",
                              payload=payload, create=create)

    # ----- 结算、退款、追缴、争议 ------------------------------------

    def settle_trip(self, *, request_id: str, actor_id: str, trip_id: str) -> dict[str, Any]:
        """对已计费行程关账：收入归路段运营方，优惠由政策/豁免发布机构承担。"""

        payload = {"actor_id": actor_id, "trip_id": trip_id}
        replayed = self._replay_check(request_id, "settle_trip", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor, trip = self._load_trip_for_ops(connection, actor_id, trip_id)
            if trip["status"] != "billed":
                raise ConflictError("只有已计费（已取得出口）的行程可以关账")
            fact = self._get_fact(connection, trip["current_fact_id"])
            bearers = self._discount_bearers(connection, fact)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                voucher_id = uuid.uuid4().hex
                gross_total = 0
                for line in self._settlement_lines(fact, bearers):
                    gross_total += line["gross_fen"]
                    self._ledger(connection, voucher_type="settlement", voucher_id=voucher_id,
                                 trip_id=trip_id, organization_id=line["organization_id"],
                                 account="revenue", direction="credit",
                                 amount_fen=line["revenue_fen"], created_at=now)
                    for org_id, amount in line["discount_borne"]:
                        self._ledger(connection, voucher_type="settlement", voucher_id=voucher_id,
                                     trip_id=trip_id, organization_id=org_id,
                                     account="discount_grant", direction="credit",
                                     amount_fen=amount, created_at=now)
                self._ledger(connection, voucher_type="settlement", voucher_id=voucher_id,
                             trip_id=trip_id, organization_id=None, account="receivable",
                             direction="debit", amount_fen=gross_total, created_at=now)
                self._assert_balanced(connection, voucher_id, "settlement")
                connection.execute(
                    "UPDATE toll_trips SET status='settled',closed_at=?,updated_at=? WHERE trip_id=?",
                    (now, now, trip_id),
                )
                append_event(connection, actor_id=actor_id, action="toll_trip.settled",
                             resource_type="toll_trip", resource_id=trip_id,
                             detail={"voucher_id": voucher_id, "fact_id": fact["fact_id"],
                                     "gross_fen": gross_total, "total_fen": fact["total_fen"]},
                             occurred_at=now)
                return "toll_settlement", voucher_id, {"trip_id": trip_id, "voucher_id": voucher_id,
                                                       "gross_fen": gross_total,
                                                       "total_fen": fact["total_fen"]}

            return self._idem(connection, request_id=request_id, action="settle_trip",
                              payload=payload, create=create)

    def create_refund(self, *, request_id: str, actor_id: str, trip_id: str, amount_fen: int,
                      reason: str, dispute_id: str | None = None) -> dict[str, Any]:
        return self._adjustment(request_id=request_id, actor_id=actor_id, trip_id=trip_id,
                                amount_fen=amount_fen, reason=reason, dispute_id=dispute_id,
                                voucher_type="refund")

    def create_surcharge(self, *, request_id: str, actor_id: str, trip_id: str, amount_fen: int,
                         reason: str) -> dict[str, Any]:
        return self._adjustment(request_id=request_id, actor_id=actor_id, trip_id=trip_id,
                                amount_fen=amount_fen, reason=reason, dispute_id=None,
                                voucher_type="surcharge")

    def _adjustment(self, *, request_id: str, actor_id: str, trip_id: str, amount_fen: int,
                    reason: str, dispute_id: str | None, voucher_type: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "trip_id": trip_id, "amount_fen": amount_fen,
                   "reason": reason, "dispute_id": dispute_id, "voucher_type": voucher_type}
        replayed = self._replay_check(request_id, f"create_{voucher_type}", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor, trip = self._load_trip_for_ops(connection, actor_id, trip_id)
            if trip["status"] != "settled":
                raise ConflictError("只有已关账行程可以办理退款/追缴")
            amount_fen = self._positive_int(amount_fen, "amount_fen")
            reason = self._text(reason, "reason")
            if dispute_id is not None:
                dispute = connection.execute("SELECT * FROM toll_disputes WHERE dispute_id=? AND trip_id=?",
                                             (dispute_id, trip_id)).fetchone()
                if dispute is None:
                    raise NotFoundError("争议不存在")
                if dispute["status"] != "open":
                    raise ConflictError("争议已处理完毕")
            if voucher_type == "refund":
                # 退款池：普通退款只能动用未冻结收入；关联争议时使用该争议冻结的收入。
                if dispute_id is None:
                    pool_query = (
                        "SELECT organization_id, SUM(amount_fen) AS amount FROM toll_ledger_entries "
                        "WHERE trip_id=? AND voucher_type='settlement' AND account='revenue' "
                        "AND direction='credit' AND frozen=0 GROUP BY organization_id")
                    pool_params: tuple[Any, ...] = (trip_id,)
                else:
                    pool_query = (
                        "SELECT organization_id, SUM(amount_fen) AS amount FROM toll_ledger_entries "
                        "WHERE trip_id=? AND voucher_type='settlement' AND account='revenue' "
                        "AND direction='credit' AND frozen=1 AND dispute_id=? GROUP BY organization_id")
                    pool_params = (trip_id, dispute_id)
                pool_rows = connection.execute(pool_query, pool_params).fetchall()
                pool = {row["organization_id"]: row["amount"] for row in pool_rows}
                if amount_fen > sum(pool.values()):
                    raise ConflictError(f"退款金额超过可分配收入 {sum(pool.values())} 分")
                allocations = self._allocate(pool, amount_fen)
            else:
                # 追缴：新增应收，按各运营方结算收入占比分配（不受既有收入上限约束）。
                weight_rows = connection.execute(
                    "SELECT organization_id, SUM(amount_fen) AS amount FROM toll_ledger_entries "
                    "WHERE trip_id=? AND voucher_type='settlement' AND account='revenue' AND direction='credit' "
                    "GROUP BY organization_id", (trip_id,)).fetchall()
                weights = {row["organization_id"]: row["amount"] for row in weight_rows}
                if not weights:
                    raise ConflictError("行程没有结算收入，无法确定追缴分摊比例")
                allocations = self._allocate_proportional(weights, amount_fen)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                voucher_id = uuid.uuid4().hex
                for org_id, amount in allocations:
                    self._ledger(connection, voucher_type=voucher_type, voucher_id=voucher_id,
                                 trip_id=trip_id, organization_id=org_id, account="adjustment",
                                 direction="debit" if voucher_type == "refund" else "credit",
                                 amount_fen=amount, created_at=now, dispute_id=dispute_id)
                self._ledger(connection, voucher_type=voucher_type, voucher_id=voucher_id,
                             trip_id=trip_id, organization_id=None,
                             account="refund_payable" if voucher_type == "refund" else "surcharge_receivable",
                             direction="credit" if voucher_type == "refund" else "debit",
                             amount_fen=amount_fen, created_at=now, dispute_id=dispute_id)
                self._assert_balanced(connection, voucher_id, voucher_type)
                append_event(connection, actor_id=actor_id,
                             action=f"toll_trip.{_voucher_action(voucher_type)}",
                             resource_type="toll_trip", resource_id=trip_id,
                             detail={"voucher_id": voucher_id, "amount_fen": amount_fen,
                                     "allocations": allocations, "reason": reason,
                                     "dispute_id": dispute_id}, occurred_at=now)
                return f"toll_{voucher_type}", voucher_id, {"trip_id": trip_id, "voucher_id": voucher_id,
                                                            "amount_fen": amount_fen,
                                                            "allocations": [
                                                                {"organization_id": o, "amount_fen": a}
                                                                for o, a in allocations]}

            return self._idem(connection, request_id=request_id, action=f"create_{voucher_type}",
                              payload=payload, create=create)

    def open_dispute(self, *, request_id: str, actor_id: str, dispute_id: str, trip_id: str,
                     reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "trip_id": trip_id, "reason": reason}
        replayed = self._replay_check(request_id, "open_dispute", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DISPUTE_STAFF)
            trip = connection.execute("SELECT * FROM toll_trips WHERE trip_id=?", (trip_id,)).fetchone()
            if trip is None:
                raise NotFoundError("行程不存在")
            dispute_id = self._identifier(dispute_id, "dispute_id")
            reason = self._text(reason, "reason")
            existing = connection.execute("SELECT 1 FROM toll_disputes WHERE trip_id=? AND status='open'",
                                          (trip_id,)).fetchone()
            if existing is not None:
                raise ConflictError("该行程已有未处理争议")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO toll_disputes(dispute_id,trip_id,status,reason,opened_by,opened_at) "
                        "VALUES(?,?,'open',?,?,?)",
                        (dispute_id, trip_id, reason, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("争议编号已经存在") from exc
                # 只冻结本行程的分录，其他行程与主体不受影响。
                frozen = connection.execute(
                    "UPDATE toll_ledger_entries SET frozen=1,dispute_id=? WHERE trip_id=? AND frozen=0",
                    (dispute_id, trip_id),
                ).rowcount
                append_event(connection, actor_id=actor_id, action="toll_dispute.opened",
                             resource_type="toll_dispute", resource_id=dispute_id,
                             detail={"trip_id": trip_id, "frozen_entries": frozen}, occurred_at=now)
                return "toll_dispute", dispute_id, {"dispute_id": dispute_id, "frozen_entries": frozen}

            return self._idem(connection, request_id=request_id, action="open_dispute",
                              payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        resolution: str, refund_fen: int = 0) -> dict[str, Any]:
        """驳回则解冻；成立则可按冻结收入办理退款后解冻。"""

        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "resolution": resolution,
                   "refund_fen": refund_fen}
        replayed = self._replay_check(request_id, "resolve_dispute", payload)
        if replayed is not None:
            return replayed
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DISPUTE_STAFF)
            dispute = connection.execute("SELECT * FROM toll_disputes WHERE dispute_id=?",
                                         (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("争议不存在")
            if dispute["status"] != "open":
                raise ConflictError("争议已处理完毕")
            if resolution not in ("upheld", "rejected"):
                raise ValidationError("resolution 必须是 upheld 或 rejected")
            refund_fen = self._money(refund_fen, "refund_fen")
            refund_allocations: list[tuple[str, int]] = []
            if resolution == "upheld" and refund_fen:
                rows = connection.execute(
                    "SELECT organization_id,SUM(amount_fen) AS amount FROM toll_ledger_entries "
                    "WHERE trip_id=? AND voucher_type='settlement' AND account='revenue' AND direction='credit' "
                    "AND dispute_id=? GROUP BY organization_id",
                    (dispute["trip_id"], dispute_id),
                ).fetchall()
                pool = {r["organization_id"]: r["amount"] for r in rows}
                if refund_fen > sum(pool.values()):
                    raise ConflictError("退款金额超过争议冻结的收入")
                refund_allocations = self._allocate(pool, refund_fen)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                voucher_id = None
                if refund_allocations:
                    voucher_id = uuid.uuid4().hex
                    for org_id, amount in refund_allocations:
                        self._ledger(connection, voucher_type="refund", voucher_id=voucher_id,
                                     trip_id=dispute["trip_id"], organization_id=org_id,
                                     account="adjustment", direction="debit", amount_fen=amount,
                                     created_at=now, dispute_id=dispute_id)
                    self._ledger(connection, voucher_type="refund", voucher_id=voucher_id,
                                 trip_id=dispute["trip_id"], organization_id=None,
                                 account="refund_payable", direction="credit",
                                 amount_fen=refund_fen, created_at=now, dispute_id=dispute_id)
                    self._assert_balanced(connection, voucher_id, "refund")
                connection.execute(
                    "UPDATE toll_ledger_entries SET frozen=0 WHERE dispute_id=?", (dispute_id,)
                )
                connection.execute(
                    "UPDATE toll_disputes SET status=?,resolved_at=? WHERE dispute_id=?",
                    ("resolved" if resolution == "upheld" else "rejected", now, dispute_id),
                )
                append_event(connection, actor_id=actor_id, action="toll_dispute.resolved",
                             resource_type="toll_dispute", resource_id=dispute_id,
                             detail={"resolution": resolution, "refund_voucher_id": voucher_id,
                                     "refund_fen": refund_fen}, occurred_at=now)
                return "toll_dispute", dispute_id, {"dispute_id": dispute_id, "resolution": resolution,
                                                    "refund_voucher_id": voucher_id,
                                                    "refund_fen": refund_fen}

            return self._idem(connection, request_id=request_id, action="resolve_dispute",
                              payload=payload, create=create)

    # ----- 计费内核（存储装配）--------------------------------------

    def _bill(self, connection, trip_id: str, *, actor_id: str) -> dict[str, Any]:
        trip = connection.execute("SELECT * FROM toll_trips WHERE trip_id=?", (trip_id,)).fetchone()
        exit_event = self._active_event(connection, trip_id, "exit")
        segment_rows = connection.execute(
            "SELECT tsv.segment_id,tsv.version,tsv.organization_id,tsv.rate_fen_per_km,tsv.length_m "
            "FROM toll_trip_segments ts "
            "JOIN toll_segment_versions tsv ON tsv.segment_id=ts.segment_id "
            "AND tsv.version=ts.segment_version WHERE ts.trip_id=? ORDER BY ts.ordinal",
            (trip_id,)).fetchall()
        segments = [{"segment_id": r["segment_id"], "version": r["version"],
                     "organization_id": r["organization_id"],
                     "rate_fen_per_km": r["rate_fen_per_km"], "length_m": r["length_m"]}
                    for r in segment_rows]
        basis_time = exit_event["event_time"] if exit_event is not None else self._provisional_basis(
            connection, trip_id)
        policies = self._policies_at(connection, basis_time)
        vehicle = {"vehicle_class": trip["vehicle_class"], "tags": json.loads(trip["tags_json"])}
        detail = price_trip(segments=segments, policies=policies, vehicle=vehicle, basis_time=basis_time)

        # 临时免费：残余应收全部免除，承担方记为事件指定机构。
        waiver = self._active_event(connection, trip_id, "free_pass")
        if waiver is not None:
            waiver_detail = json.loads(waiver["detail_json"])
            borne = waiver_detail["borne_organization_id"]
            waived = detail["total_fen"]
            for item in detail["per_segment"]:
                if item["final_fen"]:
                    item.setdefault("waivers", []).append(
                        {"borne_organization_id": borne, "amount_fen": item["final_fen"],
                         "event_id": waiver["event_id"], "reason": waiver_detail.get("reason", "")})
                    item["discount_fen"] = item["gross_fen"]
                    item["final_fen"] = 0
            detail["waiver"] = {"event_id": waiver["event_id"], "borne_organization_id": borne,
                                "amount_fen": waived, "reason": waiver_detail.get("reason", "")}
            detail["total_fen"] = 0
            detail["total_discount_fen"] = detail["total_gross_fen"]

        extra_events = [r["event_id"] for r in connection.execute(
            "SELECT event_id FROM toll_events WHERE trip_id=? AND kind IN ('free_pass','closure_detour') "
            "ORDER BY event_time,received_at", (trip_id,))]
        applied_keys = {(ref["policy_id"], ref["version"])
                        for ref in detail.get("policy_versions", [])}
        applied_policies = [{"policy_id": p["policy_id"], "version": p["version"]}
                            for p in policies
                            if (p["policy_id"], p["version"]) in applied_keys]
        input_hash = build_input_hash(trip_id=trip_id, segments=segments,
                                      applied_policies=applied_policies,
                                      vehicle=vehicle, basis_time=basis_time,
                                      extra_event_ids=extra_events)
        # 输入版本完全相同则复用事实：计费事实不可重复。
        existing = connection.execute("SELECT * FROM toll_billing_facts WHERE input_hash=?",
                                      (input_hash,)).fetchone()
        if existing is not None:
            return self._fact_dict(existing)
        version_row = connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS version FROM toll_billing_facts WHERE trip_id=?",
            (trip_id,)).fetchone()
        fact_id = uuid.uuid4().hex
        now = self._now()
        status = "final" if exit_event is not None else "provisional"
        connection.execute(
            "INSERT INTO toll_billing_facts(fact_id,trip_id,version,status,input_hash,total_fen,"
            "detail_json,basis_time,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fact_id, trip_id, version_row["version"], status, input_hash, detail["total_fen"],
             canonical_json(detail), basis_time, actor_id, now),
        )
        return {"fact_id": fact_id, "trip_id": trip_id, "version": version_row["version"],
                "status": status, "input_hash": input_hash, "total_fen": detail["total_fen"],
                "detail": detail, "basis_time": basis_time, "created_by": actor_id, "created_at": now}

    def _rebill_open_trip(self, connection, trip_id: str, *, actor_id: str) -> dict[str, Any] | None:
        """政策撤回后为未关账行程重算，并切换当前事实指针。"""

        trip = connection.execute("SELECT * FROM toll_trips WHERE trip_id=?", (trip_id,)).fetchone()
        if trip is None or trip["status"] not in ("open", "pending_evidence", "billed"):
            return None
        fact = self._bill(connection, trip_id, actor_id=actor_id)
        connection.execute("UPDATE toll_trips SET current_fact_id=?,updated_at=? WHERE trip_id=?",
                           (fact["fact_id"], self._now(), trip_id))
        return fact

    # ----- 查询、解释与历史重放 -------------------------------------

    def get_trip(self, trip_id: str) -> TollTrip:
        row = self.database.connection.execute("SELECT * FROM toll_trips WHERE trip_id=?",
                                               (trip_id,)).fetchone()
        if row is None:
            raise NotFoundError("行程不存在")
        return TollTrip(row["trip_id"], row["vehicle_plate"], row["vehicle_class"],
                        tuple(json.loads(row["tags_json"])), row["status"],
                        row["entry_event_id"], row["exit_event_id"], row["opened_at"], row["updated_at"])

    def explain_trip(self, trip_id: str) -> dict[str, Any]:
        """说明一次通行为什么得到当前金额，以及每个经营主体承担的调整。"""

        connection = self.database.connection
        trip = connection.execute("SELECT * FROM toll_trips WHERE trip_id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFoundError("行程不存在")
        fact = self._get_fact(connection, trip["current_fact_id"]) if trip["current_fact_id"] else None
        bearers = self._discount_bearers(connection, fact) if fact else {}
        settlement, adjustments, disputes = self._ledger_view(connection, trip_id)
        return {
            "trip": asdict(self.get_trip(trip_id)),
            "events": self._events(connection, trip_id),
            "locked_segments": self._locked_segments(connection, trip_id),
            "billing": self._fact_view(fact, bearers) if fact else None,
            "settlement": settlement,
            "adjustments": adjustments,
            "disputes": disputes,
            "organization_shares": self._organization_shares(connection, trip_id),
        }

    def replay_at(self, trip_id: str, as_of: str) -> dict[str, Any]:
        """按历史时点重放：当时可见的计费事实版本与清分分录、冻结状态。"""

        as_of = self._text(as_of, "as_of", 40)
        connection = self.database.connection
        trip = connection.execute("SELECT * FROM toll_trips WHERE trip_id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFoundError("行程不存在")
        fact_row = connection.execute(
            "SELECT * FROM toll_billing_facts WHERE trip_id=? AND created_at<=? ORDER BY version DESC LIMIT 1",
            (trip_id, as_of),
        ).fetchone()
        fact = self._fact_dict(fact_row) if fact_row else None
        bearers = self._discount_bearers(connection, fact) if fact else {}
        entries = []
        for row in connection.execute(
            "SELECT * FROM toll_ledger_entries WHERE trip_id=? AND created_at<=? ORDER BY created_at,entry_id",
            (trip_id, as_of),
        ):
            dispute_open = row["dispute_id"] is not None and connection.execute(
                "SELECT 1 FROM toll_disputes WHERE dispute_id=? AND opened_at<=? "
                "AND (resolved_at IS NULL OR resolved_at>?)",
                (row["dispute_id"], as_of, as_of),
            ).fetchone() is not None
            entries.append({"entry_id": row["entry_id"], "voucher_type": row["voucher_type"],
                            "voucher_id": row["voucher_id"], "organization_id": row["organization_id"],
                            "account": row["account"], "direction": row["direction"],
                            "amount_fen": row["amount_fen"], "frozen": bool(dispute_open),
                            "dispute_id": row["dispute_id"], "created_at": row["created_at"]})
        has_settlement = any(e["voucher_type"] == "settlement" for e in entries)
        status_at = "settled" if has_settlement else ("billed" if fact else "open")
        return {"trip_id": trip_id, "as_of": as_of, "trip_status_at": status_at,
                "billing": self._fact_view(fact, bearers) if fact else None,
                "ledger_entries": entries}

    def replay_pricing(self, trip_id: str, basis_time: str) -> dict[str, Any]:
        """不落库地按任意历史时点重算当时有效政策下的金额（供审计比对）。"""

        basis_time = self._text(basis_time, "basis_time", 40)
        connection = self.database.connection
        trip = connection.execute("SELECT * FROM toll_trips WHERE trip_id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFoundError("行程不存在")
        segment_rows = connection.execute(
            "SELECT tsv.segment_id,tsv.version,tsv.organization_id,tsv.rate_fen_per_km,tsv.length_m "
            "FROM toll_trip_segments ts "
            "JOIN toll_segment_versions tsv ON tsv.segment_id=ts.segment_id "
            "AND tsv.version=ts.segment_version WHERE ts.trip_id=? ORDER BY ts.ordinal",
            (trip_id,)).fetchall()
        segments = [{"segment_id": r["segment_id"], "version": r["version"],
                     "organization_id": r["organization_id"],
                     "rate_fen_per_km": r["rate_fen_per_km"], "length_m": r["length_m"]}
                    for r in segment_rows]
        policies = self._policies_at(connection, basis_time)
        vehicle = {"vehicle_class": trip["vehicle_class"], "tags": json.loads(trip["tags_json"])}
        return price_trip(segments=segments, policies=policies, vehicle=vehicle, basis_time=basis_time)

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        connection = self.database.connection
        header = connection.execute("SELECT * FROM toll_policies WHERE policy_id=?",
                                    (policy_id,)).fetchone()
        if header is None:
            raise NotFoundError("政策不存在")
        versions = [asdict(self._version_dict(row)) for row in connection.execute(
            "SELECT * FROM toll_policy_versions WHERE policy_id=? ORDER BY version", (policy_id,))]
        return {"policy_id": policy_id, "kind": header["kind"], "status": header["status"],
                "current_version": header["current_version"], "published_org": header["published_org"],
                "withdrawn_at": header["withdrawn_at"], "versions": versions}

    def list_policies(self, active_at: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        result = []
        for row in connection.execute("SELECT * FROM toll_policies ORDER BY policy_id"):
            current = connection.execute(
                "SELECT * FROM toll_policy_versions WHERE policy_id=? AND version=?",
                (row["policy_id"], row["current_version"])).fetchone()
            if active_at is not None:
                from .toll_engine import active_policy
                if not active_policy(
                        {"status": current["status"], "effective_at": current["effective_at"],
                         "expires_at": current["expires_at"]}, active_at):
                    continue
            result.append(self.get_policy(row["policy_id"]))
        return result

    def trip_ledger(self, trip_id: str) -> list[LedgerEntry]:
        rows = self.database.connection.execute(
            "SELECT * FROM toll_ledger_entries WHERE trip_id=? ORDER BY created_at,entry_id", (trip_id,)
        ).fetchall()
        return [LedgerEntry(r["entry_id"], r["voucher_type"], r["voucher_id"], r["trip_id"],
                            r["organization_id"], r["account"], r["direction"], r["amount_fen"],
                            bool(r["frozen"]), r["dispute_id"], r["created_at"]) for r in rows]

    def get_segment(self, segment_id: str) -> TollSegment:
        row = self.database.connection.execute("SELECT * FROM toll_segments WHERE segment_id=?",
                                               (segment_id,)).fetchone()
        if row is None:
            raise NotFoundError("路段不存在")
        return TollSegment(row["segment_id"], row["organization_id"], row["name"],
                           row["rate_fen_per_km"], row["length_m"], row["version"])

    # ----- 内部辅助 -------------------------------------------------

    def _replay_check(self, request_id: str, action: str,
                      payload: dict[str, Any]) -> dict[str, Any] | None:
        """事务开启前的幂等预查：保证重试不被后续业务状态拦截。

        这是一次无事务快速读取；并发写入的最终正确性仍由
        BEGIN IMMEDIATE 与 request_receipts 主键、_idempotent 复查保证。
        """

        request_id = str(request_id).strip()
        prior = self.database.connection.execute(
            "SELECT action,payload_hash,response_json FROM request_receipts WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if prior is None:
            return None
        if prior["action"] != action or prior["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return json.loads(prior["response_json"])

    def _idem(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
              create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        """幂等包装：重放时返回首次写入时保存的同一响应体。

        调用方进入事务前无法校验业务状态，因此先在事务外预查回执，
        保证“同一请求重试”永远返回首次结果而不被后续状态变化（如已关账）拦截；
        事务内 _idempotent 仍会再查一次以兜底并发。
        """

        prior = self.database.connection.execute(
            "SELECT action,payload_hash,response_json FROM request_receipts WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if prior is not None:
            if prior["action"] != action or prior["payload_hash"] != digest(payload):
                raise ConflictError("request_id 已被不同内容使用")
            return json.loads(prior["response_json"])

        captured: dict[str, Any] = {}

        def wrapped() -> tuple[str, str, dict[str, Any]]:
            resource_type, resource_id, response = create()
            captured["response"] = response
            return resource_type, resource_id, response

        receipt = self._idempotent(connection, request_id=request_id, action=action,
                                   payload=payload, create=wrapped)
        if receipt.replayed:
            row = connection.execute("SELECT response_json FROM request_receipts WHERE request_id=?",
                                     (request_id,)).fetchone()
            return json.loads(row["response_json"])
        return captured["response"]

    def _load_trip_for_ops(self, connection, actor_id: str, trip_id: str):
        actor = self._actor(connection, actor_id)
        self._require(actor, *OPERATIONS_STAFF)
        trip = connection.execute("SELECT * FROM toll_trips WHERE trip_id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFoundError("行程不存在")
        return actor, trip

    def _active_event(self, connection, trip_id: str, kind: str):
        return connection.execute(
            "SELECT * FROM toll_events WHERE trip_id=? AND kind=? AND active=1 "
            "ORDER BY received_at DESC LIMIT 1",
            (trip_id, kind),
        ).fetchone()

    def _provisional_basis(self, connection, trip_id: str) -> str:
        """缺出口时的临时计价基准：取入口/临时免费/绕行等已知事件的最晚时间。"""

        row = connection.execute(
            "SELECT MAX(event_time) AS basis FROM toll_events WHERE trip_id=? AND active=1",
            (trip_id,),
        ).fetchone()
        return row["basis"] or self._now()

    def _add_event(self, connection, *, trip_id: str, kind: str, event_time: str, location,
                   detail: dict[str, Any], evidence_hash, received_at: str, event_id: str | None = None,
                   supersedes_event_id: str | None = None) -> str:
        new_event_id = event_id or uuid.uuid4().hex
        if connection.execute("SELECT 1 FROM toll_events WHERE event_id=?", (new_event_id,)).fetchone():
            raise ConflictError("事件编号已经存在")
        if supersedes_event_id is not None:
            connection.execute("UPDATE toll_events SET active=0 WHERE event_id=?", (supersedes_event_id,))
        if kind == "exit":
            # 部分唯一索引保证每行程仅一个有效出口；补录时旧出口显式失活而非删除。
            connection.execute("UPDATE toll_events SET active=0 WHERE trip_id=? AND kind='exit' AND active=1",
                               (trip_id,))
        connection.execute(
            "INSERT INTO toll_events(event_id,trip_id,kind,event_time,location,detail_json,evidence_hash,"
            "received_at,supersedes_event_id,active) VALUES(?,?,?,?,?,?,?,?,?,1)",
            (new_event_id, trip_id, kind, event_time, location, canonical_json(detail),
             evidence_hash, received_at, supersedes_event_id),
        )
        return new_event_id

    def _policies_at(self, connection, at: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM toll_policy_versions WHERE status='active' AND effective_at<=? "
            "AND (expires_at IS NULL OR expires_at>?)",
            (at, at),
        ).fetchall()
        return [{"policy_id": r["policy_id"], "version": r["version"], "kind": r["kind"],
                 "priority": r["priority"], "scope": json.loads(r["scope_json"]),
                 "params": json.loads(r["params_json"]), "status": r["status"],
                 "effective_at": r["effective_at"], "expires_at": r["expires_at"]} for r in rows]

    def _get_fact(self, connection, fact_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM toll_billing_facts WHERE fact_id=?",
                                 (fact_id,)).fetchone()
        if row is None:
            raise NotFoundError("计费事实不存在")
        return self._fact_dict(row)

    @staticmethod
    def _fact_dict(row) -> dict[str, Any]:
        return {"fact_id": row["fact_id"], "trip_id": row["trip_id"], "version": row["version"],
                "status": row["status"], "input_hash": row["input_hash"], "total_fen": row["total_fen"],
                "detail": json.loads(row["detail_json"]), "basis_time": row["basis_time"],
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def _discount_bearers(self, connection, fact: dict[str, Any]) -> dict[str, tuple[str, int, str]]:
        """返回参与计算的政策版本 -> (发布机构, 版本号, 类型)。"""

        bearers: dict[str, tuple[str, int, str]] = {}
        for ref in fact["detail"].get("policy_versions", []):
            row = connection.execute(
                "SELECT tpv.version,tpv.kind,tp.published_org FROM toll_policy_versions tpv "
                "JOIN toll_policies tp ON tp.policy_id=tpv.policy_id "
                "WHERE tpv.policy_id=? AND tpv.version=?",
                (ref["policy_id"], ref["version"]),
            ).fetchone()
            if row is not None:
                bearers[ref["policy_id"]] = (row["published_org"], row["version"], row["kind"])
        return bearers

    def _settlement_lines(self, fact: dict[str, Any], bearers: dict[str, tuple[str, int, str]]):
        """把计费事实展开为逐路段收入与按机构归集的优惠承担，并校验逐路段守恒。"""

        lines = []
        for item in fact["detail"]["per_segment"]:
            borne: dict[str, int] = {}

            def add(org_id: str | None, amount: int) -> None:
                if amount <= 0:
                    return
                if org_id is None:
                    raise ConflictError("优惠承担机构缺失，无法关账")
                borne[org_id] = borne.get(org_id, 0) + amount

            chain_total = 0
            for step in item["discounts"]:
                add(bearers.get(step["policy_id"], (None,))[0], step["discount_fen"])
                chain_total += step["discount_fen"]
            trip_relief = item.get("trip_cap_relief_fen", 0)
            if item.get("segment_cap"):
                add(bearers.get(item["segment_cap"]["policy_id"], (None,))[0],
                    item["discount_fen"] - chain_total - trip_relief)
            if trip_relief:
                add(bearers.get(item["trip_cap_policy_id"], (None,))[0], trip_relief)
            if item.get("exemption"):
                add(bearers.get(item["exemption"]["policy_id"], (None,))[0], item["discount_fen"])
            for waiver in item.get("waivers", []):
                add(waiver["borne_organization_id"], waiver["amount_fen"])
            if sum(borne.values()) != item["gross_fen"] - item["final_fen"]:
                raise ConflictError("路段优惠承担金额不守恒，拒绝关账")
            lines.append({"segment_id": item["segment_id"], "organization_id": item["organization_id"],
                          "gross_fen": item["gross_fen"], "revenue_fen": item["final_fen"],
                          "discount_borne": sorted(borne.items())})
        return lines

    def _ledger(self, connection, *, voucher_type: str, voucher_id: str, trip_id: str,
                organization_id: str | None, account: str, direction: str, amount_fen: int,
                created_at: str, dispute_id: str | None = None) -> None:
        if amount_fen == 0:
            return
        entry_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO toll_ledger_entries(entry_id,voucher_type,voucher_id,trip_id,organization_id,"
            "account,direction,amount_fen,frozen,dispute_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
            (entry_id, voucher_type, voucher_id, trip_id, organization_id, account, direction,
             amount_fen, dispute_id, created_at),
        )

    def _assert_balanced(self, connection, voucher_id: str, voucher_type: str) -> None:
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='debit' THEN amount_fen ELSE 0 END),0) AS debit_total,"
            "COALESCE(SUM(CASE WHEN direction='credit' THEN amount_fen ELSE 0 END),0) AS credit_total "
            "FROM toll_ledger_entries WHERE voucher_type=? AND voucher_id=?",
            (voucher_type, voucher_id),
        ).fetchone()
        if row["debit_total"] != row["credit_total"]:
            raise ConflictError(
                f"凭证 {voucher_type}/{voucher_id} 借贷不平衡: "
                f"{row['debit_total']} != {row['credit_total']}")

    @staticmethod
    def _allocate(pool: dict[str, int], amount: int) -> list[tuple[str, int]]:
        """按各机构在池中的权重分配整数金额，最大余数法兜底，保证一分不差。"""

        total = sum(pool.values())
        if amount > total or total == 0:
            raise ConflictError("金额分摊失败：资金池不足")
        result: dict[str, int] = {}
        remainders: list[tuple[int, str]] = []
        for org_id, weight in sorted(pool.items()):
            share = amount * weight
            result[org_id] = share // total
            remainders.append((share % total, org_id))
        left = amount - sum(result.values())
        for _, org_id in sorted(remainders, key=lambda x: (-x[0], x[1])):
            if left == 0:
                break
            if result[org_id] < pool[org_id]:
                result[org_id] += 1
                left -= 1
        for org_id in sorted(pool):  # 极端情况下用容量兜底
            if left == 0:
                break
            take = min(pool[org_id] - result[org_id], left)
            if take > 0:
                result[org_id] += take
                left -= take
        if left or sum(result.values()) != amount:
            raise ConflictError("金额分摊失败，未保持守恒")
        return sorted((o, a) for o, a in result.items() if a)

    @staticmethod
    def _allocate_proportional(weights: dict[str, int], amount: int) -> list[tuple[str, int]]:
        """按权重（不设容量上限）分摊金额，最大余数法保证合计精确。"""

        from .toll_engine import _proportional

        ordered = sorted(weights)
        parts = _proportional([weights[o] for o in ordered], amount)
        return sorted((o, part) for o, part in zip(ordered, parts) if part)

    def _events(self, connection, trip_id: str) -> list[dict[str, Any]]:
        return [{"event_id": r["event_id"], "kind": r["kind"], "event_time": r["event_time"],
                 "location": r["location"], "detail": json.loads(r["detail_json"]),
                 "evidence_hash": r["evidence_hash"], "received_at": r["received_at"],
                 "supersedes_event_id": r["supersedes_event_id"], "active": bool(r["active"])}
                for r in connection.execute(
                    "SELECT * FROM toll_events WHERE trip_id=? ORDER BY event_time,received_at", (trip_id,))]

    def _locked_segments(self, connection, trip_id: str) -> list[dict[str, Any]]:
        return [{"ordinal": r["ordinal"], "segment_id": r["segment_id"],
                 "segment_version": r["segment_version"]}
                for r in connection.execute(
                    "SELECT * FROM toll_trip_segments WHERE trip_id=? ORDER BY ordinal", (trip_id,))]

    def _fact_view(self, fact: dict[str, Any], bearers: dict[str, tuple[str, int, str]]) -> dict[str, Any]:
        per_segment = []
        for item in fact["detail"]["per_segment"]:
            line = dict(item)
            attribution: list[dict[str, Any]] = []
            for step in item["discounts"]:
                attribution.append({"policy_id": step["policy_id"],
                                    "organization_id": bearers.get(step["policy_id"], (None,))[0],
                                    "amount_fen": step["discount_fen"], "reason": "discount"})
            if item.get("segment_cap"):
                chain = sum(s["discount_fen"] for s in item["discounts"])
                attribution.append({"policy_id": item["segment_cap"]["policy_id"],
                                    "organization_id": bearers.get(item["segment_cap"]["policy_id"],
                                                                  (None,))[0],
                                    "amount_fen": item["discount_fen"] - chain
                                    - item.get("trip_cap_relief_fen", 0), "reason": "cap_segment"})
            if item.get("trip_cap_relief_fen"):
                attribution.append({"policy_id": item["trip_cap_policy_id"],
                                    "organization_id": bearers.get(item["trip_cap_policy_id"],
                                                                  (None,))[0],
                                    "amount_fen": item["trip_cap_relief_fen"],
                                    "reason": "cap_trip"})
            if item.get("exemption"):
                attribution.append({"policy_id": item["exemption"]["policy_id"],
                                    "organization_id": bearers.get(item["exemption"]["policy_id"],
                                                                  (None,))[0],
                                    "amount_fen": item["discount_fen"], "reason": "exemption"})
            for waiver in item.get("waivers", []):
                attribution.append({"policy_id": None,
                                    "organization_id": waiver["borne_organization_id"],
                                    "event_id": waiver["event_id"], "amount_fen": waiver["amount_fen"],
                                    "reason": "temporary_free"})
            line["discount_attribution"] = attribution
            per_segment.append(line)
        detail = dict(fact["detail"])
        detail["per_segment"] = per_segment
        return {k: fact[k] for k in ("fact_id", "version", "status", "input_hash", "total_fen",
                                     "basis_time", "created_by", "created_at")} | {"detail": detail}

    def _ledger_view(self, connection, trip_id: str):
        settlements, adjustments, disputes = [], [], []
        for row in connection.execute(
            "SELECT * FROM toll_ledger_entries WHERE trip_id=? ORDER BY created_at,entry_id", (trip_id,)):
            entry = {"entry_id": row["entry_id"], "voucher_type": row["voucher_type"],
                     "voucher_id": row["voucher_id"], "organization_id": row["organization_id"],
                     "account": row["account"], "direction": row["direction"],
                     "amount_fen": row["amount_fen"], "frozen": bool(row["frozen"]),
                     "dispute_id": row["dispute_id"]}
            (settlements if row["voucher_type"] == "settlement" else adjustments).append(entry)
        for row in connection.execute("SELECT * FROM toll_disputes WHERE trip_id=? ORDER BY opened_at",
                                      (trip_id,)):
            disputes.append({"dispute_id": row["dispute_id"], "status": row["status"],
                             "reason": row["reason"], "opened_at": row["opened_at"],
                             "resolved_at": row["resolved_at"]})
        return settlements, adjustments, disputes

    def _organization_shares(self, connection, trip_id: str) -> dict[str, dict[str, int]]:
        """汇总每家运营方的收入、优惠承担、冻结金额与累计调整。"""

        shares: dict[str, dict[str, int]] = {}

        def ensure(org_id: str | None) -> dict[str, int]:
            return shares.setdefault(org_id or "__pool__",
                                     {"revenue_fen": 0, "discount_borne_fen": 0, "frozen_fen": 0,
                                      "refund_fen": 0, "surcharge_fen": 0, "net_fen": 0})

        for row in connection.execute("SELECT * FROM toll_ledger_entries WHERE trip_id=?", (trip_id,)):
            bucket = ensure(row["organization_id"])
            if row["voucher_type"] == "settlement" and row["direction"] == "credit":
                if row["account"] == "revenue":
                    bucket["revenue_fen"] += row["amount_fen"]
                elif row["account"] == "discount_grant":
                    bucket["discount_borne_fen"] += row["amount_fen"]
            elif row["voucher_type"] == "refund" and row["direction"] == "debit":
                bucket["refund_fen"] += row["amount_fen"]
            elif row["voucher_type"] == "surcharge" and row["direction"] == "credit":
                bucket["surcharge_fen"] += row["amount_fen"]
            if row["frozen"]:
                bucket["frozen_fen"] += row["amount_fen"]
        for bucket in shares.values():
            bucket["net_fen"] = (bucket["revenue_fen"] - bucket["refund_fen"]
                                 + bucket["surcharge_fen"])
        return shares

    def _version_dict(self, row) -> PolicyVersion:
        return PolicyVersion(row["policy_id"], row["version"], row["kind"], row["priority"],
                             json.loads(row["scope_json"]), json.loads(row["params_json"]),
                             row["effective_at"], row["expires_at"], row["status"],
                             row["withdrawn_at"], row["published_by"], row["published_at"])

    # ----- 字段校验 -------------------------------------------------

    @staticmethod
    def _money(value, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数（分）")
        return value

    @staticmethod
    def _positive_int(value, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{field} 必须是正整数")
        return value

    @staticmethod
    def _int_range(value, field: str, low: int, high: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ValidationError(f"{field} 必须是 [{low},{high}] 范围内的整数")
        return value

    def _scope(self, scope: Any) -> dict[str, Any]:
        if not isinstance(scope, dict):
            raise ValidationError("scope 必须是对象")
        allowed = {"segment_ids", "organization_ids", "vehicle_classes", "tags", "hours"}
        if not set(scope) <= allowed:
            raise ValidationError(
                "scope 只能包含 segment_ids/organization_ids/vehicle_classes/tags/hours")
        for key, value in scope.items():
            if key == "hours":
                if (not isinstance(value, list) or len(value) != 2
                        or not all(isinstance(v, int) and not isinstance(v, bool)
                                   and 0 <= v <= 23 for v in value)):
                    raise ValidationError("scope.hours 必须是两个 0-23 的整数小时 [起,止)")
            elif not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
                raise ValidationError(f"scope.{key} 必须是非空字符串数组")
        return scope

    def _params(self, kind: str, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or not params:
            raise ValidationError("params 必须是非空对象")
        if kind == "rate":
            self._money(params.get("rate_fen_per_km"), "params.rate_fen_per_km")
        elif kind == "discount":
            mode = params.get("mode", "percent")
            if mode == "percent":
                self._int_range(params.get("permille"), "params.permille", 1, 1000)
            elif mode == "reduction":
                self._positive_int(params.get("amount_fen"), "params.amount_fen")
            else:
                raise ValidationError("折扣 mode 必须是 percent 或 reduction")
        elif kind == "cap":
            self._positive_int(params.get("max_fen"), "params.max_fen")
            if params.get("level", "trip") not in ("trip", "segment"):
                raise ValidationError("封顶 level 必须是 trip 或 segment")
        elif kind == "exemption":
            pass
        return params


def header_effective(connection, policy_id: str, version: int) -> str:
    """读取某政策当前版本的生效时间。"""

    row = connection.execute(
        "SELECT effective_at FROM toll_policy_versions WHERE policy_id=? AND version=?",
        (policy_id, version),
    ).fetchone()
    return row["effective_at"]
