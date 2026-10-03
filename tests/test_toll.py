import unittest
from datetime import datetime, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from transport_coordination.storage import Database
from transport_coordination.toll_engine import price_trip
from transport_coordination.toll_service import TollService


def make_service():
    database = Database()
    service = TollService(database, FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc)))
    service.register_organization(request_id="o1", actor_id="bootstrap", organization_id="o1", name="东段")
    service.register_organization(request_id="o2", actor_id="bootstrap", organization_id="o2", name="西段")
    service.register_organization(request_id="gov", actor_id="bootstrap", organization_id="gov", name="政策中心")
    service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                           display_name="管理员", role="admin", organization_id="gov")
    service.register_actor(request_id="rv", actor_id="admin", new_actor_id="rv",
                           display_name="政策员", role="reviewer", organization_id="gov")
    service.register_actor(request_id="op", actor_id="admin", new_actor_id="op",
                           display_name="收费员", role="operator", organization_id="o1")
    service.register_segment(request_id="sa", actor_id="op", segment_id="seg-a",
                             organization_id="o1", name="东段", rate_fen_per_km=50, length_m=100_000)
    service.register_segment(request_id="sb", actor_id="admin", segment_id="seg-b",
                             organization_id="o2", name="西段", rate_fen_per_km=60, length_m=60_000)
    return database, service


class EngineTest(unittest.TestCase):
    def segments(self):
        return [
            {"segment_id": "a", "version": 1, "organization_id": "o1",
             "rate_fen_per_km": 50, "length_m": 100_000},
            {"segment_id": "b", "version": 1, "organization_id": "o2",
             "rate_fen_per_km": 60, "length_m": 60_000},
        ]

    def test_priority_order_discount_then_cap(self):
        policies = [
            {"policy_id": "d1", "version": 1, "kind": "discount", "priority": 10,
             "scope": {}, "params": {"mode": "percent", "permille": 900}, "status": "active",
             "effective_at": "2026-01-01T00:00:00Z", "expires_at": None},
            {"policy_id": "cap", "version": 1, "kind": "cap", "priority": 100,
             "scope": {}, "params": {"max_fen": 7000}, "status": "active",
             "effective_at": "2026-01-01T00:00:00Z", "expires_at": None},
        ]
        result = price_trip(segments=self.segments(), policies=policies,
                            vehicle={"vehicle_class": "passenger", "tags": []},
                            basis_time="2026-10-01T12:00:00Z")
        self.assertEqual(8600, result["total_gross_fen"])
        self.assertEqual(7000, result["total_fen"])
        # 折后 4500/3240，封顶减免 740 按比例分摊为 430/310
        relief = [s["trip_cap_relief_fen"] for s in result["per_segment"]]
        self.assertEqual([430, 310], relief)
        self.assertEqual(4070, result["per_segment"][0]["final_fen"])
        self.assertEqual(2930, result["per_segment"][1]["final_fen"])

    def test_exemption_beats_discount(self):
        policies = [
            {"policy_id": "d1", "version": 1, "kind": "discount", "priority": 1,
             "scope": {}, "params": {"mode": "percent", "permille": 500}, "status": "active",
             "effective_at": "2026-01-01T00:00:00Z", "expires_at": None},
            {"policy_id": "ex", "version": 1, "kind": "exemption", "priority": 9,
             "scope": {"tags": ["green"]}, "params": {}, "status": "active",
             "effective_at": "2026-01-01T00:00:00Z", "expires_at": None},
        ]
        result = price_trip(segments=[self.segments()[0]], policies=policies,
                            vehicle={"vehicle_class": "truck", "tags": ["green"]},
                            basis_time="2026-10-01T12:00:00Z")
        self.assertEqual(0, result["total_fen"])
        self.assertEqual(5000, result["per_segment"][0]["discount_fen"])

    def test_night_hours_window(self):
        policy = {"policy_id": "n", "version": 1, "kind": "discount", "priority": 1,
                  "scope": {"hours": [22, 6]}, "params": {"mode": "percent", "permille": 800},
                  "status": "active", "effective_at": "2026-01-01T00:00:00Z", "expires_at": None}
        day = price_trip(segments=[self.segments()[0]], policies=[policy],
                         vehicle={"vehicle_class": "truck", "tags": []},
                         basis_time="2026-10-01T12:00:00Z")
        night = price_trip(segments=[self.segments()[0]], policies=[policy],
                           vehicle={"vehicle_class": "truck", "tags": []},
                           basis_time="2026-10-01T23:00:00Z")
        self.assertEqual(5000, day["total_fen"])
        self.assertEqual(4000, night["total_fen"])


