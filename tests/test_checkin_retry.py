import unittest
from unittest.mock import patch
import workbuddy_checkin as w


NOT_SIGNED = {"active": True, "today_checked_in": False}
SIGNED = {"active": True, "today_checked_in": True, "streak_days": 5}
AUTH = {"auth": {"domain": "codebuddy.cn", "accessToken": "dummy"},
        "account": {"uid": "test-account", "nickname": "test"}}
TEMP_ERROR = ({"success": False, "message": "code=10001 msg=签到失败，请稍后重试"}, None)
OK = ({"success": True, "message": "签到成功"}, None)


class CheckinRetryTest(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(w.time, "sleep", return_value=None)
        self.delay = patch.object(w, "CHECKIN_RETRY_DELAYS", (1,))
        self.token = patch.object(w, "ensure_token_valid", side_effect=lambda p, a: a)
        self.credit = patch.object(w, "fetch_user_resource", return_value=({"total_remain": 100}, None))
        for p in (self.sleep, self.delay, self.token, self.credit):
            p.start()
            self.addCleanup(p.stop)

    def test_temporary_10001_recovers(self):
        with patch.object(w, "fetch_checkin_status", side_effect=[
            (NOT_SIGNED, None), (NOT_SIGNED, None), (SIGNED, None)
        ]), patch.object(w, "perform_checkin", side_effect=[TEMP_ERROR, OK]) as call:
            r = w.checkin_one_account("dummy.json", AUTH)
            self.assertTrue(r["success"])
            self.assertEqual(2, call.call_count)
            self.assertEqual(5, r["checkin_status"]["streak_days"])

    def test_uncertain_request_verifies_status_before_retry(self):
        with patch.object(w, "fetch_checkin_status", side_effect=[
            (NOT_SIGNED, None), (SIGNED, None)
        ]), patch.object(w, "perform_checkin", return_value=TEMP_ERROR) as call:
            r = w.checkin_one_account("dummy.json", AUTH)
            self.assertEqual(1, call.call_count)
            self.assertTrue(r["success"])
            self.assertEqual("already", r["reason"])

    def test_persistent_error_is_not_success(self):
        with patch.object(w, "fetch_checkin_status", return_value=(NOT_SIGNED, None)), \
             patch.object(w, "perform_checkin", return_value=TEMP_ERROR) as call:
            r = w.checkin_one_account("dummy.json", AUTH)
            self.assertFalse(r["success"])
            self.assertIn("10001", r["error"])
            self.assertEqual(2, call.call_count)

    def test_already_signed_skips_request(self):
        with patch.object(w, "fetch_checkin_status", return_value=(SIGNED, None)), \
             patch.object(w, "perform_checkin") as call:
            r = w.checkin_one_account("dummy.json", AUTH)
            self.assertEqual("already", r["reason"])
            call.assert_not_called()

    def test_do_not_mistake_today_error_for_signed(self):
        self.assertFalse(w._already_checked_message("今日签到失败"))
        self.assertTrue(w._already_checked_message("今日已签到"))


if __name__ == "__main__":
    unittest.main()
