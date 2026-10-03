"""收费政策执行与清算服务的离线端到端验收。

覆盖主叙事：
- 节假日折扣 + 夜间货运折扣 + 绿通豁免 + 整程封顶的优惠顺序；
- 跨路段行程缺出口进入待补证，出口补录后生成不可重复计费事实；
- 关账后迟到出口不得改写收入，只能走退款/追缴；
- 政策撤回只重算未结算行程，已关账金额不变；
- 结算/退款复式分录逐分守恒，退款按收入比例分摊回经营主体；
- 争议只冻结相关行程分录；
- explain 可解释金额来源，replay 可按历史时点重放政策与清分。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError
from .storage import Database
from .toll_service import TollService

HOLIDAY_START = "2026-10-01T00:00:00Z"
HOLIDAY_END = "2026-10-08T00:00:00Z"
EXIT_TIME = "2026-10-01T23:30:00Z"      # 假期内且处于夜间窗口
EARLY_TIME = "2025-12-31T12:00:00Z"     # 任何政策尚未生效


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "toll_acceptance.sqlite3")
        service = TollService(database, FixedClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)))

        def tick(day: int, hour: int = 12, minute: int = 0) -> None:
            service.clock = FixedClock(
                datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc))

        # ---- 主体与人员 ----
        service.register_organization(request_id="org-o1", actor_id="bootstrap",
                                      organization_id="o1", name="东段运营公司")
        service.register_organization(request_id="org-o2", actor_id="bootstrap",
                                      organization_id="o2", name="西段运营公司")
        service.register_organization(request_id="org-gov", actor_id="bootstrap",
                                      organization_id="gov", name="省级收费政策中心")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                               display_name="管理员", role="admin", organization_id="gov")
        service.register_actor(request_id="policy", actor_id="admin-1", new_actor_id="policy-1",
                               display_name="政策审核员", role="reviewer", organization_id="gov")
        service.register_actor(request_id="ops1", actor_id="admin-1", new_actor_id="ops-1",
                               display_name="东段收费员", role="operator", organization_id="o1")

        # ---- 路段：东段 100km 0.50 元/km；西段 60km 0.60 元/km ----
        service.register_segment(request_id="seg-a", actor_id="ops-1", segment_id="seg-a",
                                 organization_id="o1", name="东段",
                                 rate_fen_per_km=50, length_m=100_000)
        service.register_segment(request_id="seg-b", actor_id="admin-1", segment_id="seg-b",
                                 organization_id="o2", name="西段",
                                 rate_fen_per_km=60, length_m=60_000)

        # ---- 政策（生效区间 + 优先级），优惠由政策中心承担 ----
        service.publish_policy(request_id="p-green", actor_id="policy-1", policy_id="p-green",
                               kind="exemption", priority=10, scope={"tags": ["fresh_green"]},
                               params={"label": "绿通民生车辆豁免"}, effective_at="2026-01-01T00:00:00Z",
                               published_org="gov")
        service.publish_policy(request_id="p-night", actor_id="policy-1", policy_id="p-night",
                               kind="discount", priority=50,
                               scope={"vehicle_classes": ["truck"], "hours": [22, 6]},
                               params={"mode": "percent", "permille": 800, "label": "夜间货运八折"},
                               effective_at="2026-01-01T00:00:00Z", published_org="gov")
        service.publish_policy(request_id="p-holiday", actor_id="policy-1", policy_id="p-holiday",
                               kind="discount", priority=100, scope={},
                               params={"mode": "percent", "permille": 900, "label": "节假日九折"},
                               effective_at=HOLIDAY_START, expires_at=HOLIDAY_END,
                               published_org="gov")
        service.publish_policy(request_id="p-cap", actor_id="policy-1", policy_id="p-cap",
                               kind="cap", priority=1000, scope={},
                               params={"max_fen": 7000, "label": "单程封顶70元"},
                               effective_at="2026-01-01T00:00:00Z", published_org="gov")

        # ---- 行程一：客车跨东西两段，假期夜间出行，出口先缺失 ----
        tick(1, 22, 12)
        service.open_trip(request_id="trip-1", actor_id="ops-1", trip_id="trip-1",
                          vehicle_plate="京A12345", vehicle_class="passenger",
                          segment_ids=["seg-a", "seg-b"], entry_time="2026-10-01T22:10:00Z",
                          tags=[])
        service.mark_pending_evidence(request_id="trip-1-pending", actor_id="ops-1", trip_id="trip-1")
        tick(1, 23, 31)
        exit1 = service.record_exit(request_id="trip-1-exit", actor_id="ops-1", trip_id="trip-1",
                                    event_time=EXIT_TIME, evidence_hash="hash-exit-1")
        # 5000+3600=8600；九折后 7740；整程封顶 7000
        assert exit1["total_fen"] == 7000, exit1

        # ---- 关账：收入归运营方、1600 分优惠归政策中心，借贷守恒 ----
        tick(2, 9, 0)
        settle1 = service.settle_trip(request_id="trip-1-settle", actor_id="ops-1", trip_id="trip-1")
        assert settle1["gross_fen"] == 8600 and settle1["total_fen"] == 7000, settle1

        # ---- 行程二：货运绿通车，夜间出行，豁免全额优惠由政策中心承担 ----
        tick(2, 3, 5)  # 行程实际发生在凌晨，验收时钟在此回拨仅为模拟历史补录场景
        service.open_trip(request_id="trip-2", actor_id="ops-1", trip_id="trip-2",
                          vehicle_plate="冀B6688", vehicle_class="truck",
                          segment_ids=["seg-a", "seg-b"], entry_time="2026-10-02T03:00:00Z",
                          tags=["fresh_green"])
        tick(2, 4, 31)
        exit2 = service.record_exit(request_id="trip-2-exit", actor_id="ops-1", trip_id="trip-2",
                                    event_time="2026-10-02T04:30:00Z")
        assert exit2["total_fen"] == 0, exit2  # 绿通豁免
        tick(2, 9, 5)
        service.settle_trip(request_id="trip-2-settle", actor_id="ops-1", trip_id="trip-2")
        ledger2 = service.trip_ledger("trip-2")
        debit2 = sum(e.amount_fen for e in ledger2 if e.direction == "debit")
        credit2 = sum(e.amount_fen for e in ledger2 if e.direction == "credit")
        assert debit2 == credit2 == 8600, (debit2, credit2)
        grant2 = sum(e.amount_fen for e in ledger2 if e.account == "discount_grant")
        assert grant2 == 8600, grant2  # 优惠全部由政策中心承担

        # 已关账后迟到出口不得改写收入
        late_rejected = False
        tick(2, 10, 0)
        try:
            service.record_exit(request_id="trip-1-late", actor_id="ops-1", trip_id="trip-1",
                                event_time="2026-10-02T01:00:00Z",
                                supersedes_event_id=exit1["exit_event_id"])
        except ConflictError:
            late_rejected = True
        assert late_rejected

        # ---- 退款 10 元：按 o1/o2 结算收入比例分摊，凭证守恒 ----
        tick(2, 11, 0)
        refund = service.create_refund(request_id="trip-1-refund", actor_id="ops-1",
                                       trip_id="trip-1", amount_fen=1000,
                                       reason="设备多扣费")
        allocated = {line["organization_id"]: line["amount_fen"]
                     for line in refund["allocations"]}
        assert sum(allocated.values()) == 1000, allocated
        assert allocated == {"o1": 581, "o2": 419}, allocated

        # ---- 争议只冻结本行程分录，驳回后解冻 ----
        tick(2, 14, 0)
        service.open_dispute(request_id="d1", actor_id="policy-1", dispute_id="d1",
                             trip_id="trip-1", reason="车主对封路路段有异议")
        assert all(entry.frozen for entry in service.trip_ledger("trip-1"))
        tick(2, 16, 0)
        service.resolve_dispute(request_id="d1-resolve", actor_id="policy-1", dispute_id="d1",
                                resolution="rejected")
        assert all(not entry.frozen for entry in service.trip_ledger("trip-1"))

        # ---- 行程三：未结算行程遭遇政策撤回，重算为全价；行程一保持 7000 ----
        tick(3, 10, 5)
        service.open_trip(request_id="trip-3", actor_id="ops-1", trip_id="trip-3",
                          vehicle_plate="京C9999", vehicle_class="passenger",
                          segment_ids=["seg-a"], entry_time="2026-10-03T10:00:00Z", tags=[])
        tick(3, 11, 1)
        exit3 = service.record_exit(request_id="trip-3-exit", actor_id="ops-1", trip_id="trip-3",
                                    event_time="2026-10-03T11:00:00Z")
        assert exit3["total_fen"] == 4500, exit3  # 九折
        tick(3, 12, 0)
        service.withdraw_policy(request_id="p-holiday-off", actor_id="policy-1",
                                policy_id="p-holiday")
        fact3 = service.explain_trip("trip-3")["billing"]
        assert fact3["total_fen"] == 5000, fact3  # 撤回后全价
        assert service.explain_trip("trip-1")["billing"]["total_fen"] == 7000
        tick(3, 13, 0)
        service.settle_trip(request_id="trip-3-settle", actor_id="ops-1", trip_id="trip-3")

        # 追缴 2 元：新增应收并按收入比例分给运营方，凭证守恒
        tick(3, 14, 0)
        surcharge = service.create_surcharge(request_id="trip-3-surcharge", actor_id="ops-1",
                                             trip_id="trip-3", amount_fen=200,
                                             reason="出站核载升级")
        assert surcharge["amount_fen"] == 200
        assert surcharge["allocations"] == [{"organization_id": "o1", "amount_fen": 200}]

        # ---- 解释与历史重放 ----
        explanation = service.explain_trip("trip-1")
        chain = explanation["billing"]["detail"]["per_segment"][0]["discounts"]
        assert [step["policy_id"] for step in chain] == ["p-holiday"], chain
        # 关账前一刻：已有计费事实，但没有任何清分分录
        before_settle = service.replay_at("trip-1", as_of="2026-10-02T08:59:00Z")
        assert before_settle["billing"]["total_fen"] == 7000
        assert before_settle["trip_status_at"] == "billed"
        assert before_settle["ledger_entries"] == []
        # 关账后：清分结果已可见（仅看结算凭证，退款发生在 11:00）
        after_settle = service.replay_at("trip-1", as_of="2026-10-02T09:30:00Z")
        assert after_settle["trip_status_at"] == "settled"
        settled_debit = sum(e["amount_fen"] for e in after_settle["ledger_entries"]
                            if e["direction"] == "debit")
        settled_credit = sum(e["amount_fen"] for e in after_settle["ledger_entries"]
                             if e["direction"] == "credit")
        assert settled_debit == settled_credit == 8600
        # 任意政策生效前重算：只有路段备案费率 8600
        full_price = service.replay_pricing("trip-1", basis_time=EARLY_TIME)
        assert full_price["total_fen"] == 8600, full_price
        # 争议期间的历史时点重放应显示当时分录处于冻结状态
        during_dispute = service.replay_at("trip-1", as_of="2026-10-02T15:00:00Z")
        assert during_dispute["ledger_entries"] and all(e["frozen"]
                                                        for e in during_dispute["ledger_entries"])

        audit_valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": event_count,
            "trip1_billed_fen": 7000,
            "trip1_gross_fen": 8600,
            "trip1_refund_split": allocated,
            "trip2_settlement_balanced": debit2 == credit2 == 8600,
            "trip3_rebilled_fen_after_withdraw": fact3["total_fen"],
            "late_exit_on_settled_rejected": late_rejected,
            "replay_before_holiday_fen": full_price["total_fen"],
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
