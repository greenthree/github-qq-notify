"""Durable delivery intent with local locking and GitHub compare-and-swap writes."""

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import requests


ROOT = Path(__file__).resolve().parents[1]


class Journal:
    def __init__(self, path=None):
        self.path = Path(path or ROOT / ".state/notifications.json")
        self.branch = os.getenv("NOTIFY_STATE_BRANCH")
        self.sha = None
        self.session = requests.Session()
        if self.branch:
            repo = os.environ["GITHUB_REPOSITORY"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                raise ValueError("通知仓库名称无效")
            self.session.headers.update({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                         "Accept": "application/vnd.github+json",
                                         "X-GitHub-Api-Version": "2022-11-28"})
            self.url = f"https://api.github.com/repos/{repo}/contents/.state/notifications.json"
            ref = self.session.get(f"https://api.github.com/repos/{repo}/git/ref/heads/{self.branch}", timeout=15)
            if ref.status_code == 404:
                self.request("post", f"https://api.github.com/repos/{repo}/git/refs", json={
                    "ref": f"refs/heads/{self.branch}", "sha": os.environ["GITHUB_SHA"],
                })
                self.request("put", self.url, json={"message": "Initialize notification state",
                    "branch": self.branch, "content": "e30K"})
            elif ref.status_code != 200:
                raise RuntimeError("无法读取通知状态分支，停止投递")
            response = self.request("get", self.url, params={"ref": self.branch}).json()
            self.sha = response["sha"]
            self.data = json.loads(base64.b64decode(response["content"]))
        else:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(self.data, dict) or any(
            not isinstance(entry, dict) or entry.get("status") not in ("pending", "sent", "rejected")
            for entry in self.data.values()
        ):
            raise ValueError("通知状态损坏，禁止使用空记录替代")

    def request(self, method, url, **kwargs):
        response = self.session.request(method, url, timeout=15, **kwargs)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"GitHub 状态操作失败：HTTP {response.status_code}；停止投递")
        return response

    @staticmethod
    def key(*parts):
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()

    def record(self, key, status, **fields):
        self.data[key] = {"status": status, **fields}
        self.save()

    def save(self):
        text = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        if self.branch:
            response = self.request("put", self.url, json={
                "message": "Record notification delivery state", "branch": self.branch, "sha": self.sha,
                "content": base64.b64encode(text.encode()).decode(),
            })
            self.sha = response.json()["content"]["sha"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


@contextlib.contextmanager
def local_lock():
    directory = ROOT / ".state"
    directory.mkdir(exist_ok=True)
    with (directory / "run.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("已有本地通知任务运行") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def run_cli(main):
    try:
        with local_lock():
            main()
    except requests.RequestException:
        # A requests exception may include the private user OpenID in its URL.
        print("网络请求失败；请查看工作流步骤和投递状态。", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print(f"[错误] {error}", file=sys.stderr)
        sys.exit(1)
