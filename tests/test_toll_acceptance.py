import unittest

from transport_coordination.toll_acceptance import run


class TollAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(7000, result["trip1_billed_fen"])
        self.assertEqual(8600, result["trip1_gross_fen"])
        self.assertEqual({"o1": 581, "o2": 419}, result["trip1_refund_split"])
        self.assertTrue(result["trip2_settlement_balanced"])
        self.assertTrue(result["late_exit_on_settled_rejected"])
        self.assertEqual(5000, result["trip3_rebilled_fen_after_withdraw"])
        self.assertEqual(8600, result["replay_before_holiday_fen"])


if __name__ == "__main__":
    unittest.main()
