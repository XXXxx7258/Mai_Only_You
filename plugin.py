"""
Mai_Only_You plugin (QQ private proactive chat).
"""

from __future__ import annotations

import time
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from src.chat.replyer.private_generator import PrivateReplyer
from src.chat.utils.chat_message_builder import build_readable_messages
from src.chat.utils.utils import is_bot_self
from src.chat.message_receive.chat_stream import get_chat_manager
from src.common.data_models.database_data_model import DatabaseMessages
from src.config.config import global_config, model_config
from src.manager.async_task_manager import AsyncTask, async_task_manager
from src.memory_system.memory_retrieval import build_memory_retrieval_prompt
from src.person_info.person_info import Person
from src.plugin_system import (
    BaseCommand,
    BaseEventHandler,
    BasePlugin,
    ConfigField,
    CustomEventHandlerResult,
    EventType,
    MaiMessages,
    register_plugin,
    get_logger,
)
from src.plugin_system.apis import chat_api, message_api, send_api, llm_api

logger = get_logger("mai_only_you")


class PrivateChatSchedulerTask(AsyncTask):
    """定时扫描私聊静默状态"""

    def __init__(self, plugin_instance: "MaiOnlyYouPlugin"):
        super().__init__(
            task_name="mai_only_you_scheduler",
            wait_before_start=60,
            run_interval=60,
        )
        self.plugin = plugin_instance

    async def run(self):
        try:
            await self.plugin._scan_private_chats()
        except Exception as exc:
            logger.error(f"私聊定时扫描失败: {exc}")


class PrivateChatSchedulerEventHandler(BaseEventHandler):
    """启动定时扫描任务"""

    event_type = EventType.ON_START
    handler_name = "mai_only_you_scheduler"
    handler_description = "启动私聊主动聊天调度"
    weight = 50
    intercept_message = False

    def __init__(self):
        super().__init__()
        self.plugin_instance: Optional["MaiOnlyYouPlugin"] = None

    async def execute(
        self, message: MaiMessages | None
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[MaiMessages]]:
        try:
            from src.plugin_system.core.plugin_manager import plugin_manager

            self.plugin_instance = plugin_manager.get_plugin_instance("mai_only_you")
            if not self.plugin_instance:
                logger.error("无法获取 Mai_Only_You 插件实例")
                return False, True, None, None, None

            if not self.get_config("plugin.enabled", False):
                logger.info("Mai_Only_You 未启用，跳过调度任务")
                return True, True, None, None, None

            task = PrivateChatSchedulerTask(self.plugin_instance)
            await async_task_manager.add_task(task)

            logger.info("Mai_Only_You 私聊调度任务已启动")
            return True, True, None, None, None

        except Exception as exc:
            logger.error(f"启动私聊调度任务失败: {exc}")
            return False, True, None, None, None


class PrivateChatSilenceEventHandler(BaseEventHandler):
    """私聊消息更新处理器"""

    event_type = EventType.ON_MESSAGE
    handler_name = "mai_only_you_silence"
    handler_description = "更新私聊最后互动时间"
    weight = 10
    intercept_message = False

    def __init__(self):
        super().__init__()
        self.plugin_instance: Optional["MaiOnlyYouPlugin"] = None

    async def execute(
        self, message: MaiMessages | None
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[MaiMessages]]:
        try:
            if not message or not message.is_private_message:
                return True, True, None, None, None

            platform = message.message_base_info.get("platform")
            if platform != "qq":
                return True, True, None, None, None

            user_id = str(message.message_base_info.get("user_id") or "")
            if not user_id:
                return True, True, None, None, None

            if is_bot_self(platform, user_id):
                return True, True, None, None, None

            from src.plugin_system.core.plugin_manager import plugin_manager

            self.plugin_instance = plugin_manager.get_plugin_instance("mai_only_you")
            if not self.plugin_instance:
                return True, True, None, None, None

            if not self.get_config("plugin.enabled", False):
                return True, True, None, None, None

            if not self.plugin_instance._is_user_allowed(user_id):
                return True, True, None, None, None

            stream_id = message.stream_id
            if not stream_id:
                return True, True, None, None, None

            self.plugin_instance._update_last_user_message(stream_id, time.time())
            return True, True, None, None, None

        except Exception as exc:
            logger.error(f"私聊消息处理失败: {exc}")
            return True, True, None, None, None


