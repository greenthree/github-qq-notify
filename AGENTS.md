# GitHub QQ 通知工具维护规范

- 本仓库公开，代码、Actions 日志、通知状态分支和提交历史均可被查看。
- AppSecret、接收人的 OpenID 只使用 Actions Secrets 或本地隐藏输入；禁止提交、打印或放进命令参数。
- 当前只查询公开仓库 `greenthree/meiriyiti-contributions` 的 PR 元数据，不下载或执行投稿代码。
- 发送私信的工作流只允许 `schedule` 和维护者的 `workflow_dispatch`，代码固定检出 `main`。
  PR 测试使用只读权限，不注入 Secrets，不添加 `pull_request_target` 或自托管 runner。
- 新增消息来源复用 `qq_bot.py`，不要为每个来源重复实现 QQ 鉴权与发送。
- 消息正文默认不写日志；公开状态仅存哈希键、PR 编号、状态、时间和公开提交摘要，不存私信正文、OpenID 或原始 QQ 消息 ID。
- 发送前持久记录 `pending`，QQ API 确认消息 ID 后记录 `sent`；明确拒绝可重试，未知结果禁止自动重发。
- 保持实现简单，不引入数据库或常驻后端。更改后执行 `python -m unittest discover -s tests -v`。
