# Mai_Only_You（麦麦，只对你）

仅对 QQ 私聊好友主动发消息，结合静默时间、上下文与记忆检索。

## 功能概览
- 仅 QQ 私聊触发
- 静默阈值触发 + 定时扫描
- 禁止时段（默认 01:00~06:00）
- 未回复当天不再主动
- 记忆检索优先，未命中继续使用上下文

## 配置说明
> 系统会根据 `config_schema` 自动生成配置文件。首次启用插件后，`config.toml` 会自动出现在插件目录中，请在生成后按需修改。

关键配置项：
- `plugin.enabled`：是否启用插件
- `filtering.mode` / `filtering.users`：私聊名单过滤（黑名单/白名单）
- `schedule.scan_interval_minutes`：定时扫描间隔
- `silence_detection.silence_threshold_minutes`：静默阈值
- `quiet_hours.quiet_hours_start` / `quiet_hours.quiet_hours_end`：禁止时段（24 小时制 HH:MM）
- `limits.min_interval_hours` / `limits.daily_max` / `limits.require_reply_before_next`：频率与未回复策略（未回复当天不触发）
- `context.history_messages`：上下文回溯条数
- `memory.question_template`：记忆检索问题模板（支持 `{user_name}` / `{user_id}`）


名单示例（TOML 数组，空列表默认放行所有好友）：
```toml
[filtering]
# 黑名单：命中则不触发
mode = "blocklist"
users = ["123456789", "987654321"]

# 白名单：仅命中才触发
mode = "allowlist"
users = ["123456789", "987654321", "135792468"]
```

## 调试命令
- `/mai_only_you_test`：在当前 QQ 私聊中手动触发
- `/mai_only_you_test <QQ号>`：指定 QQ 私聊触发
