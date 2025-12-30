"""
Mai_Only_You plugin (QQ private proactive chat).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from src.chat.message_receive.chat_stream import get_chat_manager
from src.common.data_models.database_data_model import DatabaseMessages
from src.config.config import global_config, model_config
from src.plugin_system import BasePlugin, ConfigField, get_logger, register_plugin
from src.plugin_system.apis import chat_api, llm_api, message_api, send_api

__path__ = [str(Path(__file__).parent)]

from .components import (
    MaiOnlyYouTestCommand,
    PrivateChatSchedulerEventHandler,
    PrivateChatSilenceEventHandler,
    PrivateChatStopEventHandler,
)
from .prompt import MaiOnlyYouPromptMixin
from .state import MaiOnlyYouStateMixin

logger = get_logger("mai_only_you")

@register_plugin
class MaiOnlyYouPlugin(MaiOnlyYouStateMixin, MaiOnlyYouPromptMixin, BasePlugin):
    """Mai_Only_You plugin."""

    plugin_name: str = "mai_only_you"
    enable_plugin: bool = False
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "filtering": "私聊过滤配置",
        "schedule": "定时扫描配置",
        "silence_detection": "静默触发配置",
        "quiet_hours": "禁止时段配置",
        "limits": "频率与未回复策略",
        "context": "上下文配置",
        "memory": "记忆检索配置",
        "state": "状态持久化配置",
    }

    config_schema: dict = {
        "plugin": {
            "config_version": ConfigField(type=str, default="0.2.5", description="配置文件版本"),
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
        },
        "filtering": {
            "mode": ConfigField(
                type=str,
                default="blocklist",
                description="名单模式：blocklist=黑名单，allowlist=白名单",
                choices=["blocklist", "allowlist"],
            ),
            "users": ConfigField(
                type=list,
                default=[],
                description="名单用户列表（QQ）",
                example='["123456789", "987654321"]',
            ),
        },
        "schedule": {
            "enable_schedule": ConfigField(type=bool, default=True, description="是否启用定时扫描"),
            "scan_interval_minutes": ConfigField(type=int, default=30, description="定时扫描间隔（分钟）"),
        },
        "silence_detection": {
            "enable_silence_detection": ConfigField(type=bool, default=True, description="是否启用静默检测"),
            "silence_threshold_minutes": ConfigField(type=int, default=120, description="静默阈值（分钟）"),
        },
        "quiet_hours": {
            "quiet_hours_start": ConfigField(
                type=str,
                default="01:00",
                description="禁止时段开始（24小时制，HH:MM）",
                example="01:00",
                pattern=r"^([01]\\d|2[0-3]):[0-5]\\d$",
            ),
            "quiet_hours_end": ConfigField(
                type=str,
                default="06:00",
                description="禁止时段结束（24小时制，HH:MM）",
                example="06:00",
                pattern=r"^([01]\\d|2[0-3]):[0-5]\\d$",
            ),
        },
        "limits": {
            "min_interval_hours": ConfigField(type=int, default=6, description="最小主动间隔（小时）"),
            "daily_max": ConfigField(type=int, default=1, description="每日最多主动次数（单用户）"),
            "require_reply_before_next": ConfigField(type=bool, default=True, description="未回复当天不再触发"),
        },
        "context": {
            "history_messages": ConfigField(type=int, default=18, description="上下文回溯条数"),
        },
        "memory": {
            "enable_memory": ConfigField(type=bool, default=True, description="是否启用记忆检索"),
            "question_template": ConfigField(
                type=str,
                default="和{user_name}最近聊过什么？有哪些未完成的话题？",
                description="记忆检索问题模板（支持 {user_name}/{user_id}）",
                example="和{user_name}最近聊过什么？有哪些未完成的话题？",
                placeholder="和{user_name}最近聊过什么？",
            ),
        },
        "state": {
            "retention_days": ConfigField(
                type=int,
                default=30,
                description="状态保留天数（按时间清理）",
                example=30,
            ),
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_user_message_ts: Dict[str, float] = {}
        self._last_proactive_ts: Dict[str, float] = {}
        self._daily_count: Dict[str, Dict[str, Any]] = {}
        self._recent_sent: Dict[str, List[Dict[str, Any]]] = {}
        self._last_schedule_ts: float = 0.0
        self._state_dirty: bool = False
        self._state_save_task: Optional[asyncio.Task] = None
        self._state_save_lock: asyncio.Lock = asyncio.Lock()
        self._load_state()

    def get_plugin_components(self) -> List[Tuple[Any, Type]]:
        if not self.get_config("plugin.enabled", False):
            return []
        return [
            (PrivateChatSchedulerEventHandler.get_handler_info(), PrivateChatSchedulerEventHandler),
            (PrivateChatSilenceEventHandler.get_handler_info(), PrivateChatSilenceEventHandler),
            (PrivateChatStopEventHandler.get_handler_info(), PrivateChatStopEventHandler),
            (MaiOnlyYouTestCommand.get_command_info(), MaiOnlyYouTestCommand),
        ]

    def _is_user_allowed(self, user_id: str) -> bool:
        mode = self.get_config("filtering.mode", "blocklist")
        users = self.get_config("filtering.users", [])
        user_set = {str(item) for item in (users or []) if item}
        if not user_set:
            return True
        mode_value = str(mode or "blocklist").strip().lower()
        if mode_value in {"allowlist", "whitelist"}:
            return user_id in user_set
        return user_id not in user_set

    def _parse_time_to_minutes(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            hour = int(value)
            if 0 <= hour <= 23:
                return hour * 60
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            hour = int(text)
            if 0 <= hour <= 23:
                return hour * 60
            return None
        if ":" not in text:
            return None
        parts = text.split(":", 1)
        if len(parts) != 2:
            return None
        hour_text, minute_text = parts[0].strip(), parts[1].strip()
        if not (hour_text.isdigit() and minute_text.isdigit()):
            return None
        hour = int(hour_text)
        minute = int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    def _is_quiet_hours(self, current_time: datetime) -> bool:
        start_value = self.get_config("quiet_hours.quiet_hours_start", "01:00")
        end_value = self.get_config("quiet_hours.quiet_hours_end", "06:00")
        start_minutes = self._parse_time_to_minutes(start_value)
        end_minutes = self._parse_time_to_minutes(end_value)
        if start_minutes is None or end_minutes is None:
            return False
        current_minutes = current_time.hour * 60 + current_time.minute
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes <= end_minutes
        return current_minutes >= start_minutes or current_minutes <= end_minutes

    def _update_last_user_message(self, stream_id: str, ts: float) -> None:
        self._last_user_message_ts[stream_id] = ts
        self._save_state()

    def _get_last_user_message_ts(self, stream_id: str) -> Optional[float]:
        cached = self._last_user_message_ts.get(stream_id)
        if cached:
            return cached
        try:
            messages = message_api.get_messages_before_time_in_chat(
                chat_id=stream_id,
                timestamp=time.time(),
                limit=1,
                filter_mai=True,
            )
            if not messages:
                return None
            latest_ts = max((msg.time for msg in messages if msg.time), default=None)
            if latest_ts:
                self._last_user_message_ts[stream_id] = latest_ts
                self._save_state()
            return latest_ts
        except Exception as exc:
            logger.error(f"获取最近私聊消息失败: {exc}")
            return None

    def _get_last_user_message(self, stream_id: str) -> Optional[DatabaseMessages]:
        try:
            messages = message_api.get_messages_before_time_in_chat(
                chat_id=stream_id,
                timestamp=time.time(),
                limit=20,
                filter_mai=True,
            )
            if not messages:
                return None
            latest = max(messages, key=lambda msg: msg.time or 0.0)
            if latest.time:
                self._last_user_message_ts[stream_id] = latest.time
                self._save_state()
            return latest
        except Exception as exc:
            logger.error(f"获取最后一条私聊消息失败: {exc}")
            return None

    def _require_reply_before_next(self, stream_id: str, last_user_ts: float) -> bool:
        require_reply = self.get_config("limits.require_reply_before_next", True)
        if not require_reply:
            return False
        last_proactive_ts = self._last_proactive_ts.get(stream_id, 0.0)
        if last_proactive_ts <= 0:
            return False
        if last_proactive_ts < last_user_ts:
            return False
        last_proactive_date = datetime.fromtimestamp(last_proactive_ts).date()
        return last_proactive_date == datetime.now().date()

    def _reset_daily_count_if_needed(self, stream_id: str) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        record = self._daily_count.get(stream_id)
        if not record or record.get("date") != today:
            self._daily_count[stream_id] = {"date": today, "count": 0}

    def _get_daily_count(self, stream_id: str) -> int:
        self._reset_daily_count_if_needed(stream_id)
        return int(self._daily_count.get(stream_id, {}).get("count", 0))

    def _increment_daily_count(self, stream_id: str) -> None:
        self._reset_daily_count_if_needed(stream_id)
        self._daily_count[stream_id]["count"] += 1
        self._save_state()

    def _normalize_text(self, text: str) -> str:
        normalized = (text or "").strip().lower()
        for ch in [" ", "\t", "\n", "-", "_", ",", ".", "!", "?", ":", "；", "，", "。", "！", "？", "：", "·", "—", "~"]:
            normalized = normalized.replace(ch, "")
        return normalized

    def _is_recent_duplicate(self, stream_id: str, content: str) -> bool:
        items = self._recent_sent.get(stream_id, [])
        if not items:
            return False
        normalized = self._normalize_text(content)
        for item in items:
            if self._normalize_text(item.get("content", "")) == normalized:
                return True
        return False

    def _record_recent_sent(self, stream_id: str, content: str) -> None:
        items = self._recent_sent.get(stream_id, [])
        items.append({"content": content, "ts": time.time()})
        if len(items) > 20:
            items = items[-20:]
        self._recent_sent[stream_id] = items
        self._save_state()

    def _should_trigger_for_stream(self, stream_id: str, user_id: str, now_ts: float) -> bool:
        if not self.get_config("plugin.enabled", False):
            return False
        if not self.get_config("silence_detection.enable_silence_detection", True):
            return False
        if not self._is_user_allowed(user_id):
            return False
        if self._is_quiet_hours(datetime.now()):
            return False

        last_user_ts = self._get_last_user_message_ts(stream_id)
        if not last_user_ts:
            return False
        silence_minutes = int(self.get_config("silence_detection.silence_threshold_minutes", 120))
        if now_ts - last_user_ts < silence_minutes * 60:
            return False
        if self._require_reply_before_next(stream_id, last_user_ts):
            return False
        min_interval = int(self.get_config("limits.min_interval_hours", 6)) * 3600
        last_proactive_ts = self._last_proactive_ts.get(stream_id)
        if last_proactive_ts and now_ts - last_proactive_ts < min_interval:
            return False
        daily_max = int(self.get_config("limits.daily_max", 1))
        if daily_max > 0 and self._get_daily_count(stream_id) >= daily_max:
            return False
        return True

    async def _scan_private_chats(self) -> None:
        if not self.get_config("schedule.enable_schedule", True):
            return

        scan_interval = int(self.get_config("schedule.scan_interval_minutes", 30)) * 60
        now_ts = time.time()
        if now_ts - self._last_schedule_ts < scan_interval:
            return
        self._last_schedule_ts = now_ts

        streams = chat_api.get_private_streams("qq")
        for stream in streams:
            if stream.platform != "qq" or stream.group_info:
                continue
            if not stream.user_info:
                continue
            user_id = str(stream.user_info.user_id or "")
            if not user_id:
                continue
            if not self._should_trigger_for_stream(stream.stream_id, user_id, now_ts):
                continue
            await self._handle_silence_trigger(stream.stream_id, user_id, reason="定时扫描")

    async def _handle_silence_trigger(self, stream_id: str, user_id: str, reason: str) -> None:
        logger.info(f"触发私聊主动聊天候选: stream={stream_id}, user={user_id}, reason={reason}")
        chat_stream = get_chat_manager().get_stream(stream_id)
        if not chat_stream:
            logger.error(f"未找到聊天流: {stream_id}")
            return

        last_user_msg = self._get_last_user_message(stream_id)
        if not last_user_msg:
            logger.info(f"未找到可用的私聊历史消息，跳过: {stream_id}")
            return

        history_messages = self.get_config("context.history_messages", 0)
        try:
            history_messages = int(history_messages)
        except (TypeError, ValueError):
            history_messages = 0
        context_limit = history_messages if history_messages > 0 else None
        prompt, selected_expressions = await self._build_proactive_prompt(
            chat_stream,
            last_user_msg,
            reason,
            user_id,
            stream_id,
            context_limit,
        )
        if not prompt:
            logger.warning(f"主动消息提示词构建失败: stream={stream_id}")
            return

        success, content, reasoning, model_name = await llm_api.generate_with_model(
            prompt,
            model_config.model_task_config.replyer,
            request_type="mai_only_you",
        )
        content = (content or "").strip()
        if success and content:
            logger.info(f"使用 {model_name} 生成主动回复内容: {content}")
            if global_config.debug.show_replyer_reasoning and reasoning:
                logger.info(f"使用 {model_name} 生成主动回复推理:\n{reasoning}")
        if not success or not content:
            logger.warning(f"主动消息生成失败或为空: stream={stream_id}")
            return
        if self._is_recent_duplicate(stream_id, content):
            logger.info(f"检测到重复内容，跳过发送: stream={stream_id}")
            return

        sent = await send_api.text_to_stream(
            text=content,
            stream_id=stream_id,
            typing=False,
            storage_message=True,
            selected_expressions=selected_expressions or None,
        )
        if sent:
            self._last_proactive_ts[stream_id] = time.time()
            self._increment_daily_count(stream_id)
            self._record_recent_sent(stream_id, content)
            logger.info(f"私聊主动消息发送成功: stream={stream_id}")
