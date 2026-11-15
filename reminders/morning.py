"""早安播报的数据装配：天气、未来一天日程（本地 + Apple）、Notion 待办。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import notices


class MorningBriefing:
    def __init__(self, store, weather, notion, apple_calendar, tz: ZoneInfo):
        self.store = store
        self.weather = weather
        self.notion = notion
        self.apple = apple_calendar
        self.tz = tz

    async def weather_text(self) -> tuple[str, str]:
        if not self.weather:
            return "", ""
        try:
            return await self.weather.fetch()
        except Exception:
            return "", ""

    async def _local_entries(
        self, user_id: str, now: datetime
    ) -> list[tuple[datetime, str, str]]:
        entries: list[tuple[datetime, str, str]] = []
        now_naive = now.replace(tzinfo=None)
        for item in await self.store.list_items(user_id):
            start = notices.parse_item_time(item.time, self.tz)
            if start is None:
                continue
            all_day = notices.is_all_day(item)
            if all_day:
                if start.date() != now.date():
                    continue
            elif not (
                now_naive - timedelta(minutes=1)
                <= start
                <= now_naive + timedelta(hours=24)
            ):
                continue
            entries.append(
                (
                    start,
                    notices.time_label(start, item.end_time, all_day, self.tz),
                    item.title,
                )
            )
        return entries

    async def _apple_entries(self, now: datetime) -> list[tuple[datetime, str, str]]:
        if not self.apple:
            return []
        try:
            events, _issues = await self.apple.get_all_events(days=2)
        except Exception:
            return []
        entries: list[tuple[datetime, str, str]] = []
        now_naive = now.replace(tzinfo=None)
        for event in events:
            start = notices.parse_item_time(event.get("start", ""), self.tz)
            if start is None:
                continue
            all_day = bool(event.get("all_day"))
            if all_day:
                if start.date() != now.date():
                    continue
            elif not (
                now_naive - timedelta(minutes=1)
                <= start
                <= now_naive + timedelta(hours=24)
            ):
                continue
            label = notices.time_label(start, event.get("end"), all_day, self.tz)
            entries.append((start, label, event.get("summary", "无标题")))
        return entries

    async def notion_text(self) -> str | None:
        """未配置返回 None（不出现待办行）；失败返回「获取失败」；空返回「暂无」。"""
        if not self.notion:
            return None
        try:
            pending = await self.notion.get_pending_transactions()
        except Exception:
            return "获取失败"
        if pending is None:
            return "获取失败"
        if not pending:
            return "暂无"
        lines = []
        for task in pending[:5]:
            ddl = notices.format_ddl(task.get("ddl", ""), self.tz)
            title = task.get("title", "(无标题)")
            lines.append(f"- {ddl} | {title}" if ddl else f"- {title}")
        return "\n".join(lines)

    async def build(self, user_id: str) -> dict:
        now = datetime.now(self.tz)
        local = await self._local_entries(user_id, now)
        apple = await self._apple_entries(now)
        merged: list[tuple[datetime, str, str]] = []
        seen = set()
        for start, label, title in sorted(local + apple, key=lambda entry: entry[0]):
            key = (label, title)
            if key in seen:
                continue
            seen.add(key)
            merged.append((start, label, title))
        agenda = notices.format_agenda([(label, title) for _, label, title in merged])
        weather_current, weather_forecast = await self.weather_text()
        return {
            "date": now.strftime("%Y-%m-%d"),
            "weekday": notices.weekday_label(now),
            "weather_current": weather_current or "暂无",
            "weather_forecast": weather_forecast,
            "agenda": agenda,
            "notion_todos": await self.notion_text(),
        }
