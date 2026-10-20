"""Poll public submission metadata and deliver notifications to the owner's QQ."""

import argparse
import datetime as dt
import os
import re

import requests

from delivery_state import Journal, run_cli
from qq_bot import DeliveryRejected, QQBotClient


SOURCE_REPO = "greenthree/meiriyiti-contributions"
SUBMISSION_PATH = re.compile(r"submissions/[a-z0-9]+(?:-[a-z0-9]+)*/.+")


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("通知起始时间必须含时区")
    return parsed.astimezone(dt.timezone.utc)


class GitHubSubmissions:
    def __init__(self, repo=SOURCE_REPO):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("投稿仓库必须为 owner/repo")
        self.repo = repo
        self.session = requests.Session()
        # Public metadata needs no token. Keep the state writer's token separate.
        self.session.headers.update({"Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"})

    def pages(self, path, **params):
        for page in range(1, 1001):
            response = self.session.get(
                f"https://api.github.com/repos/{self.repo}/{path}",
                params={**params, "per_page": 100, "page": page}, timeout=15,
            )
            response.raise_for_status()
            items = response.json()
            if not isinstance(items, list):
                raise ValueError("GitHub 分页响应格式异常")
            yield items
            if len(items) < 100:
                return
        raise ValueError("GitHub 分页超出上限，停止推进通知记录")

    def pulls(self, since=None):
        # Include closed PRs so a submission closed between two polls is still reported.
        state = "all" if since else "open"
        for page in self.pages("pulls", state=state, sort="created", direction="desc"):
            for pull in page:
                if since and timestamp(pull["created_at"]) < since:
                    return
                yield pull

    def is_submission(self, number):
        return any(SUBMISSION_PATH.fullmatch(item["filename"])
                   for page in self.pages(f"pulls/{number}/files") for item in page)


def message(repo, pull):
    number = pull["number"]
    if type(number) is not int or number <= 0:
        raise ValueError("GitHub PR 编号无效")
    # PR titles are untrusted text: keep them on one bounded line, never run them.
    title = " ".join(str(pull["title"]).split())[:120]
    author = " ".join(str(pull["user"]["login"]).split())[:80]
    status = "已合并" if pull.get("merged_at") else (
        "已关闭" if pull["state"] == "closed" else "草稿" if pull.get("draft") else "待审核")
    return (f"[GitHub 新题投稿]\n仓库：{repo}\nPR：#{number} {title}\n"
            f"作者：{author}\n状态：{status}\n"
            f"审核链接：https://github.com/{repo}/pull/{number}")


def notify(source, bot, user_openid, journal):
    start_key = journal.key("github-submissions-start", source.repo)
    if start_key not in journal.data:
        journal.record(start_key, "sent", since=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
        print("已记录启用时间；只提醒此后创建的投稿 PR。")
    since = timestamp(journal.data[start_key]["since"])
    failures = []
    token_ready = False
    for pull in source.pulls(since):
        number = pull["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("GitHub PR 编号无效")
        key = journal.key("github-submission", bot.app_id, bot.use_sandbox,
                          user_openid, source.repo, number)
        previous = journal.data.get(key, {})
        if previous.get("status") == "sent":
            if previous.get("kind") == "submission" or previous.get("head") == pull["head"]["sha"]:
                continue
        if previous.get("status") == "pending":
            failures.append(f"#{number} 上次投递结果不明，请先核对 QQ 私信和状态记录")
            continue
        if not source.is_submission(number):
            journal.record(key, "sent", pr=number, kind="not-submission", head=pull["head"]["sha"])
            continue
        content = message(source.repo, pull)
        if not token_ready:
            try:
                bot.get_access_token()
            except (RuntimeError, ValueError, requests.RequestException):
                raise RuntimeError("无法获取私人通知机器人的凭证，请检查配置") from None
            token_ready = True
        # Durable intent precedes the non-idempotent POST. Never automatically retry a timeout.
        journal.record(key, "pending", pr=number, kind="submission")
        try:
            bot.send_private_message(user_openid, content)
        except DeliveryRejected as error:
            journal.record(key, "rejected", pr=number, kind="submission")
            failures.append(f"#{number} 投递被拒绝：{error}")
        except (RuntimeError, ValueError, requests.RequestException):
            failures.append(f"#{number} 投递结果不明，请先核对 QQ 私信和状态记录")
        else:
            journal.record(key, "sent", pr=number, kind="submission")
            print(f"已发送投稿 #{number} 的私人提醒。")
    if failures:
        raise RuntimeError("；".join(failures))
    print("投稿检查完成。")


def private_bot():
    names = ("QQ_GITHUB_BOT_APPID", "QQ_GITHUB_BOT_SECRET", "QQ_GITHUB_USER_OPENID")
    values = [os.getenv(name, "").strip() for name in names]
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise ValueError("缺少私人通知配置：" + ", ".join(missing))
    if not re.fullmatch(r"[0-9a-fA-F]{32}", values[2]):
        raise ValueError("QQ_GITHUB_USER_OPENID 必须为本机器人对应的 32 位 user_openid")
    sandbox = os.getenv("QQ_GITHUB_SANDBOX", "false").lower()
    if sandbox not in ("true", "false", "1", "0"):
        raise ValueError("QQ_GITHUB_SANDBOX 必须为 true/false/1/0")
    return QQBotClient(values[0], values[1], sandbox in ("true", "1")), values[2]


def main():
    parser = argparse.ArgumentParser(description="GitHub 投稿的 QQ 私人通知")
    parser.add_argument("--dry-run", action="store_true", help="预览当前未关闭投稿；不发送或修改状态")
    parser.add_argument("--test", action="store_true", help="仅发送一条私人通知测试消息")
    args = parser.parse_args()
    if args.dry_run and args.test:
        raise ValueError("--dry-run 与 --test 不能同时使用")
    source = GitHubSubmissions()
    if args.dry_run:
        for pull in source.pulls():
            if source.is_submission(pull["number"]):
                print(message(source.repo, pull))
        print("预览完成；未发送消息或修改通知记录。")
        return
    bot, user_openid = private_bot()
    if args.test:
        try:
            bot.send_private_message(user_openid, "[GitHub 私人通知测试]\n新题投稿提醒通道已连通。")
        except (RuntimeError, ValueError, requests.RequestException):
            raise RuntimeError("测试投递未能确认成功；请核对 QQ、主动消息权限、域名白名单和机器人配置") from None
        print("QQ API 已确认测试消息发送成功。")
        return
    notify(source, bot, user_openid, Journal())


if __name__ == "__main__":
    run_cli(main)
