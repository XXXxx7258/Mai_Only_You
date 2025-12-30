"""
Mai_Only_You prompt helpers.
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import List, Optional, Tuple

from src.chat.replyer.private_generator import PrivateReplyer
from src.chat.utils.chat_message_builder import build_readable_messages
from src.common.data_models.database_data_model import DatabaseMessages
from src.config.config import global_config
from src.memory_system.memory_retrieval import build_memory_retrieval_prompt
from src.person_info.person_info import Person
from src.plugin_system import get_logger
from src.plugin_system.apis import message_api

logger = get_logger("mai_only_you")
SHORT_CONTEXT_RATIO = 0.33


class MaiOnlyYouPromptMixin:
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

        short_limit = max(1, int(context_size * SHORT_CONTEXT_RATIO))
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
