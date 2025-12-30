"""
Mai_Only_You plugin components.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from src.chat.utils.utils import is_bot_self
from src.manager.async_task_manager import AsyncTask, async_task_manager
from src.plugin_system import (
    BaseCommand,
    BaseEventHandler,
    CustomEventHandlerResult,
    EventType,
    MaiMessages,
    get_logger,
)
from src.plugin_system.apis import chat_api

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
                logger.error("无法获取 Mai_Only_You 插件实例")
                return False, True, None, None, None

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


class PrivateChatStopEventHandler(BaseEventHandler):
    """停止时刷新状态"""

    event_type = EventType.ON_STOP
    handler_name = "mai_only_you_stop"
    handler_description = "停止时刷新插件状态"
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

            await self.plugin_instance._flush_state_on_shutdown()
            return True, True, None, None, None
        except Exception as exc:
            logger.error(f"停止时刷新状态失败: {exc}")
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
                return False, "插件实例获取失败", 1

            if not self.get_config("plugin.enabled", False):
                await self.send_text("❌ 插件未启用")
                return False, "插件未启用", 1

            user_id = (self.matched_groups.get("user_id") or "").strip()
            if user_id:
                stream = chat_api.get_stream_by_user_id(user_id)
                if not stream:
                    await self.send_text("❌ 未找到指定用户的私聊")
                    return False, "未找到私聊", 1
            else:
                stream = self.message.chat_stream
                if not stream or stream.group_info:
                    await self.send_text("❌ 请在私聊中使用或指定 user_id")
                    return False, "非私聊", 1

            if stream.platform != "qq":
                await self.send_text("❌ 仅支持 QQ 私聊")
                return False, "非 QQ 私聊", 1

            stream_id = stream.stream_id
            fallback_user_id = user_id if user_id else ""
            user_info = getattr(stream, "user_info", None)
            target_user_id = str(getattr(user_info, "user_id", None) or fallback_user_id)

            if target_user_id and not plugin_instance._is_user_allowed(target_user_id):
                await self.send_text("❌ 目标用户被名单过滤")
                return False, "用户被过滤", 1

            await plugin_instance._handle_silence_trigger(stream_id, target_user_id, reason="调试命令")
            return True, "调试触发完成", 1

        except Exception as exc:
            logger.error(f"调试触发失败: {exc}")
            await self.send_text("❌ 调试触发失败")
            return False, "调试触发失败", 1
