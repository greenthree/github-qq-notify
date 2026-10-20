import asyncio
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import configure_bot as gateway
import notify_github as notify
from delivery_state import Journal
from qq_bot import DeliveryRejected, QQBotClient


def pull(number=12, **extra):
    return {"number": number, "title": "测试投稿", "user": {"login": "author"},
            "state": "open", "draft": False, "head": {"sha": "a" * 40},
            "created_at": "2026-10-21T01:00:00Z", **extra}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.env = patch.dict(os.environ, {"NOTIFY_STATE_BRANCH": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.source = Mock(repo=notify.SOURCE_REPO)
        self.source.pulls.return_value = [pull()]
        self.source.is_submission.return_value = True
        self.bot = Mock(app_id="private-bot", use_sandbox=False)
        self.bot.send_private_message.return_value = {"id": "qq-message"}
        self.user = "A" * 32

    def journal(self):
        return Journal(self.path)

    def deliver(self):
        notify.notify(self.source, self.bot, self.user, self.journal())

    def test_restart_and_changed_head_never_duplicate_a_pr(self):
        self.deliver()
        self.source.pulls.return_value = [pull(title="新标题", head={"sha": "new"})]
        self.deliver()
        self.bot.send_private_message.assert_called_once()
        content = self.bot.send_private_message.call_args.args[1]
        self.assertIn("https://github.com/greenthree/meiriyiti-contributions/pull/12", content)
        self.assertNotIn(self.user, self.path.read_text())
        self.assertNotIn("qq-message", self.path.read_text())

    def test_documentation_prs_are_ignored_persistently(self):
        self.source.is_submission.return_value = False
        self.deliver()
        self.deliver()
        self.bot.get_access_token.assert_not_called()
        self.bot.send_private_message.assert_not_called()
        self.source.is_submission.assert_called_once_with(12)

    def test_ignored_draft_that_later_adds_a_submission_is_rechecked(self):
        self.source.is_submission.return_value = False
        self.deliver()
        self.source.pulls.return_value = [pull(head={"sha": "b" * 40})]
        self.source.is_submission.return_value = True
        self.deliver()
        self.bot.send_private_message.assert_called_once()

    def test_pending_is_saved_before_a_send_and_survives_timeout(self):
        def timeout(*args):
            self.assertIn("pending", self.path.read_text())
            raise requests.ReadTimeout("response lost")
        self.bot.send_private_message.side_effect = timeout
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "结果不明"):
                self.deliver()
        self.bot.send_private_message.assert_called_once()

    def test_only_explicit_rejection_can_be_retried(self):
        self.bot.send_private_message.side_effect = [DeliveryRejected("HTTP 403"), {"id": "success"}]
        with self.assertRaisesRegex(RuntimeError, "被拒绝"):
            self.deliver()
        self.deliver()
        self.deliver()
        self.assertEqual(self.bot.send_private_message.call_count, 2)

    def test_one_failed_pr_does_not_block_other_new_submissions(self):
        self.source.pulls.return_value = [pull(13), pull(12)]
        self.bot.send_private_message.side_effect = [requests.ReadTimeout(), {"id": "success"}]
        with self.assertRaises(RuntimeError):
            self.deliver()
        self.assertEqual(self.bot.send_private_message.call_count, 2)
        entries = [e for e in self.journal().data.values() if e.get("kind") == "submission"]
        self.assertEqual({e["pr"]: e["status"] for e in entries}, {13: "pending", 12: "sent"})

    def test_auth_failure_is_not_recorded_as_pending_or_leaked(self):
        self.bot.get_access_token.side_effect = RuntimeError("secret-repeated-by-upstream")
        with self.assertRaisesRegex(RuntimeError, "私人通知机器人的凭证") as error:
            self.deliver()
        self.assertNotIn("secret-repeated", str(error.exception))
        self.assertNotIn("pending", self.path.read_text())
        self.bot.send_private_message.assert_not_called()

    def test_failed_state_save_prevents_network_send(self):
        journal = self.journal()
        key = journal.key("github-submissions-start", self.source.repo)
        journal.record(key, "sent", since="2026-10-20T00:00:00Z")
        with patch.object(journal, "save", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                notify.notify(self.source, self.bot, self.user, journal)
        self.bot.send_private_message.assert_not_called()

    def test_baseline_is_persisted_and_not_moved_on_future_runs(self):
        self.source.pulls.return_value = []
        self.deliver()
        since = self.source.pulls.call_args.args[0]
        self.deliver()
        self.assertEqual(since, self.source.pulls.call_args.args[0])

    def test_private_config_never_falls_back_to_daily_group_credentials(self):
        with patch.dict(os.environ, {"QQ_BOT_APPID": "daily", "QQ_BOT_SECRET": "secret"}, clear=True):
            with self.assertRaisesRegex(ValueError, "QQ_GITHUB_BOT_APPID"):
                notify.private_bot()

    def test_direct_private_endpoint_has_no_passive_reply_or_group_fallback(self):
        bot = QQBotClient("private", "secret")
        bot._token = "token"
        bot.session = Mock()
        bot.session.post.return_value = Mock(status_code=200)
        bot.session.post.return_value.json.return_value = {"id": "sent"}
        bot.send_private_message(self.user, "notification")
        call = bot.session.post.call_args
        self.assertEqual(call.args[0], f"https://api.sgroup.qq.com/v2/users/{self.user}/messages")
        self.assertEqual(call.kwargs["json"], {"content": "notification", "msg_type": 0})
        self.assertFalse(call.kwargs["allow_redirects"])
        with self.assertRaises(ValueError):
            bot.send_private_message("123456789", "notification")
        self.assertEqual(bot.session.post.call_count, 1)

    def test_preview_needs_no_qq_secrets_and_writes_no_state(self):
        with patch.object(sys, "argv", ["notify", "--dry-run"]), \
                patch.object(notify, "GitHubSubmissions", return_value=self.source), \
                patch.object(notify, "private_bot") as bot, patch.object(notify, "Journal") as journal:
            notify.main()
        bot.assert_not_called()
        journal.assert_not_called()

    def test_message_bounds_untrusted_title_and_describes_closed_draft(self):
        text = notify.message(notify.SOURCE_REPO, pull(title="a\n::warning::x" * 100, draft=True))
        self.assertIn("状态：草稿", text)
        self.assertLess(len(text), 450)
        self.assertNotIn("\n::warning", text)
        self.assertIn("状态：已合并", notify.message(notify.SOURCE_REPO, pull(state="closed", merged_at="time")))


class GitHubQueryTests(unittest.TestCase):
    def test_all_states_are_paginated_until_creation_baseline(self):
        source = notify.GitHubSubmissions()
        first = [pull(n) for n in range(200, 100, -1)]
        second = [pull(100, state="closed"), pull(99, created_at="2026-10-19T00:00:00Z")]
        with patch.object(source, "pages", return_value=iter([first, second])) as pages:
            items = list(source.pulls(notify.timestamp("2026-10-20T00:00:00Z")))
        self.assertEqual(len(items), 101)
        self.assertEqual(items[-1]["state"], "closed")
        self.assertEqual(pages.call_args.kwargs["state"], "all")

    def test_api_pagination_and_public_calls_never_include_private_token(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "private-repo-token"}):
            source = notify.GitHubSubmissions()
        source.session = Mock(headers={})
        responses = [Mock(), Mock()]
        responses[0].json.return_value = [{"filename": f"docs/{n}"} for n in range(100)]
        responses[1].json.return_value = [{"filename": "submissions/array-walk/P0001/problem.md"}]
        source.session.get.side_effect = responses
        self.assertTrue(source.is_submission(12))
        self.assertEqual(source.session.get.call_args_list[1].kwargs["params"]["page"], 2)
        self.assertNotIn("Authorization", source.session.headers)
        self.assertNotIn("private-repo-token", repr(source.session.get.call_args_list))

    def test_invalid_public_api_response_cannot_become_an_empty_poll(self):
        source = notify.GitHubSubmissions()
        source.session = Mock()
        source.session.get.return_value.json.return_value = {"message": "error"}
        with self.assertRaises(ValueError):
            list(source.pulls())


class OpenIDBindingTests(unittest.TestCase):
    def test_binding_ignores_unrelated_users_and_group_events(self):
        events = iter([
            {"op": 10, "d": {"heartbeat_interval": 10000}},
            {"op": 0, "t": "READY", "d": {}},
            {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"content": "hi", "author": {"user_openid": "wrong"}}},
            {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {"content": "bind test", "group_openid": "wrong"}},
            {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"content": "bind test", "author": {"user_openid": "A" * 32}}},
        ])
        class Socket:
            async def recv(self):
                return json.dumps(next(events))
            async def send(self, value):
                pass
        class Connection:
            async def __aenter__(self):
                return Socket()
            async def __aexit__(self, *args):
                pass
        with patch.object(gateway.websockets, "connect", return_value=Connection()), \
                contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(gateway.capture_user("wss://test.invalid", "token", "bind test"))
        self.assertEqual(result, "A" * 32)

    def test_private_workflow_has_no_untrusted_trigger_and_uses_distinct_secrets(self):
        path = ROOT / ".github/workflows/notify.yml"
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"schedule", "workflow_dispatch"})
        checkout = workflow["jobs"]["notify"]["steps"][0]
        self.assertEqual(checkout["with"]["ref"], "main")
        contents = path.read_text(encoding="utf-8")
        self.assertNotIn("secrets.QQ_BOT_SECRET", contents)
        self.assertIn("secrets.QQ_GITHUB_BOT_SECRET", contents)
        tests = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertNotIn("secrets.", tests)
        self.assertNotIn("pull_request_target", contents + tests)


if __name__ == "__main__":
    unittest.main()
