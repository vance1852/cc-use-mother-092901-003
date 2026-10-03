"""运行收费政策执行与清算服务的离线端到端验收。

场景覆盖：差异化政策发布、缺失出口待补证、跨路段封路绕行重计费、运营方分账守恒、
退款分摊、结算关账、迟到事件不可改写、政策撤回范围、争议冻结范围与历史时点重放。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .billing import BillingService
from .clock import FixedClock
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整的收费清算链并返回核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "billing_acceptance.sqlite3")
        start = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
        service = BillingService(database, FixedClock(start))

        def tick(**kwargs) -> None:
            service.clock = FixedClock(service.clock.now() + timedelta(**kwargs))

        # 两个经营主体与操作者
        service.register_organization(request_id="req-org-1", actor_id="bootstrap",
                                      organization_id="op-east", name="东段高速公司")
        service.register_organization(request_id="req-org-2", actor_id="bootstrap",
                                      organization_id="op-west", name="西段高速公司")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin",
                               display_name="收费管理员", role="admin", organization_id="op-east")
        service.register_actor(request_id="req-cashier", actor_id="admin", new_actor_id="cashier",
                               display_name="站点收费员", role="operator", organization_id="op-east")
        service.register_actor(request_id="req-reviewer", actor_id="admin", new_actor_id="reviewer",
                               display_name="稽核员", role="reviewer", organization_id="op-east")

        # 跨路段路网：东段 100 元、西段 200 元、西段备用绕行段 50 元
        service.register_segment(request_id="req-seg-1", actor_id="admin", segment_id="seg-east",
                                 organization_id="op-east", name="东主线", base_amount_fen=10000)
        service.register_segment(request_id="req-seg-2", actor_id="admin", segment_id="seg-west",
                                 organization_id="op-west", name="西主线", base_amount_fen=20000)
        service.register_segment(request_id="req-seg-3", actor_id="admin", segment_id="seg-west-alt",
                                 organization_id="op-west", name="西绕行线", base_amount_fen=5000)

        # 差异化政策：民生资格 → 全免（优先级 5）；节假日 9 折（10）；夜间货运 8 折（20）；全程封顶（50）
        service.publish_policy(request_id="req-pol-elig", actor_id="admin", policy_id="green-pass",
                               kind="eligibility", priority=1, name="绿通车辆资格",
                               conditions={"vehicle_classes": ["fresh_goods"]},
                               effect={"grant_tags": ["livelihood"]},
                               effective_from="2026-01-01T00:00:00+08:00")
        service.publish_policy(request_id="req-pol-exempt", actor_id="admin", policy_id="livelihood-free",
                               kind="exemption", priority=5, name="民生车辆免费",
                               conditions={"tags": ["livelihood"]}, effect={"type": "full"},
                               effective_from="2026-01-01T00:00:00+08:00")
        service.publish_policy(request_id="req-pol-holiday", actor_id="admin", policy_id="holiday-90",
                               kind="rate", priority=10, name="节假日九折",
                               conditions={"is_holiday": True},
                               effect={"type": "discount_pct", "percent": 10},
                               effective_from="2026-01-01T00:00:00+08:00")
        service.publish_policy(request_id="req-pol-night", actor_id="admin", policy_id="night-freight-80",
                               kind="rate", priority=20, name="夜间货运八折",
                               conditions={"vehicle_classes": ["freight"], "night": True},
                               effect={"type": "discount_pct", "percent": 20},
                               effective_from="2026-01-01T00:00:00+08:00")
        service.publish_policy(request_id="req-pol-cap", actor_id="admin", policy_id="whole-trip-cap",
                               kind="cap", priority=50, name="全程封顶270元",
                               conditions={}, effect={"type": "amount_cap", "amount_fen": 27000},
                               effective_from="2026-01-01T00:00:00+08:00")

        # 行程一：节假日小客车跨东西两段，入口证据齐全但出口设备故障，先进入待补证
        tick(minutes=10)
        service.record_trip_event(request_id="req-entry-1", actor_id="cashier", trip_id="trip-001",
                                  kind="entry", event_occurred_at="2026-10-02T08:00:00+08:00",
                                  evidence_ref="GATE-E-1001",
                                  data={"vehicle_id": "蓝牌A1001", "vehicle_class": "car",
                                        "entry_plaza": "东入口01", "entry_segment_id": "seg-east",
                                        "is_holiday": True, "tags": []})
        pending = service.explain_trip("trip-001")
        assert pending["state"] == "pending_evidence" and pending["current_amount_fen"] is None

        # 补录出口：主线西段封闭，实际经西绕行线驶出
        tick(hours=1, minutes=30)
        service.record_trip_event(request_id="req-supp-1", actor_id="cashier", trip_id="trip-001",
                                  kind="supplemental_exit",
                                  event_occurred_at="2026-10-02T09:20:00+08:00",
                                  evidence_ref="GATE-X-2001-MANUAL",
                                  data={"path": ["seg-east", "seg-west"]})
        first_bill = service.explain_trip("trip-001")
        # 节假日 9 折后 27000，恰好等于封顶，封顶不再产生减免
        assert first_bill["current_amount_fen"] == 27000, first_bill["current_amount_fen"]
        assert first_bill["operator_shares_fen"]["op-east"]["net_fen"] == 9000
        assert first_bill["operator_shares_fen"]["op-west"]["net_fen"] == 18000
        applied_order = [(step["order"], step["kind"], step["policy_id"])
                         for step in first_bill["current_trace"]["steps"] if step["applied"]]
        assert applied_order == [(3, "rate", "holiday-90")], applied_order

        # 封路绕行确认：生成第二版计费事实，冲回第一版分录
        tick(minutes=20)
        service.record_trip_event(request_id="req-detour-1", actor_id="reviewer", trip_id="trip-001",
                                  kind="detour", event_occurred_at="2026-10-02T09:40:00+08:00",
                                  evidence_ref="DETOUR-WEST-77",
                                  data={"path": ["seg-east", "seg-west-alt"]})
        second_bill = service.explain_trip("trip-001")
        assert second_bill["current_fact_version"] == 2
        # 节假日 9 折：东 9000 + 绕行 4500 = 13500，未触封顶
        assert second_bill["current_amount_fen"] == 13500, second_bill["current_amount_fen"]
        # 守恒：两版收费 - 冲回 = 当前金额
        ledger_total = database.connection.execute(
            "SELECT COALESCE(SUM(amount_fen),0) AS total FROM ledger_entries WHERE trip_id='trip-001'"
        ).fetchone()["total"]
        assert ledger_total == 13500

        # 设备故障退款 15 元，按净额 2:1 分摊回两个经营主体
        refund = service.register_adjustment(request_id="req-refund-1", actor_id="reviewer",
                                             trip_id="trip-001", kind="refund", amount_fen=1500,
                                             reason="出口设备故障服务补偿")
        assert sum(refund.response["operator_shares_fen"].values()) == 1500
        net_after_refund = sum(
            bucket["net_fen"]
            for bucket in service.explain_trip("trip-001")["operator_shares_fen"].values()
        )
        assert net_after_refund == 12000

        # 进入结算期并关账
        service.create_period(request_id="req-period", actor_id="admin", period_id="period-2026-10",
                              name="2026年10月清算期")
        service.settle_trip(request_id="req-settle-1", actor_id="admin", trip_id="trip-001",
                            period_id="period-2026-10")

        # 关账后又收到迟到的路径更正：只能登记，不得改写已关账收入
        tick(days=3)
        late = service.record_trip_event(request_id="req-late-1", actor_id="cashier",
                                         trip_id="trip-001", kind="late_notice",
                                         event_occurred_at="2026-10-05T08:00:00+08:00",
                                         evidence_ref="LATE-PATCH-9", data={})
        assert late.response["amount_changed"] is False
        settled = service.explain_trip("trip-001")
        assert settled["state"] == "settled" and settled["has_late_event"]
        assert settled["current_amount_fen"] == 13500

        reconciliation = service.operator_reconciliation("period-2026-10")
        assert reconciliation["totals"]["net_fen"] == 12000
        service.close_period(request_id="req-close", actor_id="admin", period_id="period-2026-10")

        # 政策撤回只影响未结算行程：撤回节假日折扣与封顶后，新行程恢复原价
        service.withdraw_policy(request_id="req-withdraw-holiday", actor_id="admin",
                                policy_id="holiday-90")
        service.withdraw_policy(request_id="req-withdraw-cap", actor_id="admin",
                                policy_id="whole-trip-cap")
        service.record_trip_event(request_id="req-entry-2", actor_id="cashier", trip_id="trip-002",
                                  kind="entry", event_occurred_at="2026-10-06T08:00:00+08:00",
                                  evidence_ref="GATE-E-1002",
                                  data={"vehicle_id": "蓝牌A1002", "vehicle_class": "car",
                                        "entry_plaza": "东入口01", "entry_segment_id": "seg-east",
                                        "is_holiday": True, "tags": []})
        tick(hours=1)
        service.record_trip_event(request_id="req-exit-2", actor_id="cashier", trip_id="trip-002",
                                  kind="exit", event_occurred_at="2026-10-06T09:00:00+08:00",
                                  evidence_ref="GATE-X-2002",
                                  data={"path": ["seg-east", "seg-west"]})
        assert service.explain_trip("trip-002")["current_amount_fen"] == 30000
        # trip-001 历史时点重放：补录后、绕行前只能看到第一版 27000
        replay_v1 = service.replay_trip("trip-001", "2026-10-02T02:45:00+00:00")
        assert replay_v1["recorded_fact_versions"] == [1]
        assert replay_v1["recomputed"]["final_total_fen"] == 27000
        # 绕行确认后重放得到第二版 13500；即使节假日折扣后来被撤回，该时点仍然有效
        replay = service.replay_trip("trip-001", "2026-10-02T03:30:00+00:00")
        assert replay["recorded_fact_versions"] == [1, 2]
        assert replay["recomputed"]["final_total_fen"] == 13500

        # 争议：trip-002 被投诉，仅冻结 trip-002 的分录，不影响 trip-001 的对账
        service.open_dispute(request_id="req-dispute-2", actor_id="reviewer", trip_id="trip-002",
                             reason="车主不认可绕行计费")
        frozen_total = database.connection.execute(
            "SELECT COALESCE(SUM(ABS(amount_fen)),0) AS total FROM ledger_entries WHERE frozen=1"
        ).fetchone()["total"]
        assert frozen_total == 30000
        dispute_affected_settled = database.connection.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries WHERE trip_id='trip-001' AND frozen=1"
        ).fetchone()["count"]
        assert dispute_affected_settled == 0

        # 夜间货运行程：无节假日标志、23 点出口 → 8 折
        service.record_trip_event(request_id="req-entry-3", actor_id="cashier", trip_id="trip-003",
                                  kind="entry", event_occurred_at="2026-10-06T22:30:00+08:00",
                                  evidence_ref="GATE-E-1003",
                                  data={"vehicle_id": "黄牌B2003", "vehicle_class": "freight",
                                        "entry_plaza": "东入口01", "entry_segment_id": "seg-east",
                                        "is_holiday": False, "tags": []})
        service.record_trip_event(request_id="req-exit-3", actor_id="cashier", trip_id="trip-003",
                                  kind="exit", event_occurred_at="2026-10-06T23:10:00+08:00",
                                  evidence_ref="GATE-X-2003",
                                  data={"path": ["seg-east", "seg-west"]})
        night_amount = service.explain_trip("trip-003")["current_amount_fen"]
        # 30000 打 8 折 = 24000，未触封顶
        assert night_amount == 24000, night_amount

        # 民生绿通车辆：资格规则授予标签后全免
        service.record_trip_event(request_id="req-entry-4", actor_id="cashier", trip_id="trip-004",
                                  kind="entry", event_occurred_at="2026-10-07T08:00:00+08:00",
                                  evidence_ref="GATE-E-1004",
                                  data={"vehicle_id": "绿牌C3004", "vehicle_class": "fresh_goods",
                                        "entry_plaza": "东入口01", "entry_segment_id": "seg-east",
                                        "is_holiday": False, "tags": []})
        service.record_trip_event(request_id="req-exit-4", actor_id="cashier", trip_id="trip-004",
                                  kind="exit", event_occurred_at="2026-10-07T09:00:00+08:00",
                                  evidence_ref="GATE-X-2004",
                                  data={"path": ["seg-east", "seg-west"]})
        exempt_amount = service.explain_trip("trip-004")["current_amount_fen"]
        assert exempt_amount == 0, exempt_amount

        audit_valid, audit_events = service.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": audit_events,
            "pending_then_supplemental": pending["state"] == "pending_evidence",
            "first_bill_fen": first_bill["current_amount_fen"],
            "final_bill_fen": second_bill["current_amount_fen"],
            "fact_versions": second_bill["current_fact_version"],
            "refund_shares_fen": refund.response["operator_shares_fen"],
            "settled_net_fen": reconciliation["totals"]["net_fen"],
            "late_event_immutable": settled["current_amount_fen"] == 13500,
            "withdraw_new_trip_fen": 30000,
            "night_freight_fen": night_amount,
            "livelihood_fen": exempt_amount,
            "dispute_froze_only_related": frozen_total == 30000,
            "replay_fen": replay["recomputed"]["final_total_fen"],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
