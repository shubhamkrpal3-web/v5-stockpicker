"""telegram_broadcaster.send(): a Telegram outage is retried and then returned as a failure, never raised, so the
pipeline that produced the message (and its data commit) does not fail with it. No network: requests.post and
time.sleep are replaced."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "v5_modules"))
import telegram_broadcaster as tb  # noqa: E402

CREDS = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "-100"}


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class TelegramSend(unittest.TestCase):
    def send(self, post, **kw):
        with mock.patch.dict(os.environ, CREDS), mock.patch.object(tb.requests, "post", side_effect=post), \
                mock.patch.object(tb.time, "sleep") as sleep:
            resp = tb.send("hello", **kw)
        return resp, sleep

    def test_timeout_is_retried_then_returned_not_raised(self):
        post = mock.Mock(side_effect=requests.exceptions.ReadTimeout("read timed out"))
        resp, sleep = self.send(post)
        self.assertEqual(post.call_count, 3)
        self.assertFalse(resp["ok"])
        self.assertIn("ReadTimeout", resp["error"])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])     # back-off between attempts only

    def test_gateway_timeout_then_success(self):
        post = mock.Mock(side_effect=[_Resp(504, {"ok": False, "error_code": 504, "description": "Gateway Timeout"}),
                                      _Resp(200, {"ok": True, "result": {}})])
        resp, _ = self.send(post)
        self.assertTrue(resp["ok"])
        self.assertEqual(post.call_count, 2)

    def test_client_error_is_not_retried(self):
        post = mock.Mock(return_value=_Resp(400, {"ok": False, "error_code": 400, "description": "chat not found"}))
        resp, sleep = self.send(post)
        self.assertEqual((post.call_count, sleep.call_count, resp["error_code"]), (1, 0, 400))

    def test_non_json_body_is_reported(self):
        post = mock.Mock(return_value=_Resp(502, None, text="<html>bad gateway</html>"))
        resp, _ = self.send(post, retries=2)
        self.assertEqual((post.call_count, resp["ok"], resp["error_code"]), (2, False, 502))

    def test_missing_credentials_still_raise(self):
        env = {k: v for k, v in os.environ.items() if k not in CREDS}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                tb.send("hello")


if __name__ == "__main__":
    unittest.main()
