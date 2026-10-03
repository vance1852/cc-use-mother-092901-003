"""收费清算接口的 HTTP 路由测试。"""

import unittest

from transport_coordination.api import route
from transport_coordination.billing import BillingService
from transport_coordination.storage import Database


class BillingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = BillingService(self.database)
        self._call("POST", "/organizations", {"request_id": "org1", "organization_id": "o1", "name": "一公司"},
                   actor="bootstrap")
        self._call("POST", "/organizations", {"request_id": "org2", "organization_id": "o2", "name": "二公司"},
                   actor="bootstrap")
        self._call("POST", "/actors", {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
                                        "role": "admin", "organization_id": "o1"}, actor="bootstrap")
        self._call("POST", "/segments", {"request_id": "s1", "segment_id": "seg-a", "organization_id": "o1",
                                          "name": "A段", "base_amount_fen": 10000}, actor="a1")
        self._call("POST", "/segments", {"request_id": "s2", "segment_id": "seg-b", "organization_id": "o2",
                                          "name": "B段", "base_amount_fen": 20000}, actor="a1")

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="a1", query=None):
        target = path + ("?" + query if query else "")
        return route(self.service, method, target, body or {}, {"X-Actor-Id": actor})

    def _full_trip(self, trip_id="trip-1"):
        status, _ = self._call("POST", "/trip-events", {
            "request_id": f"entry-{trip_id}", "trip_id": trip_id, "kind": "entry",
            "event_occurred_at": "2026-10-02T08:00:00+08:00", "evidence_ref": "E1",
            "data": {"vehicle_id": "v1", "vehicle_class": "car", "entry_plaza": "P01",
                     "entry_segment_id": "seg-a"}})
        self.assertIn(status, (200, 201))
        status, body = self._call("POST", "/trip-events", {
            "request_id": f"exit-{trip_id}", "trip_id": trip_id, "kind": "exit",
            "event_occurred_at": "2026-10-02T09:00:00+08:00", "evidence_ref": "X1",
            "data": {"path": ["seg-a", "seg-b"]}})
        self.assertIn(status, (200, 201))
        return body

    def test_publish_policy_and_bill_via_api(self):
        status, body = self._call("POST", "/policies", {
            "request_id": "cap1", "policy_id": "cap", "kind": "cap", "priority": 50,
            "name": "全程封顶", "conditions": {},
            "effect": {"type": "amount_cap", "amount_fen": 27000},
            "effective_from": "2026-01-01T00:00:00+08:00"})
        self.assertEqual(201, status)
        self.assertEqual(1, body["response"]["version"])
        billed = self._full_trip()
        self.assertTrue(billed["response"]["billed"])
        status, explanation = self._call("GET", "/trips/trip-1/explain")
        self.assertEqual(200, status)
        self.assertEqual(27000, explanation["current_amount_fen"])
        self.assertEqual({"o1": 9000, "o2": 18000},
                         {key: value["net_fen"] for key, value in explanation["operator_shares_fen"].items()})

    def test_explain_unknown_trip_returns_404(self):
        status, body = self._call("GET", "/trips/nope/explain")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])

    def test_replay_requires_as_of(self):
        status, body = self._call("GET", "/trips/trip-1/replay")
        self.assertEqual(400, status)

    def test_refund_and_settlement_flow(self):
        self._call("POST", "/policies", {
            "request_id": "cap1", "policy_id": "cap", "kind": "cap", "priority": 50,
            "name": "封顶", "conditions": {},
            "effect": {"type": "amount_cap", "amount_fen": 27000},
            "effective_from": "2026-01-01T00:00:00+08:00"})
        self._full_trip()
        status, body = self._call("POST", "/adjustments", {
            "request_id": "rf1", "trip_id": "trip-1", "kind": "refund",
            "amount_fen": 3000, "reason": "补偿"})
        self.assertEqual(201, status)
        self.assertEqual(3000, sum(body["response"]["operator_shares_fen"].values()))
        self._call("POST", "/settlement-periods", {"request_id": "p1", "period_id": "per-10", "name": "10月"})
        status, body = self._call("POST", "/settlements",
                                  {"request_id": "set-1", "trip_id": "trip-1", "period_id": "per-10"})
        self.assertEqual(201, status)
        self.assertEqual(24000, body["response"]["total_fen"])
        # 关账后迟到事件不改金额
        status, body = self._call("POST", "/trip-events", {
            "request_id": "late1", "trip_id": "trip-1", "kind": "late_notice",
            "event_occurred_at": "2026-10-05T08:00:00+08:00", "evidence_ref": "L1", "data": {}})
        self.assertEqual(201, status)
        self.assertFalse(body["response"]["amount_changed"])
        status, report = self._call("GET", "/reconciliation", query="period_id=per-10")
        self.assertEqual(200, status)
        self.assertEqual(24000, report["totals"]["net_fen"])

    def test_dispute_freeze_blocks_settlement(self):
        self._full_trip()
        self._call("POST", "/settlement-periods", {"request_id": "p1", "period_id": "per-10", "name": "10月"})
        status, body = self._call("POST", "/disputes",
                                  {"request_id": "d1", "trip_id": "trip-1", "reason": "投诉"})
        self.assertEqual(201, status)
        status, body = self._call("POST", "/settlements",
                                  {"request_id": "set-1", "trip_id": "trip-1", "period_id": "per-10"})
        self.assertEqual(409, status)
        dispute_id = self.database.connection.execute(
            "SELECT dispute_id FROM disputes WHERE trip_id='trip-1'").fetchone()[0]
        status, _ = self._call("POST", "/disputes/resolve",
                               {"request_id": "r1", "dispute_id": dispute_id, "resolution": "维持"})
        self.assertEqual(200, status)

    def test_policy_listing_excludes_withdrawn(self):
        self._call("POST", "/policies", {
            "request_id": "cap1", "policy_id": "cap", "kind": "cap", "priority": 50,
            "name": "封顶", "conditions": {},
            "effect": {"type": "amount_cap", "amount_fen": 27000},
            "effective_from": "2026-01-01T00:00:00+08:00"})
        status, body = self._call("GET", "/policies", query="include_withdrawn=false")
        self.assertEqual(1, len(body["items"]))
        self._call("POST", "/policies/withdraw", {"request_id": "w1", "policy_id": "cap"})
        status, body = self._call("GET", "/policies", query="include_withdrawn=false")
        self.assertEqual(0, len(body["items"]))


if __name__ == "__main__":
    unittest.main()