class MaiOnlyYouTestCommand(BaseCommand):
    """手动触发私聊主动聊天"""

    command_name = "mai_only_you_test"
    command_description = "手动触发私聊主动聊天"
    command_pattern = r"^/mai_only_you_test(?:\s+(?P<user_id>\d+))?$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        try:
            from src.plugin_system.core.plugin_manager import plugin_manager

            plugin_instance = plugin_manager.get_plugin_instance("mai_only_you")
            if not plugin_instance:
                await self.send_text("❌ 无法获取 Mai_Only_You 插件实例")
                return False, "插件实例获取失败", True

            if not self.get_config("plugin.enabled", False):
                await self.send_text("❌ 插件未启用")
                return False, "插件未启用", True

            user_id = (self.matched_groups.get("user_id") or "").strip()
            stream_id = None
            target_user_id = ""
            if user_id:
                stream = chat_api.get_stream_by_user_id(user_id)
                if not stream:
                    await self.send_text("❌ 未找到指定用户的私聊")
                    return False, "未找到私聊", True
                if stream.platform != "qq":
                    await self.send_text("❌ 仅支持 QQ 私聊")
                    return False, "非 QQ 私聊", True
                stream_id = stream.stream_id
                target_user_id = str(stream.user_info.user_id or user_id)
            else:
                chat_stream = self.message.chat_stream
                if not chat_stream or chat_stream.group_info:
                    await self.send_text("❌ 请在私聊中使用或指定 user_id")
                    return False, "非私聊", True
                if chat_stream.platform != "qq":
                    await self.send_text("❌ 仅支持 QQ 私聊")
                    return False, "非 QQ 私聊", True
                stream_id = chat_stream.stream_id
                target_user_id = str(chat_stream.user_info.user_id or "")

            if target_user_id and not plugin_instance._is_user_allowed(target_user_id):
                await self.send_text("❌ 目标用户被名单过滤")
                return False, "用户被过滤", True

            await plugin_instance._handle_silence_trigger(stream_id, target_user_id, reason="调试命令")
            return True, "调试触发完成", True

        except Exception as exc:
            logger.error(f"调试触发失败: {exc}")
            await self.send_text("❌ 调试触发失败")
            return False, "调试触发失败", True


