"""日程数据存储模块（MaiBot 插件版）

提供日程和习惯的数据持久化，基于 MaiBot 插件数据目录（ctx.paths.data_dir）下的
JSON 文件（替代 AstrBot 内置 KV API）。
支持单次日程、定期习惯、喝水记录、临时覆盖等数据管理。
"""

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .constants import (
    HABITS_KEY,
    LOG_PREFIX,
    SCHEDULES_KEY,
    WATER_LAST_KEY,
)
from .messaging import parse_user_target

logger = logging.getLogger(__name__)

__all__ = ["ScheduleItem", "ScheduleStore"]


def _bare_user_id(user_id: str) -> str:
    """把用户标识归一化为裸 ID（存储键统一口径）。

    历史上定时路径用配置原值（qq:123456）做键，而 @Tool 写入路径经
    parse_user_target 取裸 ID，导致同一用户两套键、早安播报读不到
    工具创建的日程。现在所有存取统一取裸 ID，平台前缀在发送侧还原。
    """
    _, bare = parse_user_target(user_id)
    return bare or str(user_id or "").strip()


def _schedules_key(user_id: str) -> str:
    return f"schedules_{user_id}"


def _habits_key(user_id: str) -> str:
    return f"habits_{user_id}"


def _water_key(user_id: str) -> str:
    return f"water_last_{user_id}"


_USERS_KEY = "_users"
_CACHE_TTL_SECONDS = 30.0


@dataclass
class ScheduleItem:
    """日程/习惯数据项"""

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: str = ""
    title: str = ""
    time: str = ""
    recur: str | None = None
    context: str = ""
    enabled: bool = True
    snoozed_until: str | None = None
    last_triggered: str | None = None
    temp_override: str | None = None
    apple_uid: str | None = None
    all_day: bool = False

    def to_dict(self) -> dict:
        """序列化为字典"""
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "ScheduleItem":
        """从字典反序列化，过滤未知字段"""
        valid_fields = {
            "id",
            "type",
            "title",
            "time",
            "recur",
            "context",
            "enabled",
            "snoozed_until",
            "last_triggered",
            "temp_override",
            "apple_uid",
            "all_day",
        }
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        if not filtered.get("id"):
            filtered["id"] = str(uuid.uuid4())[:8]
        return ScheduleItem(**filtered)


