import unittest

from transport_coordination.billing_acceptance import run


class BillingAcceptanceTest(unittest.TestCase):
    def test_offline_billing_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["pending_then_supplemental"])
        self.assertEqual(27000, result["first_bill_fen"])
        self.assertEqual(13500, result["final_bill_fen"])
        self.assertEqual(2, result["fact_versions"])
        self.assertEqual(12000, result["settled_net_fen"])
        self.assertTrue(result["late_event_immutable"])
        self.assertEqual(24000, result["night_freight_fen"])
        self.assertEqual(0, result["livelihood_fen"])
        self.assertEqual(30000, result["withdraw_new_trip_fen"])
        self.assertTrue(result["dispute_froze_only_related"])
        self.assertEqual(13500, result["replay_fen"])


if __name__ == "__main__":
    unittest.main()
