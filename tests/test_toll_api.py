import unittest

from transport_coordination.api import route
from transport_coordination.storage import Database
from transport_coordination.toll_service import TollService


def bootstrap(service: TollService) -> None:
    service.register_organization(request_id="org-o1", actor_id="bootstrap",
                                  organization_id="o1", name="东段公司")
    service.register_organization(request_id="org-gov", actor_id="bootstrap",
                                  organization_id="gov", name="政策中心")
    service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                           display_name="管理员", role="admin", organization_id="gov")
    service.register_actor(request_id="rv", actor_id="admin-1", new_actor_id="rv-1",
                           display_name="政策员", role="reviewer", organization_id="gov")
    service.register_actor(request_id="op", actor_id="admin-1", new_actor_id="op-1",
                           display_name="收费员", role="operator", organization_id="o1")
    service.register_segment(request_id="seg-a", actor_id="admin-1", segment_id="seg-a",
                             organization_id="o1", name="东段",
                             rate_fen_per_km=50, length_m=100_000)


class TollApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = TollService(self.database)
        bootstrap(self.service)
        self.headers = {"X-Actor-Id": "op-1"}

    def tearDown(self):
        self.database.close()

    def post(self, path, body, actor="op-1"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def test_full_trip_flow_over_http(self):
        status, body = self.post("/toll/policies", {
            "request_id": "p1", "policy_id": "holiday", "kind": "discount",
            "priority": 100, "scope": {}, "params": {"mode": "percent", "permille": 900},
            "effective_at": "2026-10-01T00:00:00Z", "expires_at": "2026-10-08T00:00:00Z",
            "published_org": "gov"}, actor="rv-1")
        self.assertEqual(200, status)

        status, body = self.post("/toll/trips", {
            "request_id": "t1", "trip_id": "trip-1",
            "vehicle_plate": "京A1", "vehicle_class": "passenger",
            "segment_ids": ["seg-a"], "entry_time": "2026-10-01T10:00:00Z"})
        self.assertEqual(200, status)

        status, body = self.post("/toll/trips/pending-evidence",
                                 {"request_id": "pe1", "trip_id": "trip-1"})
        self.assertEqual(200, status)
        self.assertEqual("pending_evidence", body["status"])

        status, body = self.post("/toll/trips/exit", {
            "request_id": "ex1", "trip_id": "trip-1",
            "event_time": "2026-10-01T12:00:00Z"})
        self.assertEqual(200, status)
        self.assertEqual(4500, body["total_fen"])

        status, body = self.post("/toll/trips/settle",
                                 {"request_id": "s1", "trip_id": "trip-1"})
        self.assertEqual(200, status)

        status, explanation = route(self.service, "GET", "/toll/trips/trip-1", None, self.headers)
        self.assertEqual(200, status)
        self.assertEqual(4500, explanation["billing"]["total_fen"])
        self.assertEqual("settled", explanation["trip"]["status"])
        self.assertIn("o1", explanation["organization_shares"])

    def test_late_exit_after_settlement_is_conflict(self):
        self.post("/toll/trips", {
            "request_id": "t1", "trip_id": "trip-1",
            "vehicle_plate": "京A1", "vehicle_class": "passenger",
            "segment_ids": ["seg-a"], "entry_time": "2026-10-01T10:00:00Z"})
        self.post("/toll/trips/exit", {
            "request_id": "ex1", "trip_id": "trip-1",
            "event_time": "2026-10-01T12:00:00Z"})
        self.post("/toll/trips/settle",
                  {"request_id": "s1", "trip_id": "trip-1"})
        status, body = self.post("/toll/trips/exit", {
            "request_id": "late", "trip_id": "trip-1",
            "event_time": "2026-10-02T01:00:00Z"})
        self.assertEqual(409, status)
        self.assertEqual("conflict", body["error"])

    def test_replay_endpoint_requires_as_of(self):
        status, body = route(self.service, "GET", "/toll/trips/trip-1/replay", None, self.headers)
        self.assertEqual(400, status)

    def test_policy_listing_and_detail(self):
        self.post("/toll/policies", {
            "request_id": "p1", "policy_id": "cap1", "kind": "cap",
            "priority": 1, "scope": {}, "params": {"max_fen": 9000},
            "effective_at": "2026-01-01T00:00:00Z", "published_org": "gov"}, actor="rv-1")
        status, body = route(self.service, "GET", "/toll/policies", None, self.headers)
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["items"]))
        status, body = route(self.service, "GET", "/toll/policies/cap1", None, self.headers)
        self.assertEqual(200, status)
        self.assertEqual("cap", body["kind"])
        self.assertEqual(1, len(body["versions"]))

    def test_operator_forbidden_from_policy_publish(self):
        status, body = self.post("/toll/policies", {
            "request_id": "p2", "policy_id": "cap2", "kind": "cap",
            "priority": 1, "scope": {}, "params": {"max_fen": 9000},
            "effective_at": "2026-01-01T00:00:00Z"})
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
