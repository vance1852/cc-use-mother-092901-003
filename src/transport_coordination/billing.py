"""收费政策执行与清算服务。

关键边界：
- 政策（费率/封顶/豁免/资格）以不可变版本发布，带生效区间与优先级；撤回打时间戳，
  只影响此后重新计费且尚未结算的行程。
- 行程事件（入口/出口/封路绕行/出口补录/迟到通知）只追加；每次计费生成新版本的
  不可变计费事实，并以“冲回旧分录 + 新分录”代替原地改写。
- 已关账（结算）收入不可变：迟到事件只登记并标记，不产生新金额；退款、追缴只能
  发生在结算前，按运营方净额成比例分摊，分录金额始终守恒。
- 争议只冻结该行程的相关分录：冻结期间不能结算、不能调整、不能再生成新计费版本，
  其他运营方与其他行程不受影响。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .audit import append_event, canonical_json
from .policy_engine import ChargeLine, Policy, evaluate_charge, policy_snapshot
from .errors import AccountClosedError, BillingStateError, ConflictError, NotFoundError, ValidationError
from .service import DomainService

ENTRY_KINDS = frozenset({"entry"})
EXIT_KINDS = frozenset({"exit", "supplemental_exit"})
EVENT_KINDS = frozenset({"entry", "exit", "supplemental_exit", "detour", "late_notice"})
ADJUSTMENT_KINDS = frozenset({"refund", "recovery"})


class BillingService(DomainService):
    """在基础登记服务之上提供政策发布、计费事实、清算分录与解释重放。"""

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _to_utc(moment: datetime) -> datetime:
        from datetime import timezone
        return moment.astimezone(timezone.utc)

    def _ts(self, value: Any, field: str) -> datetime:
        return self._to_utc(self._parse_ts_raw(value, field))

    def _parse_ts_raw(self, value: Any, field: str) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field} 必须是带时区的 ISO 时间字符串")
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 时间格式无效") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须携带时区")
        return moment

    def _amount(self, value: Any, field: str, *, minimum: int = 0) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是以分为单位的整数")
        if value < minimum:
            raise ValidationError(f"{field} 不能小于 {minimum}")
        return value

    def _string_list(self, value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValidationError(f"{field} 必须是非空字符串数组")
        return [item.strip() for item in value]

    def _conditions(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("conditions 必须是对象")
        allowed = {"vehicle_classes", "tags", "segment_ids", "is_holiday", "night"}
        if not set(value) <= allowed:
            raise ValidationError("conditions 含不支持的字段")
        if "vehicle_classes" in value:
            self._string_list(value["vehicle_classes"], "conditions.vehicle_classes")
        if "tags" in value:
            self._string_list(value["tags"], "conditions.tags")
        if "segment_ids" in value:
            self._string_list(value["segment_ids"], "conditions.segment_ids")
        if "is_holiday" in value and not isinstance(value["is_holiday"], bool):
            raise ValidationError("conditions.is_holiday 必须是布尔值")
        if "night" in value and not isinstance(value["night"], bool):
            raise ValidationError("conditions.night 必须是布尔值")
        return value

    def _effect(self, kind: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("effect 必须是对象")
        if kind == "rate":
            if value.get("type") != "discount_pct":
                raise ValidationError("rate 政策的 effect.type 必须是 discount_pct")
            percent = value.get("percent")
            if isinstance(percent, bool) or not isinstance(percent, int) or not 1 <= percent <= 99:
                raise ValidationError("rate 政策的 percent 必须是 1-99 的整数")
            return {"type": "discount_pct", "percent": percent}
        if kind == "cap":
            if value.get("type") != "amount_cap":
                raise ValidationError("cap 政策的 effect.type 必须是 amount_cap")
            self._amount(value.get("amount_fen"), "effect.amount_fen", minimum=1)
            return {"type": "amount_cap", "amount_fen": value["amount_fen"]}
        if kind == "exemption":
            if value.get("type") != "full":
                raise ValidationError("exemption 政策的 effect.type 必须是 full")
            return {"type": "full"}
        tags = value.get("grant_tags")
        if not isinstance(tags, list) or not tags or any(not isinstance(item, str) or not item.strip() for item in tags):
            raise ValidationError("eligibility 政策必须通过 grant_tags 授予至少一个资格标签")
        return {"grant_tags": [item.strip() for item in tags]}

    # ------------------------------------------------------------- 路网登记

    def register_segment(self, *, request_id: str, actor_id: str, segment_id: str,
                         organization_id: str, name: str, base_amount_fen: int) -> Any:
        payload = {"actor_id": actor_id, "segment_id": segment_id, "organization_id": organization_id,
                   "name": name, "base_amount_fen": base_amount_fen}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if actor.organization_id != organization_id and actor.role != "admin":
                raise ConflictError("不能为其他组织登记路段")
            segment_id = self._identifier(segment_id, "segment_id")
            name = self._text(name, "name")
            base_amount_fen = self._amount(base_amount_fen, "base_amount_fen")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("运营组织不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO road_segments(segment_id,organization_id,name,base_amount_fen,active,created_at) "
                        "VALUES(?,?,?,?,1,?)",
                        (segment_id, organization_id, name, base_amount_fen, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("路段编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="segment.registered",
                             resource_type="road_segment", resource_id=segment_id,
                             detail={"organization_id": organization_id, "name": name,
                                     "base_amount_fen": base_amount_fen}, occurred_at=self._now())
                return "road_segment", segment_id, {"segment_id": segment_id}

            return self._idempotent(connection, request_id=request_id, action="register_segment",
                                    payload=payload, create=create)

    # ------------------------------------------------------------- 政策发布

    def publish_policy(self, *, request_id: str, actor_id: str, policy_id: str, kind: str,
                       priority: int, name: str, conditions: dict[str, Any], effect: dict[str, Any],
                       effective_from: str, effective_to: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "policy_id": policy_id, "kind": kind, "priority": priority,
                   "name": name, "conditions": conditions, "effect": effect,
                   "effective_from": effective_from, "effective_to": effective_to}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            policy_id = self._identifier(policy_id, "policy_id")
            name = self._text(name, "name")
            if kind not in ("rate", "cap", "exemption", "eligibility"):
                raise ValidationError("kind 必须是 rate/cap/exemption/eligibility")
            if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 999999:
                raise ValidationError("priority 必须是 0-999999 的整数，数字越小越优先")
            conditions = self._conditions(conditions)
            effect = self._effect(kind, effect)
            start = self._ts(effective_from, "effective_from")
            end = self._ts(effective_to, "effective_to") if effective_to else None
            if end is not None and end <= start:
                raise ValidationError("effective_to 必须晚于 effective_from")
            row = connection.execute("SELECT COALESCE(MAX(version),0) AS version FROM policies WHERE policy_id=?",
                                     (policy_id,)).fetchone()
            version = row["version"] + 1

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO policies(policy_id,version,kind,priority,name,conditions_json,effect_json,"
                    "effective_from,effective_to,published_by,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (policy_id, version, kind, priority, name, canonical_json(conditions),
                     canonical_json(effect), start.isoformat(), end.isoformat() if end else None,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="policy.published",
                             resource_type="policy", resource_id=policy_id,
                             detail={"version": version, "kind": kind, "priority": priority, "name": name,
                                     "effective_from": start.isoformat(),
                                     "effective_to": end.isoformat() if end else None,
                                     "conditions": conditions, "effect": effect},
                             occurred_at=self._now())
                return "policy", f"{policy_id}:{version}", {"policy_id": policy_id, "version": version}

            return self._idempotent(connection, request_id=request_id, action="publish_policy",
                                    payload=payload, create=create)

    def withdraw_policy(self, *, request_id: str, actor_id: str, policy_id: str,
                        version: int | None = None) -> Any:
        payload = {"actor_id": actor_id, "policy_id": policy_id, "version": version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            policy_id = self._identifier(policy_id, "policy_id")
            if version is None:
                row = connection.execute(
                    "SELECT version FROM policies WHERE policy_id=? ORDER BY version DESC LIMIT 1",
                    (policy_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError("政策不存在")
                version = row["version"]
            elif isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValidationError("version 必须是正整数")
            target = connection.execute(
                "SELECT * FROM policies WHERE policy_id=? AND version=?", (policy_id, version)
            ).fetchone()
            if target is None:
                raise NotFoundError("政策版本不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                updated = connection.execute(
                    "UPDATE policies SET withdrawn_at=? WHERE policy_id=? AND version=? AND withdrawn_at IS NULL",
                    (self._now(), policy_id, version),
                )
                if updated.rowcount == 0:
                    raise ConflictError("该政策版本已经撤回")
                append_event(connection, actor_id=actor_id, action="policy.withdrawn",
                             resource_type="policy", resource_id=policy_id,
                             detail={"version": version}, occurred_at=self._now())
                return "policy", f"{policy_id}:{version}", {"policy_id": policy_id, "version": version,
                                                            "withdrawn": True}

            return self._idempotent(connection, request_id=request_id, action="withdraw_policy",
                                    payload=payload, create=create)

    def list_policies(self, *, include_withdrawn: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM policies"
        if not include_withdrawn:
            sql += " WHERE withdrawn_at IS NULL"
        sql += " ORDER BY policy_id, version"
        return [self._policy_dict(row) for row in self.database.connection.execute(sql)]

    @staticmethod
    def _policy_dict(row) -> dict[str, Any]:
        return {
            "policy_id": row["policy_id"], "version": row["version"], "kind": row["kind"],
            "priority": row["priority"], "name": row["name"],
            "conditions": json.loads(row["conditions_json"]), "effect": json.loads(row["effect_json"]),
            "effective_from": row["effective_from"], "effective_to": row["effective_to"],
            "published_by": row["published_by"], "published_at": row["published_at"],
            "withdrawn_at": row["withdrawn_at"],
        }

    def _policies_in_force(self, connection, at: datetime,
                           observed_at: datetime | None = None) -> list[Policy]:
        """返回某业务时点有效、且在观察时点仍可见未撤回的政策最新版本快照。

        ``at`` 是业务发生时点（如出口时间），决定生效区间；``observed_at`` 是
        计费/重放的观察时点，决定政策是否已发布、是否已撤回。这样政策撤回只影响
        撤回时还未结算（未完成计费）的行程，而不追溯改变已固化的计费事实。
        """

        observed_at = observed_at or at
        latest: dict[str, Policy] = {}
        for row in connection.execute("SELECT * FROM policies ORDER BY version"):
            published = datetime.fromisoformat(row["published_at"])
            if published > observed_at:
                continue
            withdrawn = datetime.fromisoformat(row["withdrawn_at"]) if row["withdrawn_at"] else None
            if withdrawn is not None and withdrawn <= observed_at:
                continue
            effective_from = datetime.fromisoformat(row["effective_from"])
            effective_to = datetime.fromisoformat(row["effective_to"]) if row["effective_to"] else None
            if effective_from > at or (effective_to is not None and at >= effective_to):
                continue
            policy = Policy(
                policy_id=row["policy_id"], version=row["version"], kind=row["kind"],
                priority=row["priority"], name=row["name"],
                conditions=json.loads(row["conditions_json"]), effect=json.loads(row["effect_json"]),
                effective_from=effective_from, effective_to=effective_to,
                published_at=published, withdrawn_at=withdrawn,
            )
            latest[policy.policy_id] = policy  # 版本升序，保留最大版本
        return list(latest.values())

    # ------------------------------------------------------------- 行程事件

    def record_trip_event(self, *, request_id: str, actor_id: str, trip_id: str, kind: str,
                          event_occurred_at: str, evidence_ref: str, data: dict[str, Any] | None = None) -> Any:
        data = data or {}
        if not isinstance(data, dict):
            raise ValidationError("data 必须是对象")
        payload = {"actor_id": actor_id, "trip_id": trip_id, "kind": kind,
                   "event_occurred_at": event_occurred_at, "evidence_ref": evidence_ref, "data": data}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            trip_id = self._identifier(trip_id, "trip_id")
            if kind not in EVENT_KINDS:
                raise ValidationError("kind 必须是 entry/exit/supplemental_exit/detour/late_notice")
            occurred = self._ts(event_occurred_at, "event_occurred_at")
            occurred_local = self._parse_ts_raw(event_occurred_at, "event_occurred_at")
            evidence_ref = self._text(evidence_ref, "evidence_ref", 200)
            trip = connection.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
            if trip is None and kind != "entry":
                raise NotFoundError("行程不存在，必须先登记入口事件")

            if trip is None:
                vehicle_id = self._text(data.get("vehicle_id", ""), "data.vehicle_id", 80)
                vehicle_class = self._text(data.get("vehicle_class", ""), "data.vehicle_class", 40)
                tags = self._string_list(data.get("tags", []), "data.tags") if data.get("tags") is not None else []
                entry_plaza = self._text(data.get("entry_plaza", ""), "data.entry_plaza", 80)
                entry_segment_id = data.get("entry_segment_id")
                is_holiday = 1 if data.get("is_holiday") is True else 0
                if entry_segment_id is not None:
                    entry_segment_id = self._identifier(entry_segment_id, "data.entry_segment_id")
                    if connection.execute("SELECT 1 FROM road_segments WHERE segment_id=?",
                                          (entry_segment_id,)).fetchone() is None:
                        raise NotFoundError("入口路段不存在")

                def create() -> tuple[str, str, dict[str, Any]]:
                    connection.execute(
                        "INSERT INTO trips(trip_id,vehicle_id,vehicle_class,vehicle_tags_json,entry_segment_id,"
                        "entry_plaza,entry_at,path_json,is_holiday,evidence_ref,state,current_version,"
                        "has_late_event,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                        (trip_id, vehicle_id, vehicle_class, canonical_json(tags), entry_segment_id,
                         entry_plaza, occurred.isoformat(),
                         canonical_json([entry_segment_id]) if entry_segment_id else None,
                         is_holiday, evidence_ref, "pending_evidence", 0, self._now()),
                    )
                    self._append_event(connection, trip_id, 0, "entry", data, occurred, actor_id,
                                       occurred_local)
                    append_event(connection, actor_id=actor_id, action="trip.opened",
                                 resource_type="trip", resource_id=trip_id,
                                 detail={"entry_plaza": entry_plaza, "entry_at": occurred.isoformat(),
                                         "vehicle_id": vehicle_id},
                                 occurred_at=self._now())
                    return "trip", trip_id, {"trip_id": trip_id, "state": "pending_evidence",
                                             "fact_version": 0, "billed": False}

                return self._idempotent(connection, request_id=request_id, action="record_trip_event",
                                        payload=payload, create=create)

            # 既有行程：追加事件并按状态分派
            frozen = self._has_open_dispute(connection, trip_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if kind == "entry":
                    raise ConflictError("行程已经登记入口事件，不能重复登记；更正请使用补录或绕行事件")
                sequence_row = connection.execute(
                    "SELECT COALESCE(MAX(sequence),0) AS sequence FROM trip_events WHERE trip_id=?",
                    (trip_id,),
                ).fetchone()
                sequence = sequence_row["sequence"] + 1
                self._append_event(connection, trip_id, sequence, kind, data, occurred, actor_id,
                                   occurred_local)
                response: dict[str, Any] = {"trip_id": trip_id, "state": trip["state"],
                                            "fact_version": trip["current_version"], "billed": False,
                                            "amount_changed": False}

                if kind == "detour":
                    path = self._string_list(data.get("path", []), "data.path")
                    self._require_segments(connection, path)
                    if trip["state"] == "settled" or frozen:
                        connection.execute("UPDATE trips SET has_late_event=1 WHERE trip_id=?", (trip_id,))
                        append_event(connection, actor_id=actor_id, action="trip.late_event_recorded",
                                     resource_type="trip", resource_id=trip_id,
                                     detail={"event_kind": kind, "reason": self._freeze_reason(trip, frozen)},
                                     occurred_at=self._now())
                    elif trip["current_version"] == 0:
                        connection.execute("UPDATE trips SET path_json=? WHERE trip_id=?",
                                           (canonical_json(path), trip_id))
                        append_event(connection, actor_id=actor_id, action="trip.path_updated",
                                     resource_type="trip", resource_id=trip_id,
                                     detail={"path": path, "sequence": sequence}, occurred_at=self._now())
                    else:
                        self._rebill(connection, trip, occurred_at=None, exit_ref=None, path=path,
                                     actor_id=actor_id, event_kind=kind, sequence=sequence)
                        response.update(state="billed", fact_version=trip["current_version"] + 1,
                                        billed=True, amount_changed=True)
                    return "trip", trip_id, response

                if kind in EXIT_KINDS:
                    path = None
                    if data.get("path") is not None:
                        path = self._string_list(data.get("path"), "data.path")
                        self._require_segments(connection, path)
                    if trip["state"] == "settled" or frozen:
                        connection.execute("UPDATE trips SET has_late_event=1 WHERE trip_id=?", (trip_id,))
                        append_event(connection, actor_id=actor_id, action="trip.late_event_recorded",
                                     resource_type="trip", resource_id=trip_id,
                                     detail={"event_kind": kind, "reason": self._freeze_reason(trip, frozen)},
                                     occurred_at=self._now())
                        response["state"] = "settled" if trip["state"] == "settled" else "billed"
                        response["amount_changed"] = False
                        return "trip", trip_id, response
                    # 已计费行程再次收到普通出口视为重复刷卡，不产生新版本；
                    # 更正路径必须显式使用 supplemental_exit。
                    if kind == "exit" and trip["current_version"] >= 1:
                        append_event(connection, actor_id=actor_id, action="trip.duplicate_exit_ignored",
                                     resource_type="trip", resource_id=trip_id,
                                     detail={"sequence": sequence}, occurred_at=self._now())
                        response.update(state="billed", fact_version=trip["current_version"],
                                        billed=False, amount_changed=False)
                        return "trip", trip_id, response
                    new_version = self._bill(connection, trip, exit_at=occurred, exit_ref=evidence_ref,
                                             path_override=path, actor_id=actor_id, event_kind=kind,
                                             sequence=sequence, charge_local_at=occurred_local)
                    if new_version == 0:
                        response.update(state="pending_evidence", fact_version=0, billed=False)
                    else:
                        response.update(state="billed", fact_version=new_version, billed=True,
                                        amount_changed=True)
                    return "trip", trip_id, response

                # late_notice：仅登记并标记，不直接改金额
                connection.execute("UPDATE trips SET has_late_event=1 WHERE trip_id=?", (trip_id,))
                append_event(connection, actor_id=actor_id, action="trip.late_event_recorded",
                             resource_type="trip", resource_id=trip_id,
                             detail={"event_kind": kind, "sequence": sequence,
                                     "settled": trip["state"] == "settled"},
                             occurred_at=self._now())
                response["amount_changed"] = False
                return "trip", trip_id, response

            return self._idempotent(connection, request_id=request_id, action="record_trip_event",
                                    payload=payload, create=create)

    @staticmethod
    def _freeze_reason(trip, frozen: bool) -> str:
        if trip["state"] == "settled":
            return "trip_settled"
        if frozen:
            return "dispute_frozen"
        return "late"

    def _append_event(self, connection, trip_id: str, sequence: int, kind: str, data: dict[str, Any],
                      occurred: datetime, actor_id: str, local_at: datetime | None = None) -> None:
        connection.execute(
            "INSERT INTO trip_events(event_id,trip_id,sequence,kind,payload_json,event_occurred_at,"
            "event_local_at,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, trip_id, sequence, kind, canonical_json(data),
             occurred.isoformat(), local_at.isoformat() if local_at else None, actor_id, self._now()),
        )

    def _require_segments(self, connection, segment_ids: list[str]) -> None:
        if not segment_ids:
            raise ValidationError("path 至少包含一个路段")
        if len(segment_ids) != len(set(segment_ids)):
            raise ValidationError("path 中存在重复路段")
        for segment_id in segment_ids:
            self._identifier(segment_id, "segment_id")
            row = connection.execute("SELECT 1 FROM road_segments WHERE segment_id=? AND active=1",
                                     (segment_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"路段 {segment_id} 不存在或未启用")

    @staticmethod
    def _has_open_dispute(connection, trip_id: str) -> bool:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM disputes WHERE trip_id=? AND status='open'", (trip_id,)
        ).fetchone()
        return row["count"] > 0

    def _resolve_path(self, connection, trip, path_override: list[str] | None) -> list[str]:
        if path_override is not None:
            return path_override
        stored = json.loads(trip["path_json"]) if trip["path_json"] else None
        if stored:
            return stored
        return [trip["entry_segment_id"]] if trip["entry_segment_id"] else []

    def _bill(self, connection, trip, *, exit_at: datetime, exit_ref: str | None,
              path_override: list[str] | None, actor_id: str, event_kind: str,
              sequence: int, charge_local_at: datetime | None = None) -> int:
        """生成下一版本计费事实与守恒分录；路径不足时保持待补证并返回 0。

        ``exit_at`` 是出口的 UTC 绝对时刻，用于选取生效政策；``charge_local_at``
        是出口事件原始时区下的墙钟时间，用于夜间等按当地时段判定的规则。
        """

        path = self._resolve_path(connection, trip, path_override)
        if not path:
            append_event(connection, actor_id=actor_id, action="trip.evidence_insufficient",
                         resource_type="trip", resource_id=trip["trip_id"],
                         detail={"sequence": sequence, "event_kind": event_kind},
                         occurred_at=self._now())
            return 0
        self._require_segments(connection, path)
        segment_rows = {
            row["segment_id"]: row
            for row in connection.execute("SELECT * FROM road_segments WHERE active=1")
        }
        lines = [
            ChargeLine(segment_id=segment_id, operator_id=segment_rows[segment_id]["organization_id"],
                       base_amount_fen=segment_rows[segment_id]["base_amount_fen"])
            for segment_id in path
        ]
        policies = self._policies_in_force(connection, exit_at, self.clock.now())
        vehicle = {"vehicle_id": trip["vehicle_id"], "vehicle_class": trip["vehicle_class"],
                   "tags": json.loads(trip["vehicle_tags_json"])}
        trace = evaluate_charge(lines, vehicle, bool(trip["is_holiday"]),
                                charge_local_at or exit_at, policies)
        new_version = trip["current_version"] + 1
        connection.execute(
            "INSERT INTO billing_facts(trip_id,version,base_total_fen,final_total_fen,trace_json,"
            "policy_snapshot_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (trip["trip_id"], new_version, trace["base_total_fen"], trace["final_total_fen"],
             canonical_json(trace), canonical_json(policy_snapshot(policies)), self._now()),
        )
        # 冲回上一版本的收费分录（金额取反），保证不原地改写历史。
        if trip["current_version"] >= 1:
            for old in connection.execute(
                "SELECT * FROM ledger_entries WHERE trip_id=? AND fact_version=? AND kind='charge'",
                (trip["trip_id"], trip["current_version"]),
            ):
                connection.execute(
                    "INSERT INTO ledger_entries(entry_id,trip_id,fact_version,segment_id,adjustment_id,"
                    "operator_id,kind,amount_fen,frozen,period_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,0,NULL,?)",
                    (uuid.uuid4().hex, trip["trip_id"], new_version, old["segment_id"], None,
                     old["operator_id"], "charge_reversal", -old["amount_fen"], self._now()),
                )
        for segment in trace["segments"]:
            connection.execute(
                "INSERT INTO ledger_entries(entry_id,trip_id,fact_version,segment_id,adjustment_id,"
                "operator_id,kind,amount_fen,frozen,period_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,NULL,?)",
                (uuid.uuid4().hex, trip["trip_id"], new_version, segment["segment_id"], None,
                 segment["operator_id"], "charge", segment["final_fen"], self._now()),
            )
        posted = connection.execute(
            "SELECT COALESCE(SUM(amount_fen),0) AS total FROM ledger_entries WHERE trip_id=? AND kind='charge'",
            (trip["trip_id"],),
        ).fetchone()["total"]
        reversed_total = connection.execute(
            "SELECT COALESCE(SUM(amount_fen),0) AS total FROM ledger_entries "
            "WHERE trip_id=? AND kind='charge_reversal'", (trip["trip_id"],),
        ).fetchone()["total"]
        if posted + reversed_total != trace["final_total_fen"]:
            raise ConflictError("计费分录金额不守恒，已回滚")
        connection.execute(
            "UPDATE trips SET state='billed',current_version=?,exit_at=?,exit_evidence_ref=?,"
            "path_json=?,has_late_event=? WHERE trip_id=?",
            (new_version, exit_at.isoformat(), exit_ref, canonical_json(path),
             1 if event_kind == "supplemental_exit" else trip["has_late_event"], trip["trip_id"]),
        )
        append_event(connection, actor_id=actor_id,
                     action="trip.billed" if new_version == 1 else "trip.rebilled",
                     resource_type="trip", resource_id=trip["trip_id"],
                     detail={"version": new_version, "event_kind": event_kind, "sequence": sequence,
                             "exit_at": exit_at.isoformat(),
                             "base_total_fen": trace["base_total_fen"],
                             "final_total_fen": trace["final_total_fen"],
                             "policies": [step["policy_id"] for step in trace["steps"] if step["applied"]]},
                     occurred_at=self._now())
        return new_version

    def _rebill(self, connection, trip, *, occurred_at: datetime | None, exit_ref: str | None,
                path: list[str], actor_id: str, event_kind: str, sequence: int) -> int:
        exit_at = datetime.fromisoformat(trip["exit_at"]) if trip["exit_at"] else occurred_at
        if exit_at is None:
            raise ValidationError("行程缺少出口时间，无法重新计费")
        # 夜间等本地时段规则以原出口事件的当地墙钟时间为准，绕行确认不改变出口时点。
        local_row = connection.execute(
            "SELECT event_local_at FROM trip_events WHERE trip_id=? "
            "AND kind IN ('exit','supplemental_exit') AND event_local_at IS NOT NULL "
            "ORDER BY sequence DESC LIMIT 1",
            (trip["trip_id"],),
        ).fetchone()
        charge_local_at = datetime.fromisoformat(local_row["event_local_at"]) if local_row else None
        return self._bill(connection, trip, exit_at=exit_at, exit_ref=exit_ref, path_override=path,
                          actor_id=actor_id, event_kind=event_kind, sequence=sequence,
                          charge_local_at=charge_local_at)

    # ------------------------------------------------------------- 退款/追缴

    def _operator_nets(self, connection, trip_id: str) -> dict[str, int]:
        nets: dict[str, int] = {}
        for row in connection.execute(
            "SELECT operator_id, SUM(amount_fen) AS net FROM ledger_entries WHERE trip_id=? GROUP BY operator_id",
            (trip_id,),
        ):
            nets[row["operator_id"]] = row["net"]
        return nets

    @staticmethod
    def _allocate(weights: dict[str, int], amount: int) -> dict[str, int]:
        """按权重以最大余数法分摊整数金额，分摊之和严格等于金额。"""

        total = sum(weights.values())
        if total <= 0 or amount <= 0:
            raise ConflictError("没有可分摊的正数金额")
        floors: dict[str, int] = {}
        remainders: list[tuple[int, str]] = []
        allocated = 0
        for operator_id, weight in weights.items():
            scaled = amount * weight
            floored = scaled // total
            floors[operator_id] = floored
            remainders.append((scaled - floored * total, operator_id))
            allocated += floored
        left = amount - allocated
        for _, operator_id in sorted(remainders, key=lambda item: (-item[0], item[1])):
            if left <= 0:
                break
            floors[operator_id] += 1
            left -= 1
        return floors

    def register_adjustment(self, *, request_id: str, actor_id: str, trip_id: str, kind: str,
                            amount_fen: int, reason: str) -> Any:
        payload = {"actor_id": actor_id, "trip_id": trip_id, "kind": kind,
                   "amount_fen": amount_fen, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            trip_id = self._identifier(trip_id, "trip_id")
            if kind not in ADJUSTMENT_KINDS:
                raise ValidationError("kind 必须是 refund/recovery")
            amount_fen = self._amount(amount_fen, "amount_fen", minimum=1)
            reason = self._text(reason, "reason", 300)
            trip = connection.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
            if trip is None:
                raise NotFoundError("行程不存在")
            if trip["current_version"] == 0:
                raise BillingStateError("行程尚未生成计费事实，不能登记调整")
            if trip["state"] == "settled":
                raise AccountClosedError("行程已关账，调整不能改写已结算收入")
            if self._has_open_dispute(connection, trip_id):
                raise ConflictError("行程存在未解决争议，相关分录已冻结")
            nets = self._operator_nets(connection, trip_id)
            if kind == "refund":
                positive = {operator_id: net for operator_id, net in nets.items() if net > 0}
                if sum(positive.values()) < amount_fen:
                    raise ConflictError("退款金额不能超过各运营方当前应收净额合计")
                shares = self._allocate(positive, amount_fen)
            else:
                weights = {operator_id: max(net, 0) for operator_id, net in nets.items()}
                if sum(weights.values()) <= 0:
                    # 净额已被全部冲减时，按当前计费版本的原始收费额确定追缴权重。
                    weights = {}
                    for row in connection.execute(
                        "SELECT operator_id, SUM(amount_fen) AS gross FROM ledger_entries "
                        "WHERE trip_id=? AND fact_version=? AND kind='charge' GROUP BY operator_id",
                        (trip_id, trip["current_version"]),
                    ):
                        if row["gross"] > 0:
                            weights[row["operator_id"]] = row["gross"]
                shares = self._allocate(weights, amount_fen)

            def create() -> tuple[str, str, dict[str, Any]]:
                adjustment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO adjustments(adjustment_id,trip_id,kind,amount_fen,reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (adjustment_id, trip_id, kind, amount_fen, reason, actor_id, self._now()),
                )
                for operator_id, share in shares.items():
                    signed = -share if kind == "refund" else share
                    connection.execute(
                        "INSERT INTO ledger_entries(entry_id,trip_id,fact_version,segment_id,adjustment_id,"
                        "operator_id,kind,amount_fen,frozen,period_id,created_at) "
                        "VALUES(?,?,NULL,NULL,?,?,?,?,0,NULL,?)",
                        (uuid.uuid4().hex, trip_id, adjustment_id, operator_id, kind,
                         signed, self._now()),
                    )
                total = sum(self._operator_nets(connection, trip_id).values())
                append_event(connection, actor_id=actor_id, action="adjustment.registered",
                             resource_type="adjustment", resource_id=adjustment_id,
                             detail={"trip_id": trip_id, "kind": kind, "amount_fen": amount_fen,
                                     "shares": shares, "trip_net_after_fen": total, "reason": reason},
                             occurred_at=self._now())
                return "adjustment", adjustment_id, {"adjustment_id": adjustment_id, "trip_id": trip_id,
                                                      "kind": kind, "amount_fen": amount_fen,
                                                      "operator_shares_fen": shares,
                                                      "trip_net_after_fen": total}

            return self._idempotent(connection, request_id=request_id, action="register_adjustment",
                                    payload=payload, create=create)

    # ------------------------------------------------------------- 争议冻结

    def open_dispute(self, *, request_id: str, actor_id: str, trip_id: str, reason: str) -> Any:
        payload = {"actor_id": actor_id, "trip_id": trip_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            trip_id = self._identifier(trip_id, "trip_id")
            reason = self._text(reason, "reason", 300)
            if connection.execute("SELECT 1 FROM trips WHERE trip_id=?", (trip_id,)).fetchone() is None:
                raise NotFoundError("行程不存在")
            if self._has_open_dispute(connection, trip_id):
                raise ConflictError("该行程已经存在未解决争议")

            def create() -> tuple[str, str, dict[str, Any]]:
                dispute_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO disputes(dispute_id,trip_id,status,reason,opened_by,opened_at) "
                    "VALUES(?,?, 'open',?,?,?)",
                    (dispute_id, trip_id, reason, actor_id, self._now()),
                )
                updated = connection.execute(
                    "UPDATE ledger_entries SET frozen=1 WHERE trip_id=?", (trip_id,)
                )
                append_event(connection, actor_id=actor_id, action="dispute.opened",
                             resource_type="dispute", resource_id=dispute_id,
                             detail={"trip_id": trip_id, "frozen_entries": updated.rowcount},
                             occurred_at=self._now())
                return "dispute", dispute_id, {"dispute_id": dispute_id, "trip_id": trip_id,
                                               "status": "open", "frozen_entries": updated.rowcount}

            return self._idempotent(connection, request_id=request_id, action="open_dispute",
                                    payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        resolution: str) -> Any:
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "resolution": resolution}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            dispute_id = self._identifier(dispute_id, "dispute_id")
            resolution = self._text(resolution, "resolution", 300)
            dispute = connection.execute("SELECT * FROM disputes WHERE dispute_id=?",
                                         (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("争议不存在")
            if dispute["status"] != "open":
                raise ConflictError("争议已经解决")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE disputes SET status='resolved',resolved_by=?,resolved_at=?,resolution=? "
                    "WHERE dispute_id=?",
                    (actor_id, self._now(), resolution, dispute_id),
                )
                unfrozen = connection.execute(
                    "UPDATE ledger_entries SET frozen=0 WHERE trip_id=? AND frozen=1", (dispute["trip_id"],)
                )
                append_event(connection, actor_id=actor_id, action="dispute.resolved",
                             resource_type="dispute", resource_id=dispute_id,
                             detail={"trip_id": dispute["trip_id"], "unfrozen_entries": unfrozen.rowcount,
                                     "resolution": resolution},
                             occurred_at=self._now())
                return "dispute", dispute_id, {"dispute_id": dispute_id, "trip_id": dispute["trip_id"],
                                               "status": "resolved",
                                               "unfrozen_entries": unfrozen.rowcount}

            return self._idempotent(connection, request_id=request_id, action="resolve_dispute",
                                    payload=payload, create=create)

    # ------------------------------------------------------------- 结算关账

    def create_period(self, *, request_id: str, actor_id: str, period_id: str, name: str) -> Any:
        payload = {"actor_id": actor_id, "period_id": period_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            period_id = self._identifier(period_id, "period_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO settlement_periods(period_id,name,status,created_by,created_at) "
                        "VALUES(?,?,'open',?,?)",
                        (period_id, name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("结算期编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="period.created",
                             resource_type="settlement_period", resource_id=period_id,
                             detail={"name": name}, occurred_at=self._now())
                return "settlement_period", period_id, {"period_id": period_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id, action="create_period",
                                    payload=payload, create=create)

    def settle_trip(self, *, request_id: str, actor_id: str, trip_id: str, period_id: str) -> Any:
        payload = {"actor_id": actor_id, "trip_id": trip_id, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            trip_id = self._identifier(trip_id, "trip_id")
            period_id = self._identifier(period_id, "period_id")
            trip = connection.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
            if trip is None:
                raise NotFoundError("行程不存在")
            period = connection.execute("SELECT * FROM settlement_periods WHERE period_id=?",
                                        (period_id,)).fetchone()
            if period is None:
                raise NotFoundError("结算期不存在")
            if period["status"] != "open":
                raise AccountClosedError("结算期已经关账")
            if trip["state"] == "settled":
                raise ConflictError("行程已经结算")
            if trip["current_version"] == 0:
                raise BillingStateError("行程仍处于待补证状态，不能结算")
            if self._has_open_dispute(connection, trip_id):
                raise ConflictError("行程存在未解决争议，相关分录已冻结，不能结算")

            def create() -> tuple[str, str, dict[str, Any]]:
                net = connection.execute(
                    "SELECT COALESCE(SUM(amount_fen),0) AS net FROM ledger_entries WHERE trip_id=?",
                    (trip_id,),
                ).fetchone()["net"]
                connection.execute(
                    "UPDATE ledger_entries SET period_id=? WHERE trip_id=? AND period_id IS NULL",
                    (period_id, trip_id),
                )
                connection.execute(
                    "INSERT INTO settlements(trip_id,period_id,fact_version,total_fen,settled_at) "
                    "VALUES(?,?,?,?,?)",
                    (trip_id, period_id, trip["current_version"], net, self._now()),
                )
                connection.execute(
                    "UPDATE trips SET state='settled',settlement_period_id=?,settled_at=? WHERE trip_id=?",
                    (period_id, self._now(), trip_id),
                )
                append_event(connection, actor_id=actor_id, action="trip.settled",
                             resource_type="trip", resource_id=trip_id,
                             detail={"period_id": period_id, "fact_version": trip["current_version"],
                                     "total_fen": net},
                             occurred_at=self._now())
                return "settlement", f"{period_id}:{trip_id}", {"trip_id": trip_id,
                                                                "period_id": period_id,
                                                                "fact_version": trip["current_version"],
                                                                "total_fen": net}

            return self._idempotent(connection, request_id=request_id, action="settle_trip",
                                    payload=payload, create=create)

    def close_period(self, *, request_id: str, actor_id: str, period_id: str) -> Any:
        payload = {"actor_id": actor_id, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            period_id = self._identifier(period_id, "period_id")
            period = connection.execute("SELECT * FROM settlement_periods WHERE period_id=?",
                                        (period_id,)).fetchone()
            if period is None:
                raise NotFoundError("结算期不存在")
            if period["status"] != "open":
                raise ConflictError("结算期已经关账")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE settlement_periods SET status='closed',cutoff_at=?,closed_at=? WHERE period_id=?",
                    (self._now(), self._now(), period_id),
                )
                append_event(connection, actor_id=actor_id, action="period.closed",
                             resource_type="settlement_period", resource_id=period_id,
                             detail={"closed_at": self._now()}, occurred_at=self._now())
                return "settlement_period", period_id, {"period_id": period_id, "status": "closed"}

            return self._idempotent(connection, request_id=request_id, action="close_period",
                                    payload=payload, create=create)

    # ------------------------------------------------------------- 查询/解释

    def explain_trip(self, trip_id: str) -> dict[str, Any]:
        connection = self.database.connection
        trip = connection.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFoundError("行程不存在")
        facts = [
            {"version": row["version"], "base_total_fen": row["base_total_fen"],
             "final_total_fen": row["final_total_fen"], "created_at": row["created_at"],
             "trace": json.loads(row["trace_json"])}
            for row in connection.execute("SELECT * FROM billing_facts WHERE trip_id=? ORDER BY version",
                                          (trip_id,))
        ]
        current = facts[-1] if facts else None
        operator_shares: dict[str, dict[str, int]] = {}
        for row in connection.execute("SELECT * FROM ledger_entries WHERE trip_id=? ORDER BY created_at, entry_id",
                                      (trip_id,)):
            bucket = operator_shares.setdefault(row["operator_id"], {
                "charge_fen": 0, "charge_reversal_fen": 0, "refund_fen": 0,
                "recovery_fen": 0, "net_fen": 0, "frozen_fen": 0,
            })
            key = {"charge": "charge_fen", "charge_reversal": "charge_reversal_fen",
                   "refund": "refund_fen", "recovery": "recovery_fen"}[row["kind"]]
            bucket[key] += row["amount_fen"]
            bucket["net_fen"] += row["amount_fen"]
            if row["frozen"]:
                bucket["frozen_fen"] += abs(row["amount_fen"])
        adjustments = [
            {"adjustment_id": row["adjustment_id"], "kind": row["kind"],
             "amount_fen": row["amount_fen"], "reason": row["reason"], "created_at": row["created_at"]}
            for row in connection.execute("SELECT * FROM adjustments WHERE trip_id=? ORDER BY created_at",
                                          (trip_id,))
        ]
        disputes = [
            {"dispute_id": row["dispute_id"], "status": row["status"], "reason": row["reason"],
             "opened_at": row["opened_at"], "resolution": row["resolution"]}
            for row in connection.execute("SELECT * FROM disputes WHERE trip_id=? ORDER BY opened_at",
                                          (trip_id,))
        ]
        return {
            "trip_id": trip["trip_id"], "vehicle_id": trip["vehicle_id"],
            "vehicle_class": trip["vehicle_class"], "tags": json.loads(trip["vehicle_tags_json"]),
            "entry_plaza": trip["entry_plaza"], "entry_at": trip["entry_at"],
            "exit_at": trip["exit_at"], "state": trip["state"],
            "current_fact_version": trip["current_version"],
            "has_late_event": bool(trip["has_late_event"]),
            "settlement_period_id": trip["settlement_period_id"], "settled_at": trip["settled_at"],
            "path": json.loads(trip["path_json"]) if trip["path_json"] else [],
            "current_amount_fen": current["final_total_fen"] if current else None,
            "current_trace": current["trace"] if current else None,
            "fact_versions": [{"version": item["version"], "base_total_fen": item["base_total_fen"],
                               "final_total_fen": item["final_total_fen"], "created_at": item["created_at"]}
                              for item in facts],
            "operator_shares_fen": operator_shares,
            "adjustments": adjustments,
            "disputes": disputes,
        }

    def replay_trip(self, trip_id: str, as_of: str) -> dict[str, Any]:
        """按历史时点重放：仅使用该时点之前的事件与当时有效政策重新求值。"""

        as_of_dt = self._ts(as_of, "as_of")
        connection = self.database.connection
        trip = connection.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFoundError("行程不存在")
        events = []
        for row in connection.execute(
            "SELECT * FROM trip_events WHERE trip_id=? ORDER BY sequence", (trip_id,)
        ):
            if datetime.fromisoformat(row["recorded_at"]) <= as_of_dt:
                events.append({"kind": row["kind"], "sequence": row["sequence"],
                               "event_occurred_at": row["event_occurred_at"],
                               "event_local_at": row["event_local_at"],
                               "data": json.loads(row["payload_json"])})
        path = None
        exit_at = None
        exit_local_at = None
        exit_kind = None
        for event in events:
            if event["kind"] == "detour" and event["data"].get("path"):
                path = event["data"]["path"]
            if event["kind"] in EXIT_KINDS:
                exit_at = datetime.fromisoformat(event["event_occurred_at"])
                exit_local_at = datetime.fromisoformat(event["event_local_at"]) if event.get("event_local_at") else None
                exit_kind = event["kind"]
                if event["data"].get("path"):
                    path = event["data"]["path"]
        if path is None and trip["entry_segment_id"]:
            path = [trip["entry_segment_id"]]
        facts_as_of = [
            row["version"] for row in connection.execute(
                "SELECT version, created_at FROM billing_facts WHERE trip_id=? ORDER BY version",
                (trip_id,),
            )
            if datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")) <= as_of_dt
        ]
        response: dict[str, Any] = {
            "trip_id": trip_id, "as_of": as_of_dt.isoformat(),
            "events_visible": events, "recorded_fact_versions": facts_as_of,
            "state_at_time": "billed" if facts_as_of else "pending_evidence",
        }
        if exit_at is None or not path:
            response["recomputed"] = None
            response["note"] = "该时点之前缺少出口或可计费路径，行程处于待补证状态"
            return response
        segment_rows = {
            row["segment_id"]: row
            for row in connection.execute("SELECT * FROM road_segments WHERE active=1")
        }
        missing = [segment_id for segment_id in path if segment_id not in segment_rows]
        if missing:
            response["recomputed"] = None
            response["note"] = f"路段当前不可用：{','.join(missing)}"
            return response
        lines = [
            ChargeLine(segment_id=segment_id, operator_id=segment_rows[segment_id]["organization_id"],
                       base_amount_fen=segment_rows[segment_id]["base_amount_fen"])
            for segment_id in path
        ]
        policies = self._policies_in_force(connection, exit_at, as_of_dt)
        vehicle = {"vehicle_id": trip["vehicle_id"], "vehicle_class": trip["vehicle_class"],
                   "tags": json.loads(trip["vehicle_tags_json"])}
        trace = evaluate_charge(lines, vehicle, bool(trip["is_holiday"]),
                                exit_local_at or exit_at, policies)
        response["recomputed"] = trace
        response["exit_event_kind"] = exit_kind
        return response

    def operator_reconciliation(self, period_id: str) -> dict[str, Any]:
        """汇总一个结算期内每家运营方的应收、冲回、退款、追缴、净额与冻结额。"""

        connection = self.database.connection
        if connection.execute("SELECT 1 FROM settlement_periods WHERE period_id=?",
                              (period_id,)).fetchone() is None:
            raise NotFoundError("结算期不存在")
        operators: dict[str, dict[str, int]] = {}
        for row in connection.execute(
            "SELECT * FROM ledger_entries WHERE period_id=? ORDER BY operator_id", (period_id,)
        ):
            bucket = operators.setdefault(row["operator_id"], {
                "operator_id": row["operator_id"], "charge_fen": 0, "charge_reversal_fen": 0,
                "refund_fen": 0, "recovery_fen": 0, "net_fen": 0, "frozen_fen": 0,
            })
            key = {"charge": "charge_fen", "charge_reversal": "charge_reversal_fen",
                   "refund": "refund_fen", "recovery": "recovery_fen"}[row["kind"]]
            bucket[key] += row["amount_fen"]
            bucket["net_fen"] += row["amount_fen"]
            if row["frozen"]:
                bucket["frozen_fen"] += abs(row["amount_fen"])
        items = sorted(operators.values(), key=lambda item: item["operator_id"])
        totals = {
            "charge_fen": sum(item["charge_fen"] for item in items),
            "charge_reversal_fen": sum(item["charge_reversal_fen"] for item in items),
            "refund_fen": sum(item["refund_fen"] for item in items),
            "recovery_fen": sum(item["recovery_fen"] for item in items),
            "net_fen": sum(item["net_fen"] for item in items),
            "frozen_fen": sum(item["frozen_fen"] for item in items),
        }
        return {"period_id": period_id, "operators": items, "totals": totals}
