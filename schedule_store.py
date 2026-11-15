"""日程存储：data_dir/schedule_data.json，按键 `schedules_<用户裸ID>` 存列表。

只保留日程本身；习惯/喝水时间戳等旧键不再读写（旧文件里的数据原样保留）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .constants import LOG_PREFIX

_CACHE_TTL_SECONDS = 30


@dataclass
class ScheduleItem:
    id: str = ""
    title: str = ""
    time: str = ""
    end_time: str | None = None
    context: str = ""
    apple_uid: str | None = None
    all_day: bool = False
    last_triggered: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:8]

    @classmethod
    def create(
        cls,
        *,
        title: str,
        time: str,
        end_time: str | None = None,
        context: str = "",
        apple_uid: str | None = None,
        all_day: bool = False,
    ) -> "ScheduleItem":
        return cls(
            title=title,
            time=time,
            end_time=end_time,
            context=context,
            apple_uid=apple_uid,
            all_day=all_day,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleItem":
        allowed = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScheduleStore:
    def __init__(self, logger=None) -> None:
        self.logger = logger
        self._data_dir: Path | None = None
        self._lock = asyncio.Lock()
        self._cache: dict[str, Any] | None = None
        self._cache_ts = 0.0

    def set_data_dir(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _db_path(self) -> Path:
        if self._data_dir is None:
            raise RuntimeError("ScheduleStore 未注入数据目录")
        return self._data_dir / "schedule_data.json"

    def _read_all(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache_ts < _CACHE_TTL_SECONDS:
            return self._cache
        try:
            data = json.loads(self._db_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (json.JSONDecodeError, OSError) as exc:
            self.logger.warning(f"{LOG_PREFIX} 日程文件读取失败，按空数据处理: {exc}")
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._cache = data
        self._cache_ts = now
        return data

    def _write_all(self, data: dict[str, Any]) -> None:
        try:
            tmp = self._db_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._db_path)
            self._cache = data
            self._cache_ts = time.monotonic()
        except OSError as exc:
            self.logger.error(f"{LOG_PREFIX} 日程文件写入失败: {exc}")

    @staticmethod
    def _key(user_id: str) -> str:
        return f"schedules_{user_id}"

    async def migrate_user(self, old_user_id: str, new_user_id: str) -> bool:
        """把旧裸 ID 键下的日程复制到完整目标键；旧键保留不删。"""
        if not old_user_id or not new_user_id or old_user_id == new_user_id:
            return False
        async with self._lock:
            data = self._read_all()
            old_key = self._key(old_user_id)
            new_key = self._key(new_user_id)
            old_items = data.get(old_key)
            if not isinstance(old_items, list) or not old_items:
                return False
            if data.get(new_key):
                return False
            data[new_key] = list(old_items)
            self._write_all(data)
        self.logger.info(f"{LOG_PREFIX} 日程键迁移 {old_key} → {new_key}")
        return True

    def _raw_items(self, user_id: str) -> list[dict]:
        raw = self._read_all().get(self._key(user_id), [])
        return raw if isinstance(raw, list) else []

    async def list_items(self, user_id: str) -> list[ScheduleItem]:
        return [ScheduleItem.from_dict(item) for item in self._raw_items(user_id)]

    async def get_item(self, user_id: str, item_id: str) -> ScheduleItem | None:
        return next(
            (item for item in await self.list_items(user_id) if item.id == item_id),
            None,
        )

    async def add_item(self, user_id: str, item: ScheduleItem) -> None:
        async with self._lock:
            data = self._read_all()
            items = list(data.get(self._key(user_id), []))
            if any(existing.get("id") == item.id for existing in items):
                item.id = uuid.uuid4().hex[:8]
            items.append(item.to_dict())
            data[self._key(user_id)] = items
            self._write_all(data)

    async def update_item(self, user_id: str, item: ScheduleItem) -> bool:
        async with self._lock:
            data = self._read_all()
            items = list(data.get(self._key(user_id), []))
            for index, existing in enumerate(items):
                if existing.get("id") == item.id:
                    items[index] = item.to_dict()
                    data[self._key(user_id)] = items
                    self._write_all(data)
                    return True
            return False

    async def remove_item(self, user_id: str, item_id: str) -> bool:
        async with self._lock:
            data = self._read_all()
            items = list(data.get(self._key(user_id), []))
            kept = [item for item in items if item.get("id") != item_id]
            if len(kept) == len(items):
                return False
            data[self._key(user_id)] = kept
            self._write_all(data)
            return True

    @staticmethod
    def _event_time(value: str, tz: ZoneInfo) -> tuple[str, datetime] | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(tz)
        return parsed.strftime("%Y-%m-%d %H:%M"), parsed.replace(tzinfo=None)

    async def sync_from_apple_calendar(
        self,
        user_id: str,
        apple_events: list[dict],
        *,
        allow_delete: bool,
        now: datetime,
    ) -> dict[str, int]:
        """把 Apple 事件并入本地日程；空事件列表直接跳过，避免误删。"""
        stats = {"added": 0, "updated": 0, "deleted": 0}
        if not apple_events:
            return stats
        tz = now.tzinfo or ZoneInfo("Asia/Shanghai")
        now_naive = now.replace(tzinfo=None)
        async with self._lock:
            data = self._read_all()
            items = list(data.get(self._key(user_id), []))
            by_uid = {
                item["apple_uid"]: item for item in items if item.get("apple_uid")
            }
            seen_uids: set[str] = set()
            apple_uids: set[str] = set()
            for event in apple_events:
                uid = event.get("uid")
                if not uid:
                    continue
                apple_uids.add(uid)
                if uid in seen_uids:
                    continue
                seen_uids.add(uid)
                start = self._event_time(event.get("start", ""), tz)
                if start is None:
                    continue
                start_text, start_dt = start
                if start_dt < now_naive - timedelta(days=1):
                    apple_uids.discard(uid)
                    continue
                if start_dt > now_naive + timedelta(days=7):
                    apple_uids.discard(uid)
                    continue
                all_day = bool(event.get("all_day"))
                time_text = start_dt.strftime("%Y-%m-%d") if all_day else start_text
                end_text = None
                if not all_day and event.get("end"):
                    end = self._event_time(event.get("end"), tz)
                    if end is not None:
                        end_text = end[0]
                title = event.get("summary", "无标题")
                context = event.get("description", "") or ""
                existing = by_uid.get(uid)
                if existing is None:
                    item = ScheduleItem.create(
                        title=title,
                        time=time_text,
                        end_time=end_text,
                        context=context,
                        apple_uid=uid,
                        all_day=all_day,
                    )
                    items.append(item.to_dict())
                    stats["added"] += 1
                    continue
                if (
                    existing.get("title") != title
                    or existing.get("time") != time_text
                    or existing.get("end_time") != end_text
                    or existing.get("context") != context
                    or bool(existing.get("all_day")) != all_day
                ):
                    existing["title"] = title
                    existing["time"] = time_text
                    existing["end_time"] = end_text
                    existing["context"] = context
                    existing["all_day"] = all_day
                    existing["last_triggered"] = None
                    stats["updated"] += 1
            if allow_delete:
                window_lo = now_naive - timedelta(days=1)
                window_hi = now_naive + timedelta(days=7)

                def _within_window(item: dict) -> bool:
                    parsed = self._event_time(item.get("time", ""), tz)
                    return parsed is not None and window_lo <= parsed[1] <= window_hi

                kept = [
                    item
                    for item in items
                    if not (
                        item.get("apple_uid")
                        and _within_window(item)
                        and item["apple_uid"] not in apple_uids
                    )
                ]
                stats["deleted"] = len(items) - len(kept)
                items = kept
            if stats["added"] or stats["updated"] or stats["deleted"]:
                data[self._key(user_id)] = items
                self._write_all(data)
        return stats
