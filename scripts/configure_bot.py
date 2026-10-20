"""Bind the owner's QQ and save credentials directly to GitHub Actions Secrets."""

import argparse
import asyncio
import contextlib
import getpass
import json
import re
import secrets
import subprocess

import websockets

from qq_bot import QQBotClient


REPO = "greenthree/github-qq-notify"


async def heartbeat(ws, interval, sequence):
    while True:
        await asyncio.sleep(interval)
        if sequence[1]:
            await ws.close()
            raise RuntimeError("QQ 网关心跳超时，请重试")
        sequence[1] = True
        await ws.send(json.dumps({"op": 1, "d": sequence[0]}))


async def capture_user(url, token, challenge):
    async with websockets.connect(url, open_timeout=15, close_timeout=5) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if hello.get("op") != 10 or hello["d"]["heartbeat_interval"] <= 0:
            raise RuntimeError("QQ 网关未返回有效 Hello")
        await ws.send(json.dumps({"op": 2, "d": {
            "token": f"QQBot {token}", "intents": 1 << 25,
        }}))
        sequence = [None, False]
        heart = asyncio.create_task(heartbeat(ws, hello["d"]["heartbeat_interval"] / 1000, sequence))
        ready = False
        deadline = asyncio.get_running_loop().time() + 30
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0, deadline - asyncio.get_running_loop().time()))
                event = json.loads(raw)
                if event.get("s") is not None:
                    sequence[0] = event["s"]
                if event.get("op") in (7, 9):
                    raise RuntimeError("QQ 网关拒绝会话或要求重连，请重试")
                if event.get("op") == 11:
                    sequence[1] = False
                if event.get("op") != 0:
                    continue
                if event.get("t") == "READY":
                    ready = True
                    deadline = asyncio.get_running_loop().time() + 180
                    print(f"请用你本人的 QQ 私信新机器人以下口令（3 分钟内）：\n{challenge}", flush=True)
                elif ready and event.get("t") == "C2C_MESSAGE_CREATE":
                    data = event["d"]
                    if data.get("content", "").strip() != challenge:
                        continue
                    user = data.get("author", {}).get("user_openid", "")
                    if not re.fullmatch(r"[0-9a-fA-F]{32}", user):
                        raise ValueError("单聊事件未包含有效 user_openid")
                    return user
        finally:
            heart.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heart


def gh(*args, value=None):
    result = subprocess.run(["gh", *args], input=value, text=True, capture_output=True, timeout=30)
    if result.returncode:
        # Suppress subprocess output: future gh versions may include request details.
        raise RuntimeError("GitHub 配置写入失败，请检查 gh 登录及仓库管理权限")


def main():
    parser = argparse.ArgumentParser(description="绑定本人 QQ 并直接保存新通知仓库的 Secrets")
    parser.add_argument("--sandbox", action="store_true", help="使用沙箱机器人")
    args = parser.parse_args()
    gh("auth", "status")
    app_id = input("新机器人的 AppID：").strip()
    secret = getpass.getpass("新机器人的 AppSecret（隐藏输入）：").strip()
    if not app_id or not secret:
        raise ValueError("AppID 和 AppSecret 不能为空")
    bot = QQBotClient(app_id, secret, args.sandbox)
    token = bot.get_access_token()
    user = asyncio.run(capture_user(bot.get_gateway_url(), token, f"bind {secrets.token_hex(8)}"))
    for name, value in (("QQ_GITHUB_BOT_APPID", app_id), ("QQ_GITHUB_BOT_SECRET", secret),
                        ("QQ_GITHUB_USER_OPENID", user)):
        gh("secret", "set", name, "--repo", REPO, value=value)
    gh("variable", "set", "QQ_GITHUB_SANDBOX", "--repo", REPO,
       "--body", "true" if args.sandbox else "false")
    print("三个 Secrets 已保存至 greenthree/github-qq-notify。请到 Actions 运行 test_message 联调。")


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, (RuntimeError, ValueError)):
            print(f"配置未完成：{error}")
        else:
            print("配置未完成；请检查网络、单聊权限、gh 登录后重试。")
        raise SystemExit(1) from None