@register_plugin
class MaiOnlyYouPlugin(BasePlugin):
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
    }

    config_schema: dict = {
        "plugin": {
            "config_version": ConfigField(type=str, default="0.2.4", description="配置文件版本"),
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
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_user_message_ts: Dict[str, float] = {}
        self._last_proactive_ts: Dict[str, float] = {}
        self._daily_count: Dict[str, Dict[str, Any]] = {}
        self._recent_sent: Dict[str, List[Dict[str, Any]]] = {}
        self._last_schedule_ts: float = 0.0
        self._load_state()

    def get_plugin_components(self) -> List[Tuple[Any, Type]]:
        if not self.get_config("plugin.enabled", False):
            return []
        return [
            (PrivateChatSchedulerEventHandler.get_handler_info(), PrivateChatSchedulerEventHandler),
            (PrivateChatSilenceEventHandler.get_handler_info(), PrivateChatSilenceEventHandler),
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

    def _get_user_display_name(self, chat_stream, user_id: str) -> str:
        try:
            person = Person(platform=chat_stream.platform, user_id=user_id)
            if getattr(person, "is_known", False) and person.person_name:
                return str(person.person_name)
        except Exception as exc:
            logger.warning(f"获取数据库用户名称失败: {exc}")

        user_info = getattr(chat_stream, "user_info", None)
        nickname = getattr(user_info, "user_nickname", None) if user_info else None
        if nickname:
            return str(nickname)
        return user_id

    def _render_question_template(self, template: str, user_name: str, user_id: str) -> str:
        values = {"user_name": user_name, "user_id": user_id}
        try:
            return template.format_map(values)
        except KeyError as exc:
            logger.warning(f"记忆模板占位符缺失: {exc}")
        except Exception as exc:
            logger.warning(f"记忆模板渲染失败: {exc}")
        return template

    async def _build_proactive_prompt(
        self,
        chat_stream,
        last_user_msg: DatabaseMessages,
        reason: str,
        user_id: str,
        stream_id: str,
        context_limit: Optional[int],
    ) -> Tuple[str, List[int]]:
        replyer = PrivateReplyer(chat_stream, request_type="mai_only_you")
        user_name = self._get_user_display_name(chat_stream, user_id)

        target = (last_user_msg.processed_plain_text or "").strip()
        if target:
            target = replyer._replace_picids_with_descriptions(target)
        else:
            target = "（无内容）"

        context_size = global_config.chat.max_context_size
        if context_limit is not None:
            try:
                context_size = int(context_limit)
            except (TypeError, ValueError):
                context_size = global_config.chat.max_context_size
        if context_size <= 0:
            context_size = global_config.chat.max_context_size

        now_ts = time.time()
        message_list = message_api.get_messages_before_time_in_chat(
            chat_id=stream_id,
            timestamp=now_ts,
            limit=context_size,
            filter_intercept_message_level=1,
        )
        dialogue_prompt = build_readable_messages(
            message_list,
            replace_bot_name=True,
            timestamp_mode="relative",
            read_mark=0.0,
            show_actions=True,
            long_time_notice=True,
        )

        short_limit = max(1, int(context_size * 0.33))
        message_list_short = message_api.get_messages_before_time_in_chat(
            chat_id=stream_id,
            timestamp=now_ts,
            limit=short_limit,
            filter_intercept_message_level=1,
        )
        dialogue_prompt_short = build_readable_messages(
            message_list_short,
            replace_bot_name=True,
            timestamp_mode="relative",
            read_mark=0.0,
            show_actions=True,
        )

        memory_prompt = ""
        if self.get_config("memory.enable_memory", True):
            template = str(self.get_config("memory.question_template", "") or "").strip()
            memory_question = None
            if template:
                rendered = self._render_question_template(template, user_name, user_id).strip()
                if rendered:
                    memory_question = rendered
            memory_prompt = await build_memory_retrieval_prompt(
                dialogue_prompt_short,
                sender=user_name,
                target=target,
                chat_stream=chat_stream,
                think_level=1,
                question=memory_question,
            )

        expression_habits, selected_expressions = await replyer.build_expression_habits(
            dialogue_prompt_short, target, reply_reason=reason
        )
        personality_prompt = await replyer.build_personality_prompt()
        keywords_reaction_prompt = await replyer.build_keywords_reaction_prompt(target)

        reply_style = global_config.personality.reply_style
        multi_styles = getattr(global_config.personality, "multiple_reply_style", None) or []
        multi_prob = getattr(global_config.personality, "multiple_probability", 0.0) or 0.0
        if multi_styles and multi_prob > 0 and random.random() < multi_prob:
            try:
                reply_style = random.choice(list(multi_styles))
            except Exception:
                reply_style = global_config.personality.reply_style

        chat_prompt_content = replyer.get_chat_prompt_for_chat(stream_id)
        chat_prompt_block = f"{chat_prompt_content}\n" if chat_prompt_content else ""

        last_user_ts = float(last_user_msg.time or 0.0)
        last_user_text = (
            datetime.fromtimestamp(last_user_ts).strftime("%Y-%m-%d %H:%M:%S") if last_user_ts > 0 else "未知"
        )
        gap_minutes = int((now_ts - last_user_ts) / 60) if last_user_ts > 0 else None
        gap_text = f"{gap_minutes} 分钟" if gap_minutes is not None else "未知"

        moderation_prompt = (
            "请不要输出违法违规内容，不要输出色情、暴力、政治相关内容，如有敏感内容，请规避。"
            "不要输出多余内容(包括前后缀、冒号、引号、括号、表情包、at 或 @ 等)。"
        )

        prompt_parts = [
            memory_prompt,
            expression_habits,
            f"当前时间：{datetime.fromtimestamp(now_ts).strftime('%Y-%m-%d %H:%M:%S')}",
            f"对方上次私聊：{last_user_text}（距今约 {gap_text}）",
            f"触发原因：{reason}",
            f"对方上次内容：{target}",
            f"你正在和{user_name}进行 QQ 私聊，以下是最近的聊天记录：",
            dialogue_prompt,
            (
                "请明确考虑时间流逝（时段与间隔），结合记忆检索与私聊上下文，"
                "生成一条合适的主动聊天内容。要求：口语化、简短、单话题，不要太有条理。"
            ),
            personality_prompt,
            chat_prompt_block,
            keywords_reaction_prompt,
            reply_style,
            moderation_prompt,
        ]
        prompt = "\n".join(part for part in prompt_parts if part)
        return prompt, selected_expressions

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

    def _get_state_path(self) -> Optional[Path]:
        if not self.plugin_dir:
            return None
        return Path(self.plugin_dir) / "data" / "state.json"

    def _load_state(self) -> None:
        path = self._get_state_path()
        if not path or not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            self._last_user_message_ts = data.get("last_user_message_ts", {}) or {}
            self._last_proactive_ts = data.get("last_proactive_ts", {}) or {}
            self._daily_count = data.get("daily_count", {}) or {}
            self._recent_sent = data.get("recent_sent", {}) or {}
        except Exception as exc:
            logger.error(f"加载状态失败: {exc}")

    def _save_state(self) -> None:
        path = self._get_state_path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "last_user_message_ts": self._last_user_message_ts,
                "last_proactive_ts": self._last_proactive_ts,
                "daily_count": self._daily_count,
                "recent_sent": self._recent_sent,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error(f"保存状态失败: {exc}")