class ScheduleStore:
    """日程数据存储器（MaiBot 版：JSON 文件 + 内存缓存）

    底层存储：data_dir/schedule_data.json（插件数据目录，ctx.paths.data_dir），
    整体读写 + 短期内存缓存。插件在 on_load 时调用 set_data_dir 注入目录。
    """

    def __init__(self):
        self._data_dir: Path | None = None
        self._cache: dict[str, tuple[Any, float]] = {}
        self._migrated = False
        logger.info(f"{LOG_PREFIX} ScheduleStore 初始化完成")

    def set_data_dir(self, data_dir: str | Path) -> None:
        """注入数据目录（MaiBot 插件在 on_load 传 self.ctx.paths.data_dir）"""
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _db_path(self) -> Path:
        if self._data_dir is None:
            raise RuntimeError("ScheduleStore 未注入数据目录（set_data_dir 未调用）")
        return self._data_dir / "schedule_data.json"

    async def _load_all(self) -> dict[str, Any]:
        """读取整个 JSON 数据文件（不存在返回空 dict），首次加载时迁移旧键"""
        try:
            raw = self._db_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"{LOG_PREFIX} 数据文件读取失败，按空数据继续: {e}")
            return {}
        if not self._migrated:
            self._migrated = True
            if self._migrate_legacy_keys(data):
                await self._save_all(data)
        return data

    @staticmethod
    def _migrate_legacy_keys(data: dict[str, Any]) -> bool:
        """把旧版 platform:id 形式的存储键迁移为裸 ID 键（原地修改）。

        旧版本定时路径以配置原值（如 qq:123456）做键，与 @Tool 写入的
        裸 ID 键并存；统一到裸 ID 后重命名旧键，避免既有数据失联。

        Returns:
            bool: 数据是否有变更（需要写回）
        """
        changed = False
        for prefix in ("schedules_", "habits_", "water_last_", "user_platform_"):
            for key in [
                k
                for k in data
                if isinstance(k, str)
                and k.startswith(prefix)
                and ":" in k[len(prefix) :]
            ]:
                bare = key[len(prefix) :].split(":", 1)[1].strip()
                if bare:
                    new_key = prefix + bare
                    if new_key not in data:
                        data[new_key] = data[key]
                del data[key]
                changed = True
        users = data.get(_USERS_KEY)
        if isinstance(users, list):
            new_users: list[str] = []
            for u in users:
                bare = _bare_user_id(str(u))
                if bare and bare not in new_users:
                    new_users.append(bare)
            if new_users != [str(u) for u in users]:
                changed = True
            data[_USERS_KEY] = new_users
        return changed

    async def _save_all(self, data: dict[str, Any]) -> None:
        """整体写回 JSON 数据文件"""
        try:
            tmp = self._db_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._db_path)
        except OSError as e:
            logger.error(f"{LOG_PREFIX} 数据文件写入失败: {e}")

    async def _get_kv(self, key: str, default=None):
        """读取 KV（JSON 顶层键），带短期内存缓存"""
        now = time.monotonic()
        if key in self._cache:
            value, ts = self._cache[key]
            if now - ts < _CACHE_TTL_SECONDS:
                return value
        data = await self._load_all()
        value = data.get(key, default)
        self._cache[key] = (value, now)
        return value

    async def _set_kv(self, key: str, value) -> None:
        """写入 KV（JSON 顶层键）并更新缓存"""
        self._cache[key] = (value, time.monotonic())
        data = await self._load_all()
        data[key] = value
        await self._save_all(data)

    async def _get_user_index(self) -> list[str]:
        users = await self._get_kv(_USERS_KEY, [])
        return [str(u) for u in users if u]

    async def _save_user_index(self, users: list[str]) -> None:
        uniq = sorted({str(u) for u in users if u})
        await self._set_kv(_USERS_KEY, uniq)

    async def _touch_user(self, user_id: str) -> None:
        if not user_id:
            return
        users = await self._get_user_index()
        if user_id not in users:
            users.append(user_id)
            await self._save_user_index(users)

    async def _load_user_data(self, user_id: str) -> dict[str, Any]:
        """向后兼容：从旧版统一 data 键迁移数据"""
        user_id = _bare_user_id(user_id)
        data: dict[str, Any] = {
            SCHEDULES_KEY: [],
            HABITS_KEY: [],
            WATER_LAST_KEY: "",
        }
        schedules = await self._get_kv(_schedules_key(user_id), [])
        if schedules:
            data[SCHEDULES_KEY] = schedules
        habits = await self._get_kv(_habits_key(user_id), [])
        if habits:
            data[HABITS_KEY] = habits
        water = await self._get_kv(_water_key(user_id), "")
        if water:
            data[WATER_LAST_KEY] = water
        return data

    async def _save_user_data(self, user_id: str, data: dict[str, Any]) -> None:
        user_id = _bare_user_id(user_id)
        await self._set_kv(_schedules_key(user_id), data.get(SCHEDULES_KEY, []))
        await self._set_kv(_habits_key(user_id), data.get(HABITS_KEY, []))
        await self._set_kv(_water_key(user_id), data.get(WATER_LAST_KEY, ""))
        await self._touch_user(user_id)

    async def add_item(self, user_id: str, item: ScheduleItem) -> None:
        data = await self._load_user_data(user_id)
        item_dict = item.to_dict()
        if item.type == "habit":
            data[HABITS_KEY] = [
                h for h in data[HABITS_KEY] if h.get("title") != item.title
            ]
            data[HABITS_KEY].append(item_dict)
        else:
            data[SCHEDULES_KEY].append(item_dict)
        await self._save_user_data(user_id, data)

    async def list_all_items(self, user_id: str) -> list[ScheduleItem]:
        data = await self._load_user_data(user_id)
        items = []
        for s in data.get(SCHEDULES_KEY, []):
            items.append(ScheduleItem.from_dict(s))
        for h in data.get(HABITS_KEY, []):
            items.append(ScheduleItem.from_dict(h))
        return items

    async def get_schedules(self, user_id: str) -> dict[str, list[ScheduleItem]]:
        data = await self._load_user_data(user_id)
        return {
            SCHEDULES_KEY: [
                ScheduleItem.from_dict(s) for s in data.get(SCHEDULES_KEY, [])
            ],
            HABITS_KEY: [ScheduleItem.from_dict(h) for h in data.get(HABITS_KEY, [])],
        }

    async def get_all_users(self) -> list[str]:
        return sorted(set(await self._get_user_index()))

    async def remove_item(self, user_id: str, item_id: str) -> bool:
        data = await self._load_user_data(user_id)
        before = len(data.get(SCHEDULES_KEY, [])) + len(data.get(HABITS_KEY, []))
        data[SCHEDULES_KEY] = [
            s for s in data.get(SCHEDULES_KEY, []) if s.get("id") != item_id
        ]
        data[HABITS_KEY] = [
            h for h in data.get(HABITS_KEY, []) if h.get("id") != item_id
        ]
        after = len(data.get(SCHEDULES_KEY, [])) + len(data.get(HABITS_KEY, []))
        if before != after:
            await self._save_user_data(user_id, data)
            return True
        return False

    async def update_item(self, user_id: str, item: "ScheduleItem") -> bool:
        data = await self._load_user_data(user_id)
        item_dict = item.to_dict()
        for key in [SCHEDULES_KEY, HABITS_KEY]:
            for i, stored in enumerate(data.get(key, [])):
                if stored.get("id") == item.id:
                    data[key][i] = item_dict
                    await self._save_user_data(user_id, data)
                    return True
        return False

    async def sync_from_apple_calendar(
        self, user_id: str, apple_events: list[dict]
    ) -> dict[str, int]:
        # 防御：API 返回空列表时（可能是失败），不执行删除，避免误删
        if not apple_events:
            logger.debug(f"{LOG_PREFIX} Apple 日历返回空事件列表，跳过同步")
            return {"added": 0, "updated": 0, "deleted": 0}
        data = await self._load_user_data(user_id)
        schedules = data.get(SCHEDULES_KEY, [])
        uid_map = {s["apple_uid"]: s for s in schedules if s.get("apple_uid")}
        apple_uids = set()
        stats = {"added": 0, "updated": 0, "deleted": 0}

        # 只同步未来 7 天内的日程
        now = datetime.now()
        future_cutoff = now + timedelta(days=7)

        # 防止重复添加：记录本次同步中已处理的 UID（解决 Apple 返回重复事件的问题）
        processed_uids_this_sync: set[str] = set()

        for evt in apple_events:
            uid = evt.get("uid")
            if not uid:
                continue

            # 防止同一个 UID 被添加两次（Apple 有时会返回重复的 RRULE 实例）
            if uid in processed_uids_this_sync:
                logger.debug(
                    f"{LOG_PREFIX} 跳过重复 UID: {uid[:16]}... (事件: {evt.get('summary', '无标题')})"
                )
                apple_uids.add(uid)
                continue

            apple_uids.add(uid)
            start_str = evt.get("start", "")
            if not start_str:
                continue
            try:
                start_dt = datetime.fromisoformat(start_str)
                # 过滤：只保留未来 7 天内的日程
                if start_dt < now - timedelta(days=1):  # 1天前的也保留（刚结束的）
                    logger.debug(
                        f"{LOG_PREFIX} 跳过过期日程: {evt.get('summary', '无标题')} ({start_str})"
                    )
                    apple_uids.discard(uid)  # 不保留在 apple_uids 中，允许后续删除
                    continue
                if start_dt > future_cutoff:
                    logger.debug(
                        f"{LOG_PREFIX} 跳过远期日程: {evt.get('summary', '无标题')} ({start_str})"
                    )
                    apple_uids.discard(uid)  # 不保留在 apple_uids 中，允许后续删除
                    continue
                schedule_time = start_dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                schedule_time = start_str
            if uid in uid_map:
                local = uid_map[uid]
                if (
                    local.get("title") != evt.get("summary")
                    or local.get("time") != schedule_time
                ):
                    local["title"] = evt.get("summary", "无标题")
                    local["time"] = schedule_time
                    stats["updated"] += 1
            else:
                schedules.append(
                    ScheduleItem(
                        type="schedule",
                        title=evt.get("summary", "无标题"),
                        time=schedule_time,
                        context=evt.get("description", ""),
                        apple_uid=uid,
                        all_day=evt.get("all_day", False),
                    ).to_dict()
                )
                stats["added"] += 1
                processed_uids_this_sync.add(uid)
        before_count = len(schedules)
        schedules = [
            s
            for s in schedules
            if not s.get("apple_uid") or s["apple_uid"] in apple_uids
        ]
        stats["deleted"] = before_count - len(schedules)
        data[SCHEDULES_KEY] = schedules
        await self._save_user_data(user_id, data)
        return stats

    async def clear_expired_overrides(self, user_id: str) -> None:
        """清理过期的临时覆盖"""
        data = await self._load_user_data(user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        changed = False
        for habit in data.get(HABITS_KEY, []):
            temp = habit.get("temp_override", "")
            if temp and not temp.startswith(today):
                habit.pop("temp_override", None)
                changed = True
        if changed:
            await self._save_user_data(user_id, data)
