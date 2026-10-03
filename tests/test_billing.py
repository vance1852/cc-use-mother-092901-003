"""收费政策引擎与计费清算服务的单元测试。"""

import unittest
from datetime import datetime, timezone

from transport_coordination.billing import BillingService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    AccountClosedError,
    BillingStateError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from transport_coordination.policy_engine import ChargeLine, Policy, evaluate_charge, is_night_hour
from transport_coordination.storage import Database


def make_policy(policy_id, kind, priority, conditions, effect, *, version=1):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Policy(policy_id=policy_id, version=version, kind=kind, priority=priority,
                  name=policy_id, conditions=conditions, effect=effect,
                  effective_from=base, effective_to=None, published_at=base)


class PolicyEngineTest(unittest.TestCase):
    def setUp(self):
        self.lines = [
            ChargeLine("seg-a", "o1", 10000),
            ChargeLine("seg-b", "o2", 20000),
        ]
        self.vehicle = {"vehicle_id": "v1", "vehicle_class": "freight", "tags": set()}
        self.at = datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)

    def test_night_window(self):
        self.assertTrue(is_night_hour(datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)))
        self.assertTrue(is_night_hour(datetime(2026, 10, 1, 5, 59, tzinfo=timezone.utc)))
        self.assertFalse(is_night_hour(datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)))
        self.assertFalse(is_night_hour(datetime(2026, 10, 1, 21, 59, tzinfo=timezone.utc)))

    def test_priority_order_eligibility_then_exemption_then_rate(self):
        policies = [
            make_policy("discount", "rate", 20, {"vehicle_classes": ["freight"]},
                        {"type": "discount_pct", "percent": 50}),
            make_policy("qualify", "eligibility", 1, {"vehicle_classes": ["freight"]},
                        {"grant_tags": ["livelihood"]}),
            make_policy("exempt", "exemption", 5, {"tags": ["livelihood"]}, {"type": "full"}),
        ]
        result = evaluate_charge(self.lines, self.vehicle, False, self.at, policies)
        self.assertEqual(0, result["final_total_fen"])
        kinds = [(step["kind"], step["policy_id"], step["applied"]) for step in result["steps"]]
        self.assertEqual(kinds, [
            ("eligibility", "qualify", True),
            ("exemption", "exempt", True),
            ("rate", "discount", False),
        ])

    def test_rate_discount_uses_integer_fen(self):
        policies = [make_policy("r", "rate", 1, {}, {"type": "discount_pct", "percent": 15})]
        result = evaluate_charge(self.lines, self.vehicle, False, self.at, policies)
        # 10000*0.85=8500；20000*0.85=17000
        self.assertEqual(25500, result["final_total_fen"])
        self.assertEqual([8500, 17000], [s["final_fen"] for s in result["segments"]])

    def test_cap_allocates_reduction_proportionally_with_conservation(self):
        policies = [make_policy("cap", "cap", 1, {}, {"type": "amount_cap", "amount_fen": 27000})]
        result = evaluate_charge(self.lines, self.vehicle, False, self.at, policies)
        self.assertEqual(27000, result["final_total_fen"])
        self.assertEqual([9000, 18000], [s["final_fen"] for s in result["segments"]])

    def test_cap_remainder_method_keeps_sum_exact(self):
        lines = [ChargeLine("a", "o1", 1), ChargeLine("b", "o2", 1), ChargeLine("c", "o3", 1)]
        policies = [make_policy("cap", "cap", 1, {}, {"type": "amount_cap", "amount_fen": 2})]
        result = evaluate_charge(lines, self.vehicle, False, self.at, policies)
        self.assertEqual(2, result["final_total_fen"])
        self.assertEqual(2, sum(s["final_fen"] for s in result["segments"]))

    def test_segment_scoped_policy(self):
        policies = [make_policy("half-a", "rate", 1, {"segment_ids": ["seg-a"]},
                                {"type": "discount_pct", "percent": 50})]
        result = evaluate_charge(self.lines, self.vehicle, False, self.at, policies)
        self.assertEqual([5000, 20000], [s["final_fen"] for s in result["segments"]])

    def test_holiday_and_class_must_match(self):
        policies = [
            make_policy("h", "rate", 1, {"is_holiday": True},
                        {"type": "discount_pct", "percent": 50}),
            make_policy("c", "rate", 2, {"vehicle_classes": ["bus"]},
                        {"type": "discount_pct", "percent": 50}),
        ]
        result = evaluate_charge(self.lines, self.vehicle, False, self.at, policies)
        self.assertEqual(30000, result["final_total_fen"])
        self.assertFalse(any(step["applied"] for step in result["steps"]))

    def test_withdrawn_or_future_policy_excluded_by_in_force(self):
        past = datetime(2025, 1, 1, tzinfo=timezone.utc)
        future = datetime(2027, 1, 1, tzinfo=timezone.utc)
        withdrawn = Policy("w", 1, "rate", 1, "w", {},
                           {"type": "discount_pct", "percent": 50}, past, None,
                           published_at=past, withdrawn_at=future)
        self.assertFalse(withdrawn.in_force(future))
        upcoming = Policy("u", 1, "rate", 1, "u", {},
                          {"type": "discount_pct", "percent": 50}, future, None, published_at=past)
        self.assertFalse(upcoming.in_force(self.at))


class BillingServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.service = BillingService(self.database, self.clock)
        self.service.register_organization(request_id="org1", actor_id="bootstrap",
                                           organization_id="o1", name="一公司")
        self.service.register_organization(request_id="org2", actor_id="bootstrap",
                                           organization_id="o2", name="二公司")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_segment(request_id="seg1", actor_id="a1", segment_id="seg-a",
                                      organization_id="o1", name="A段", base_amount_fen=10000)
        self.service.register_segment(request_id="seg2", actor_id="a1", segment_id="seg-b",
                                      organization_id="o2", name="B段", base_amount_fen=20000)
        self.service.register_segment(request_id="seg3", actor_id="a1", segment_id="seg-c",
                                      organization_id="o2", name="C绕行段", base_amount_fen=5000)
        self.service.publish_policy(
            request_id="cap-policy", actor_id="a1", policy_id="cap", kind="cap", priority=50,
            name="全程封顶", conditions={}, effect={"type": "amount_cap", "amount_fen": 27000},
            effective_from="2026-01-01T00:00:00+08:00")

    def tearDown(self):
        self.database.close()

    def _advance(self, **kwargs):
        from datetime import timedelta
        self.service.clock = FixedClock(self.service.clock.now() + timedelta(**kwargs))

    def _open_trip(self, trip_id="trip-1", **overrides):
        data = {"vehicle_id": "v1", "vehicle_class": "car", "entry_plaza": "P01",
                "entry_segment_id": "seg-a"}
        data.update(overrides)
        return self.service.record_trip_event(
            request_id=f"entry-{trip_id}", actor_id="op1", trip_id=trip_id, kind="entry",
            event_occurred_at="2026-10-02T08:00:00+08:00", evidence_ref="ENTRY-1", data=data)

    def _exit_trip(self, trip_id="trip-1", path=("seg-a", "seg-b"), request_id=None,
                   kind="exit", at="2026-10-02T09:00:00+08:00", evidence_ref="EXIT-1"):
        return self.service.record_trip_event(
            request_id=request_id or f"exit-{trip_id}", actor_id="op1", trip_id=trip_id, kind=kind,
            event_occurred_at=at, evidence_ref=evidence_ref, data={"path": list(path)})

    def test_operator_cannot_publish_policy(self):
        with self.assertRaises(PermissionDenied):
            self.service.publish_policy(
                request_id="x", actor_id="op1", policy_id="p", kind="rate", priority=1, name="x",
                conditions={}, effect={"type": "discount_pct", "percent": 10},
                effective_from="2026-01-01T00:00:00+08:00")

    def test_policy_validation(self):
        with self.assertRaises(ValidationError):
            self.service.publish_policy(
                request_id="x", actor_id="a1", policy_id="p", kind="rate", priority=1, name="x",
                conditions={}, effect={"type": "discount_pct", "percent": 0},
                effective_from="2026-01-01T00:00:00+08:00")
        with self.assertRaises(ValidationError):
            self.service.publish_policy(
                request_id="y", actor_id="a1", policy_id="p2", kind="cap", priority=1, name="x",
                conditions={}, effect={"type": "amount_cap", "amount_fen": -1},
                effective_from="2026-01-01T00:00:00+08:00")

    def test_missing_exit_waits_for_evidence(self):
        self._open_trip()
        explained = self.service.explain_trip("trip-1")
        self.assertEqual("pending_evidence", explained["state"])
        self.assertIsNone(explained["current_amount_fen"])
        receipt = self._exit_trip(kind="supplemental_exit", request_id="supp-1",
                                  at="2026-10-02T09:30:00+08:00")
        self.assertTrue(receipt.response["billed"])
        self.assertEqual(27000, self.service.explain_trip("trip-1")["current_amount_fen"])

    def test_entry_without_segment_and_exit_without_path_stays_pending(self):
        self.service.record_trip_event(
            request_id="entry-t2", actor_id="op1", trip_id="trip-2", kind="entry",
            event_occurred_at="2026-10-02T08:00:00+08:00", evidence_ref="E",
            data={"vehicle_id": "v2", "vehicle_class": "car", "entry_plaza": "P"})
        receipt = self.service.record_trip_event(
            request_id="exit-t2", actor_id="op1", trip_id="trip-2", kind="exit",
            event_occurred_at="2026-10-02T09:00:00+08:00", evidence_ref="X")
        self.assertEqual("pending_evidence", receipt.response["state"])

    def test_detour_after_billing_creates_version_with_reversal_and_conserves(self):
        self._open_trip()
        self._advance(hours=2)
        self._exit_trip()
        self._advance(minutes=30)
        self.service.record_trip_event(
            request_id="detour-1", actor_id="op1", trip_id="trip-1", kind="detour",
            event_occurred_at="2026-10-02T09:30:00+08:00", evidence_ref="DETOUR-1",
            data={"path": ["seg-a", "seg-c"]})
        explained = self.service.explain_trip("trip-1")
        self.assertEqual(2, explained["current_fact_version"])
        self.assertEqual(15000, explained["current_amount_fen"])
        self.assertEqual([1, 2], [item["version"] for item in explained["fact_versions"]])
        # 旧分录保留 + 冲回分录 + 新分录，净额等于新版本金额
        rows = self.database.connection.execute(
            "SELECT kind, COUNT(*) AS count, SUM(amount_fen) AS total FROM ledger_entries "
            "WHERE trip_id='trip-1' GROUP BY kind").fetchall()
        grouped = {row["kind"]: (row["count"], row["total"]) for row in rows}
        self.assertEqual((4, 42000), grouped["charge"])
        self.assertEqual((2, -27000), grouped["charge_reversal"])
        self.assertEqual(15000, sum(row["total"] for row in rows))

    def test_refund_allocates_by_net_and_keeps_conservation(self):
        self._open_trip()
        self._advance(hours=2)
        self._exit_trip(path=("seg-a", "seg-c"))
        receipt = self.service.register_adjustment(
            request_id="refund-1", actor_id="a1", trip_id="trip-1", kind="refund",
            amount_fen=3000, reason="设备故障")
        shares = receipt.response["operator_shares_fen"]
        self.assertEqual(3000, sum(shares.values()))
        self.assertEqual(2, len(shares))  # 10000/5000 两个运营方都分摊
        explained = self.service.explain_trip("trip-1")
        self.assertEqual(12000, sum(v["net_fen"] for v in explained["operator_shares_fen"].values()))

    def test_refund_cannot_exceed_collectible_net(self):
        self._open_trip()
        self._advance(hours=2)
        self._exit_trip()
        with self.assertRaises(ConflictError):
            self.service.register_adjustment(
                request_id="refund-x", actor_id="a1", trip_id="trip-1", kind="refund",
                amount_fen=27001, reason="超额")

    def test_recovery_allocates_by_net_weights(self):
        self._open_trip()
        self._advance(hours=2)
        self._exit_trip()
        receipt = self.service.register_adjustment(
            request_id="rec-1", actor_id="a1", trip_id="trip-1", kind="recovery",
            amount_fen=900, reason="追缴")
        # 净额 9000:18000 = 1:2
        self.assertEqual({"o1": 300, "o2": 600}, receipt.response["operator_shares_fen"])

    def test_settled_trip_rejects_late_events_and_adjustments(self):
        self._open_trip()
        self._advance(hours=2)
        self._exit_trip()
        self.service.create_period(request_id="period-1", actor_id="a1",
                                   period_id="per-10", name="10月")
        self.service.settle_trip(request_id="settle-1", actor_id="a1",
                                 trip_id="trip-1", period_id="per-10")
        receipt = self._exit_trip(kind="supplemental_exit", request_id="late-exit",
                                  at="2026-10-05T08:00:00+08:00")
        self.assertFalse(receipt.response["amount_changed"])
        explained = self.service.explain_trip("trip-1")
        self.assertTrue(explained["has_late_event"])
        self.assertEqual(27000, explained["current_amount_fen"])
        with self.assertRaises(AccountClosedError):
            self.service.register_adjustment(
                request_id="refund-late", actor_id="a1", trip_id="trip-1", kind="refund",
                amount_fen=100, reason="迟到退款")

    def test_dispute_freezes_only_related_entries(self):
        self._open_trip("trip-1")
        self._open_trip("trip-2")
        self._advance(hours=2)
        self._exit_trip("trip-1")
        self._exit_trip("trip-2")
        self.service.open_dispute(request_id="dispute-1", actor_id="a1",
                                  trip_id="trip-1", reason="车主投诉")
        self.service.create_period(request_id="period-x", actor_id="a1",
                                   period_id="per-x", name="冻结期")
        frozen = self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries WHERE frozen=1").fetchone()["count"]
        self.assertEqual(2, frozen)  # 只有 trip-1 的两条收费分录
        with self.assertRaises(ConflictError):
            self.service.settle_trip(request_id="s1", actor_id="a1",
                                     trip_id="trip-1", period_id="per-x")
        # trip-2 完全不受影响，可以正常调整
        self.service.register_adjustment(
            request_id="r2", actor_id="a1", trip_id="trip-2", kind="recovery",
            amount_fen=300, reason="正常追缴")
        # 争议期间迟到的绕行事件不重计费
        self.service.record_trip_event(
            request_id="detour-frozen", actor_id="op1", trip_id="trip-1", kind="detour",
            event_occurred_at="2026-10-02T10:00:00+08:00", evidence_ref="D",
            data={"path": ["seg-a", "seg-c"]})
        self.assertEqual(1, self.service.explain_trip("trip-1")["current_fact_version"])
        dispute_id = self.database.connection.execute(
            "SELECT dispute_id FROM disputes WHERE trip_id='trip-1'").fetchone()[0]
        self.service.resolve_dispute(request_id="resolve-1", actor_id="a1",
                                     dispute_id=dispute_id, resolution="维持原价")
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries WHERE frozen=1").fetchone()["count"])

    def test_policy_withdrawal_only_affects_unsettled_trips(self):
        # trip-1 在撤回首出账
        self._open_trip("trip-1")
        self._advance(hours=2)
        self._exit_trip("trip-1")
        self.assertEqual(27000, self.service.explain_trip("trip-1")["current_amount_fen"])
        self._advance(days=2)
        self.service.withdraw_policy(request_id="withdraw-cap", actor_id="a1", policy_id="cap")
        # 撤回后新行程不再封顶
        self._open_trip("trip-2")
        self._advance(hours=2)
        self._exit_trip("trip-2")
        self.assertEqual(30000, self.service.explain_trip("trip-2")["current_amount_fen"])
        # trip-1 已固化的事实不受撤回影响
        self.assertEqual(27000, self.service.explain_trip("trip-1")["current_amount_fen"])

    def test_replay_at_historical_point(self):
        self._open_trip("trip-1")
        self._advance(hours=2)
        self._exit_trip("trip-1")
        snapshot = self.service.clock.now().isoformat()
        self._advance(minutes=30)
        self.service.record_trip_event(
            request_id="detour-1", actor_id="op1", trip_id="trip-1", kind="detour",
            event_occurred_at="2026-10-02T09:30:00+08:00", evidence_ref="D",
            data={"path": ["seg-a", "seg-c"]})
        replay = self.service.replay_trip("trip-1", snapshot)
        self.assertEqual([1], replay["recorded_fact_versions"])
        self.assertEqual(27000, replay["recomputed"]["final_total_fen"])
        # 当前值已经是绕行后的 15000
        self.assertEqual(15000, self.service.explain_trip("trip-1")["current_amount_fen"])

    def test_replay_before_exit_shows_pending(self):
        self._open_trip("trip-1")
        replay = self.service.replay_trip("trip-1", self.service.clock.now().isoformat())
        self.assertIsNone(replay["recomputed"])
        self.assertEqual("pending_evidence", replay["state_at_time"])

    def test_reconciliation_totals_balance(self):
        self._open_trip("trip-1")
        self._advance(hours=2)
        self._exit_trip("trip-1", path=("seg-a", "seg-c"))
        self.service.register_adjustment(
            request_id="refund-1", actor_id="a1", trip_id="trip-1", kind="refund",
            amount_fen=3000, reason="补偿")
        self.service.create_period(request_id="period-1", actor_id="a1",
                                   period_id="per-10", name="10月")
        self.service.settle_trip(request_id="settle-1", actor_id="a1",
                                 trip_id="trip-1", period_id="per-10")
        report = self.service.operator_reconciliation("per-10")
        self.assertEqual(12000, report["totals"]["net_fen"])
        self.assertEqual(-3000, report["totals"]["refund_fen"])

    def test_close_period_blocks_new_settlement(self):
        self._open_trip()
        self._advance(hours=2)
        self._exit_trip()
        self.service.create_period(request_id="period-1", actor_id="a1",
                                   period_id="per-10", name="10月")
        self.service.close_period(request_id="close-1", actor_id="a1", period_id="per-10")
        with self.assertRaises(AccountClosedError):
            self.service.settle_trip(request_id="s1", actor_id="a1",
                                     trip_id="trip-1", period_id="per-10")

    def test_idempotent_event_replay_does_not_duplicate_billing(self):
        self._open_trip()
        self._advance(hours=2)
        first = self._exit_trip(request_id="exit-once")
        second = self._exit_trip(request_id="exit-once")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(1, self.service.explain_trip("trip-1")["current_fact_version"])
        entries = self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries WHERE kind='charge'").fetchone()["count"]
        self.assertEqual(2, entries)

    def test_cannot_adjust_pending_trip(self):
        self._open_trip()
        with self.assertRaises(BillingStateError):
            self.service.register_adjustment(
                request_id="x", actor_id="a1", trip_id="trip-1", kind="refund",
                amount_fen=1, reason="无事实")

    def test_unknown_segment_in_path_rejected(self):
        self._open_trip()
        with self.assertRaises(NotFoundError):
            self._exit_trip(path=("seg-a", "seg-zzz"))

    def test_duplicate_entry_rejected(self):
        self._open_trip()
        with self.assertRaises(ConflictError):
            self.service.record_trip_event(
                request_id="entry-duplicate", actor_id="op1", trip_id="trip-1", kind="entry",
                event_occurred_at="2026-10-02T08:05:00+08:00", evidence_ref="ENTRY-2",
                data={"vehicle_id": "v1", "vehicle_class": "car", "entry_plaza": "P01",
                      "entry_segment_id": "seg-a"})

    def test_duplicate_plain_exit_does_not_rebill_but_supplemental_does(self):
        self._open_trip()
        self._advance(hours=2)
        self._exit_trip(request_id="exit-once")
        # 普通出口再次上报：不产生新版本
        receipt = self._exit_trip(request_id="exit-again")
        self.assertFalse(receipt.response["amount_changed"])
        self.assertEqual(1, self.service.explain_trip("trip-1")["current_fact_version"])
        # 显式出口补录携带更正路径：产生新版本
        receipt = self._exit_trip(request_id="supp-1", kind="supplemental_exit",
                                  at="2026-10-02T09:40:00+08:00",
                                  path=("seg-a", "seg-c"))
        self.assertTrue(receipt.response["amount_changed"])
        self.assertEqual(2, self.service.explain_trip("trip-1")["current_fact_version"])
        self.assertEqual(15000, self.service.explain_trip("trip-1")["current_amount_fen"])

    def test_night_freight_discount_uses_local_wall_clock(self):
        self.service.publish_policy(
            request_id="night", actor_id="a1", policy_id="night80", kind="rate", priority=20,
            name="夜间货运八折", conditions={"vehicle_classes": ["freight"], "night": True},
            effect={"type": "discount_pct", "percent": 20},
            effective_from="2026-01-01T00:00:00+08:00")
        self._open_trip(vehicle_class="freight")
        self._advance(hours=2)
        # 北京时间 23:10（UTC 15:10）出口，应按本地墙钟识别为夜间
        self._exit_trip(at="2026-10-02T23:10:00+08:00")
        self.assertEqual(24000, self.service.explain_trip("trip-1")["current_amount_fen"])


if __name__ == "__main__":
    unittest.main()
