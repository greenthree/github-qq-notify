# GitHub → QQ 私人通知

用独立的 QQ 官方机器人，将 GitHub 消息私信给仓库负责人。
目前每 **30 分钟**检查一次 [每日一题投稿仓库](https://github.com/greenthree/meiriyiti-contributions)；
发现新题投稿 PR 后发送作者、标题、状态和审核链接，同一 PR 只提醒一次。

本仓库只维护通知工具。投稿仍在投稿仓库提交，审核和正式入库仍在每日一题仓库完成。
以后其他 GitHub 通知可复用同一 QQ 私信通道。

## 配置机器人

1. 在 QQ 开放平台开启新机器人的单聊能力，在 QQ 中添加机器人并开启接收主动消息。
   沙箱阶段将你本人添加为测试用户；如平台要求消息 URL 白名单，添加 `github.com`。
2. 本地安装 Python 3.11 和已登录的 GitHub CLI，然后运行：

   ```powershell
   git clone https://github.com/greenthree/github-qq-notify.git
   cd github-qq-notify
   python -m pip install -r requirements.txt
   python scripts/configure_bot.py
   ```

   输入**新机器人**的 AppID 和 AppSecret（隐藏输入），再用你本人的 QQ 私信机器人屏幕显示的 `bind ...` 口令。
   工具会把三项配置直接写入**本仓库**的 Actions Secrets，不打印密钥或接收人的 OpenID。
   使用沙箱时，最后一条命令改为 `python scripts/configure_bot.py --sandbox`。

   如需手动配置，进入 [本仓库 Actions Secrets](https://github.com/greenthree/github-qq-notify/settings/secrets/actions)：

   | Secret | 内容 |
   | --- | --- |
   | `QQ_GITHUB_BOT_APPID` | 新机器人的 AppID |
   | `QQ_GITHUB_BOT_SECRET` | 新机器人的 AppSecret / ClientSecret |
   | `QQ_GITHUB_USER_OPENID` | 你在新机器人中的单聊用户 OpenID，不能用 QQ 号或群成员 OpenID |

3. 打开 [Actions → GitHub QQ Notifications](https://github.com/greenthree/github-qq-notify/actions/workflows/notify.yml)，
   Run workflow 勾选 `test_message`，确认本人收到测试私信。
4. 在仓库 Actions Variables 添加 `QQ_GITHUB_NOTIFY_ENABLED=true`，并手动运行一次（两个勾选均关闭）。
   首次运行记录启用时间，此后新建的投稿开始提醒。沙箱额外配置 `QQ_GITHUB_SANDBOX=true`，配置工具会自动设置它。

不需要向投稿仓库、正式题库或第三方服务提供机器人的凭据。

## 运行与隐私

- 定时检查约每 30 分钟一次，GitHub 排队可能造成延迟。
  公开仓库使用标准 GitHub 托管 Linux runner 的 [Actions 分钟免费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。
- 只读取公开 PR 元数据和文件路径，不下载或执行投稿代码；查询投稿 API 不携带通知仓库的写入令牌。
- 带密钥的通知任务只由定时器或维护者手动触发，始终使用 `main` 代码。
  外部 PR 只能运行不含 Secrets 的只读测试。
- 代码、Actions 日志和 `notification-state` 状态分支均公开。密钥和接收人的 OpenID 保存在 Secrets；
  日志不打印私信正文，状态仅保存哈希键、启用时间、PR 编号、状态和公开提交摘要，不保存原始 QQ 消息 ID。
- 当前仅支持公开投稿来源；以后接入私人事件时，不得把其标题、正文或其他私人元数据写入公开日志或状态。
- `notification-state` 分支中的 `.state/notifications.json` 用于重跑去重，不合并到 main，也不要删除。
- 首次启用前的历史 PR 不补发。新稿草稿也提醒；文档或工具 PR 不提醒。
  后续提交、改标题、重新打开同一个已通知的 PR 均不会重复提醒。
- 手动勾选 `dry_run` 可预览当前未关闭投稿，不使用 QQ 凭据、不发送、不写通知状态。
  `dry_run` 与 `test_message` 不可同时勾选。
- 公开仓库连续 60 天无活动时，GitHub 可能 [自动停用定时工作流](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。
  通知状态提交发生在有新事件时，不能保证持续保持活跃；长时间没有投稿时需检查 Actions 是否仍启用。

## 发送失败

发送前保存 `pending`，QQ API 确认消息 ID 后保存 `sent`。明确拒绝保存 `rejected`，下次检查可重试。
超时或异常响应的实际投递结果可能不明，保留 `pending` 并使工作流失败，禁止自动重发。

出现 `pending` 时，按日志的 PR 编号核对 QQ 私信，再编辑状态分支中的对应 `kind=submission` 条目：
确认收到改为 `sent`；确认未收到且接受重复风险时改为 `rejected`，再运行工作流。
不要删除整个状态文件或启用时间。GitHub 的 Actions 失败通知可作为独立的故障提醒。

## 本地检查

```powershell
python -m unittest discover -s tests -v
python scripts/notify_github.py --dry-run
```

QQ 官方文档：[单聊发送接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)、
[主动消息权限与频控](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)。
