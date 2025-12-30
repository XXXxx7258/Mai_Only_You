from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.plugin_system import get_logger

logger = get_logger("mai_only_you")


class MaiOnlyYouStateMixin:
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

    def _get_state_retention_days(self) -> Optional[int]:
        value = self.get_config("state.retention_days", 30)
        try:
            days = int(value)
        except (TypeError, ValueError):
            return 30
        if days <= 0:
            return None
        return days

    def _coerce_timestamp(self, value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return None
        return None

    def _cleanup_state_by_age(self, now_ts: float) -> None:
        retention_days = self._get_state_retention_days()
        if not retention_days:
            return
        cutoff_ts = now_ts - (retention_days * 86400)
        stream_ids = (
            set(self._last_user_message_ts.keys())
            | set(self._last_proactive_ts.keys())
            | set(self._daily_count.keys())
            | set(self._recent_sent.keys())
        )
        for stream_id in list(stream_ids):
            last_ts = 0.0
            user_ts = self._coerce_timestamp(self._last_user_message_ts.get(stream_id))
            if user_ts is not None:
                last_ts = max(last_ts, user_ts)
            proactive_ts = self._coerce_timestamp(self._last_proactive_ts.get(stream_id))
            if proactive_ts is not None:
                last_ts = max(last_ts, proactive_ts)
            recent_items = self._recent_sent.get(stream_id, [])
            if recent_items:
                recent_ts_values = []
                for item in recent_items:
                    if not isinstance(item, dict):
                        continue
                    ts_value = self._coerce_timestamp(item.get("ts"))
                    if ts_value is not None:
                        recent_ts_values.append(ts_value)
                if recent_ts_values:
                    last_ts = max(last_ts, max(recent_ts_values))
            daily_record = self._daily_count.get(stream_id, {})
            date_text = daily_record.get("date") if isinstance(daily_record, dict) else None
            if date_text:
                try:
                    date_ts = datetime.strptime(str(date_text), "%Y-%m-%d").timestamp()
                    last_ts = max(last_ts, date_ts)
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        f"无法解析状态中的日期 '{date_text}' (stream_id: {stream_id}): {exc}"
                    )
            if last_ts < cutoff_ts:
                self._last_user_message_ts.pop(stream_id, None)
                self._last_proactive_ts.pop(stream_id, None)
                self._daily_count.pop(stream_id, None)
                self._recent_sent.pop(stream_id, None)
                continue
            if recent_items:
                kept_items = []
                for item in recent_items:
                    if not isinstance(item, dict):
                        continue
                    ts_value = self._coerce_timestamp(item.get("ts"))
                    if ts_value is not None and ts_value >= cutoff_ts:
                        kept_items.append(item)
                if kept_items:
                    self._recent_sent[stream_id] = kept_items
                else:
                    self._recent_sent.pop(stream_id, None)

    def _build_state_snapshot(self) -> Dict[str, Any]:
        return {
            "last_user_message_ts": dict(self._last_user_message_ts),
            "last_proactive_ts": dict(self._last_proactive_ts),
            "daily_count": copy.deepcopy(self._daily_count),
            "recent_sent": copy.deepcopy(self._recent_sent),
        }

    def _write_state_file(self, data: Dict[str, Any]) -> None:
        path = self._get_state_path()
        if not path:
            return
        tmp_path: Optional[Path] = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except Exception as exc:
            logger.error(f"保存状态失败: {exc}")
            try:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink()
            except Exception as cleanup_exc:
                logger.warning(f"清理临时状态文件失败: {cleanup_exc}")

    async def _flush_state_async(self) -> None:
        try:
            if self._state_save_lock is None:
                self._state_save_lock = asyncio.Lock()
            async with self._state_save_lock:
                while self._state_dirty:
                    self._state_dirty = False
                    self._cleanup_state_by_age(time.time())
                    data = self._build_state_snapshot()
                    await asyncio.to_thread(self._write_state_file, data)
        except Exception as exc:
            logger.error(f"异步保存状态失败: {exc}")
        finally:
            self._state_save_task = None

    async def _flush_state_on_shutdown(self) -> None:
        if self._state_save_task and not self._state_save_task.done():
            try:
                await self._state_save_task
            except Exception as exc:
                logger.error(f"关闭时等待状态保存失败: {exc}")
            return
        if self._state_dirty:
            await self._flush_state_async()

    def _save_state(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._cleanup_state_by_age(time.time())
            self._write_state_file(self._build_state_snapshot())
            return
        self._state_dirty = True
        if self._state_save_task and not self._state_save_task.done():
            return
        self._state_save_task = loop.create_task(self._flush_state_async())
