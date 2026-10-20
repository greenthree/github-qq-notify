import base64
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from delivery_state import Journal, run_cli
from qq_bot import DeliveryRejected, QQBotClient
import configure_bot


def response(status=200, **data):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.env = patch.dict(os.environ, {"NOTIFY_STATE_BRANCH": "notification-state",
            "GITHUB_REPOSITORY": "owner/notify", "GITHUB_TOKEN": "state-token", "GITHUB_SHA": "source-sha"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def session(self, data):
        session = Mock()
        session.get.return_value = response()
        session.request.return_value = response(sha="file-sha",
            content=base64.b64encode(json.dumps(data).encode()).decode())
        return session

    def test_durable_state_read_and_write_uses_compare_and_swap(self):
        session = self.session({})
        with patch("delivery_state.requests.Session", return_value=session):
            journal = Journal(self.path)
        session.request.return_value = response(content={"sha": "next-sha"})
        journal.record("hash", "pending", pr=12)
        payload = session.request.call_args.kwargs["json"]
        self.assertEqual(payload["sha"], "file-sha")
        self.assertEqual(payload["branch"], "notification-state")
        self.assertEqual(journal.sha, "next-sha")
        self.assertEqual(json.loads(self.path.read_text())["hash"]["status"], "pending")

    def test_initializes_only_a_missing_branch(self):
        session = self.session({})
        session.get.return_value = response(404)
        with patch("delivery_state.requests.Session", return_value=session):
            Journal(self.path)
        calls = session.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["post", "put", "get"])
        self.assertEqual(calls[0].kwargs["json"]["sha"], "source-sha")
        self.assertNotIn("sha", calls[1].kwargs["json"])

    def test_missing_state_on_existing_branch_is_not_empty_history(self):
        session = self.session({})
        session.request.return_value = response(404)
        with patch("delivery_state.requests.Session", return_value=session):
            with self.assertRaises(RuntimeError):
                Journal(self.path)
        self.assertEqual(session.request.call_count, 1)

    def test_conflicting_state_write_does_not_claim_saved_or_change_sha(self):
        session = self.session({})
        with patch("delivery_state.requests.Session", return_value=session):
            journal = Journal(self.path)
        session.request.return_value = response(409)
        with self.assertRaises(RuntimeError):
            journal.record("hash", "pending", pr=12)
        self.assertEqual(journal.sha, "file-sha")
        self.assertFalse(self.path.exists())

    def test_corrupt_state_blocks_notifications(self):
        for data in ([], {"hash": {"status": "unknown"}}):
            with self.subTest(data=data), patch("delivery_state.requests.Session", return_value=self.session(data)):
                with self.assertRaises(ValueError):
                    Journal(self.path)

    def test_cli_hides_sensitive_request_urls(self):
        def fail():
            raise requests.HTTPError("https://api.sgroup.qq.com/v2/users/private-user-id/messages")
        with patch("delivery_state.local_lock", contextlib.nullcontext), contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                run_cli(fail)
        self.assertNotIn("private-user-id", output.getvalue())


class QQAndSetupTests(unittest.TestCase):
    def test_token_reused_without_logging_upstream_auth_body(self):
        bot = QQBotClient("app", "secret")
        bot.session = Mock()
        bot.session.post.return_value = response(access_token="token")
        self.assertEqual(bot.get_access_token(), "token")
        self.assertEqual(bot.get_access_token(), "token")
        self.assertEqual(bot.session.post.call_count, 1)
        self.assertFalse(bot.session.post.call_args.kwargs["allow_redirects"])
        bot._token = None
        bot.session.post.return_value = response(403, message="secret-and-identifiers")
        with self.assertRaises(RuntimeError) as caught:
            bot.get_access_token()
        self.assertNotIn("secret-and-identifiers", str(caught.exception))

    def test_explicit_rejection_exposes_only_numeric_code(self):
        bot = QQBotClient("app", "secret")
        bot._token = "token"
        bot.session = Mock()
        bot.session.post.return_value = response(403, code=40034105, message="sensitive user")
        with self.assertRaises(DeliveryRejected) as caught:
            bot.send_private_message("A" * 32, "private body")
        self.assertIn("40034105", str(caught.exception))
        self.assertNotIn("sensitive", str(caught.exception))

    def test_sandbox_never_falls_back_to_production_on_timeout(self):
        bot = QQBotClient("app", "secret", use_sandbox=True)
        bot._token = "token"
        bot.session = Mock()
        bot.session.post.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            bot.send_private_message("A" * 32, "private body")
        bot.session.post.assert_called_once()
        self.assertTrue(bot.session.post.call_args.args[0].startswith(bot.SANDBOX_API_HOST))

    def test_cli_passes_secret_on_stdin_and_hides_failed_subprocess_output(self):
        with patch.object(configure_bot.subprocess, "run", return_value=Mock(returncode=0)) as run:
            configure_bot.gh("secret", "set", "QQ_GITHUB_BOT_SECRET", value="new-secret")
        self.assertNotIn("new-secret", " ".join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["input"], "new-secret")
        with patch.object(configure_bot.subprocess, "run", return_value=Mock(returncode=1, stderr="secret")):
            with self.assertRaises(RuntimeError) as caught:
                configure_bot.gh("secret", "set", "QQ_GITHUB_BOT_SECRET", value="secret")
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