class TollServiceTest(unittest.TestCase):
    def setUp(self):
        self.database, self.service = make_service()

    def tearDown(self):
        self.database.close()

    def publish_holiday(self):
        self.service.publish_policy(
            request_id="ph", actor_id="rv", policy_id="holiday", kind="discount", priority=100,
            scope={}, params={"mode": "percent", "permille": 900},
            effective_at="2026-10-01T00:00:00Z", expires_at="2026-10-08T00:00:00Z",
            published_org="gov")
        self.service.publish_policy(
            request_id="pc", actor_id="rv", policy_id="cap", kind="cap", priority=1000,
            scope={}, params={"max_fen": 7000},
            effective_at="2026-01-01T00:00:00Z", published_org="gov")

    def open_exit_settle(self, trip_id="t1", request_prefix="t1", vehicle_class="passenger",
                         tags=None):
        self.service.open_trip(request_id=f"{request_prefix}-open", actor_id="op", trip_id=trip_id,
                               vehicle_plate="京A1", vehicle_class=vehicle_class,
                               segment_ids=["seg-a", "seg-b"],
                               entry_time="2026-10-01T22:00:00Z", tags=tags or [])
        exit_result = self.service.record_exit(
            request_id=f"{request_prefix}-exit", actor_id="op", trip_id=trip_id,
            event_time="2026-10-01T23:30:00Z")
        settle = self.service.settle_trip(request_id=f"{request_prefix}-settle", actor_id="op",
                                          trip_id=trip_id)
        return exit_result, settle

    def test_pending_evidence_then_exit(self):
        self.publish_holiday()
        self.service.open_trip(request_id="open", actor_id="op", trip_id="t1",
                               vehicle_plate="京A1", vehicle_class="passenger",
                               segment_ids=["seg-a"], entry_time="2026-10-01T10:00:00Z")
        self.service.mark_pending_evidence(request_id="pend", actor_id="op", trip_id="t1")
        self.assertEqual("pending_evidence", self.service.get_trip("t1").status)
        result = self.service.record_exit(request_id="exit", actor_id="op", trip_id="t1",
                                          event_time="2026-10-01T12:00:00Z")
        self.assertEqual(4500, result["total_fen"])

    def test_billing_fact_is_deduped_on_same_inputs(self):
        self.publish_holiday()
        self.service.open_trip(request_id="open", actor_id="op", trip_id="t1",
                               vehicle_plate="京A1", vehicle_class="passenger",
                               segment_ids=["seg-a"], entry_time="2026-10-01T10:00:00Z")
        first = self.service.record_exit(request_id="exit1", actor_id="op", trip_id="t1",
                                         event_id="e1", event_time="2026-10-01T12:00:00Z")
        # 补录一个内容相同的出口事件（不同 event_id），输入版本不变，应复用事实
        second = self.service.record_exit(request_id="exit2", actor_id="op", trip_id="t1",
                                          event_id="e2", event_time="2026-10-01T12:00:00Z",
                                          supersedes_event_id="e1")
        self.assertEqual(first["fact_id"], second["fact_id"])

    def test_late_exit_cannot_rewrite_settled_income(self):
        self.publish_holiday()
        exit_result, _ = self.open_exit_settle()
        with self.assertRaises(ConflictError):
            self.service.record_exit(request_id="late", actor_id="op", trip_id="t1",
                                     event_time="2026-10-02T01:00:00Z",
                                     supersedes_event_id=exit_result["exit_event_id"])

    def test_same_request_replays_after_settlement(self):
        # 关账后用同一 request_id 重试出口请求，必须返回首次结果而不是被状态拦截
        self.publish_holiday()
        self.service.open_trip(request_id="open", actor_id="op", trip_id="t1",
                               vehicle_plate="京A1", vehicle_class="passenger",
                               segment_ids=["seg-a"], entry_time="2026-10-01T10:00:00Z")
        first = self.service.record_exit(request_id="exit-req", actor_id="op", trip_id="t1",
                                         event_time="2026-10-01T12:00:00Z")
        self.service.settle_trip(request_id="settle-req", actor_id="op", trip_id="t1")
        replay = self.service.record_exit(request_id="exit-req", actor_id="op", trip_id="t1",
                                          event_time="2026-10-01T12:00:00Z")
        self.assertEqual(first["fact_id"], replay["fact_id"])

    def test_settlement_entries_balance_and_attribute_discount(self):
        self.publish_holiday()
        _, settle = self.open_exit_settle()
        self.assertEqual(7000, settle["total_fen"])
        entries = self.service.trip_ledger("t1")
        debit = sum(e.amount_fen for e in entries if e.direction == "debit")
        credit = sum(e.amount_fen for e in entries if e.direction == "credit")
        self.assertEqual(debit, credit)
        self.assertEqual(8600, debit)
        grants = [e for e in entries if e.account == "discount_grant"]
        self.assertEqual(sum(g.amount_fen for g in grants), 1600)
        self.assertTrue(all(g.organization_id == "gov" for g in grants))

    def test_refund_allocates_by_revenue_and_balances(self):
        self.publish_holiday()
        self.open_exit_settle()
        refund = self.service.create_refund(request_id="r1", actor_id="op", trip_id="t1",
                                            amount_fen=1000, reason="多扣费")
        amounts = {a["organization_id"]: a["amount_fen"] for a in refund["allocations"]}
        self.assertEqual(1000, sum(amounts.values()))
        entries = self.service.trip_ledger("t1")
        voucher = [e for e in entries if e.voucher_id == refund["voucher_id"]]
        debit = sum(e.amount_fen for e in voucher if e.direction == "debit")
        credit = sum(e.amount_fen for e in voucher if e.direction == "credit")
        self.assertEqual(debit, credit)

    def test_refund_cannot_exceed_unfrozen_revenue_under_dispute(self):
        self.publish_holiday()
        self.open_exit_settle()
        self.service.open_dispute(request_id="disp-open", actor_id="rv", dispute_id="d1", trip_id="t1",
                                  reason="异议")
        with self.assertRaises(ConflictError):
            self.service.create_refund(request_id="r2", actor_id="op", trip_id="t1",
                                       amount_fen=1, reason="试图退未冻结收入")
        # 与争议关联的退款可以使用冻结池
        refund = self.service.create_refund(request_id="r3", actor_id="op", trip_id="t1",
                                            amount_fen=500, reason="争议退款", dispute_id="d1")
        self.assertEqual(500, refund["amount_fen"])

    def test_dispute_only_freezes_its_trip_entries(self):
        self.publish_holiday()
        self.open_exit_settle(trip_id="t1", request_prefix="t1")
        self.open_exit_settle(trip_id="t2", request_prefix="t2")
        self.service.open_dispute(request_id="disp-open", actor_id="rv", dispute_id="d1", trip_id="t1",
                                  reason="异议")
        self.assertTrue(all(e.frozen for e in self.service.trip_ledger("t1")))
        self.assertTrue(all(not e.frozen for e in self.service.trip_ledger("t2")))

    def test_policy_withdraw_only_affects_unsettled_trips(self):
        self.publish_holiday()
        self.open_exit_settle(trip_id="t1", request_prefix="t1")  # 已关账
        self.service.open_trip(request_id="t2-open", actor_id="op", trip_id="t2", vehicle_plate="京B2",
                               vehicle_class="passenger", segment_ids=["seg-a"],
                               entry_time="2026-10-03T10:00:00Z")
        billed = self.service.record_exit(request_id="e2", actor_id="op", trip_id="t2",
                                          event_time="2026-10-03T11:00:00Z")
        self.assertEqual(4500, billed["total_fen"])
        self.service.withdraw_policy(request_id="withdraw", actor_id="rv", policy_id="holiday")
        self.assertEqual(7000, self.service.explain_trip("t1")["billing"]["total_fen"])
        self.assertEqual(5000, self.service.explain_trip("t2")["billing"]["total_fen"])

    def test_segment_update_versions_do_not_change_open_trip_lock(self):
        self.publish_holiday()
        self.service.open_trip(request_id="open", actor_id="op", trip_id="t1",
                               vehicle_plate="京A1", vehicle_class="passenger",
                               segment_ids=["seg-a"], entry_time="2026-10-01T10:00:00Z")
        self.service.update_segment(request_id="upd", actor_id="op", segment_id="seg-a",
                                    rate_fen_per_km=99)
        result = self.service.record_exit(request_id="exit", actor_id="op", trip_id="t1",
                                          event_time="2026-10-01T12:00:00Z")
        # 行程锁定 v1 费率，九折后仍是 4500；新行程才用 0.99 元/km
        self.assertEqual(4500, result["total_fen"])

    def test_detour_changes_path_before_settlement(self):
        self.publish_holiday()
        self.service.open_trip(request_id="open", actor_id="op", trip_id="t1",
                               vehicle_plate="京A1", vehicle_class="passenger",
                               segment_ids=["seg-a"], entry_time="2026-10-01T10:00:00Z")
        self.service.record_exit(request_id="exit", actor_id="op", trip_id="t1",
                                 event_time="2026-10-01T12:00:00Z")
        self.service.record_detour(request_id="det", actor_id="op", trip_id="t1",
                                   event_time="2026-10-01T11:30:00Z",
                                   add_segment_ids=["seg-b"], reason="封路绕行")
        self.assertEqual(7000, self.service.explain_trip("t1")["billing"]["total_fen"])

    def test_operator_cannot_publish_policy(self):
        with self.assertRaises(PermissionDenied):
            self.service.publish_policy(
                request_id="bad-policy", actor_id="op", policy_id="p", kind="discount", priority=1,
                scope={}, params={"mode": "percent", "permille": 900},
                effective_at="2026-10-01T00:00:00Z")

    def test_invalid_policy_params_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.publish_policy(
                request_id="bad-policy", actor_id="rv", policy_id="p", kind="discount", priority=1,
                scope={}, params={"mode": "percent", "permille": 5000},
                effective_at="2026-10-01T00:00:00Z")

    def test_replay_pricing_uses_historical_policy_set(self):
        self.publish_holiday()
        self.service.open_trip(request_id="open", actor_id="op", trip_id="t1",
                               vehicle_plate="京A1", vehicle_class="passenger",
                               segment_ids=["seg-a", "seg-b"],
                               entry_time="2026-10-01T10:00:00Z")
        early = self.service.replay_pricing("t1", "2025-01-01T00:00:00Z")
        self.assertEqual(8600, early["total_fen"])
        holiday = self.service.replay_pricing("t1", "2026-10-02T00:00:00Z")
        self.assertEqual(7000, holiday["total_fen"])

    def test_unknown_trip_explains_404(self):
        with self.assertRaises(NotFoundError):
            self.service.explain_trip("missing")


if __name__ == "__main__":
    unittest.main()
