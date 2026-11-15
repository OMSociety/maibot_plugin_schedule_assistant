"""早安数据装配。"""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from schedule_assistant.reminders.morning import MorningBriefing
from schedule_assistant.schedule_store import ScheduleItem

TZ = ZoneInfo("Asia/Shanghai")


class FakeStore:
    def __init__(self, items):
        self.items = items

    async def list_items(self, user_id):
        return self.items


class FakeWeather:
    def __init__(self, current="晴, 20°C", forecast="白天晴"):
        self.current = current
        self.forecast = forecast

    async def fetch(self):
        return self.current, self.forecast


class FakeNotion:
    def __init__(self, pending=None, error=None):
        self.pending = pending
        self.error = error

    async def get_pending_transactions(self):
        if self.error:
            raise self.error
        return self.pending


class FakeApple:
    def __init__(self, events=None, issues=None):
        self.events = events or []
        self.issues = issues or []

    async def get_all_events(self, days=1):
        return self.events, self.issues


def build(store, notion=None, apple=None):
    briefing = MorningBriefing(store, FakeWeather(), notion, apple, TZ)
    return asyncio.run(briefing.build("u1"))


def test_build_without_optional_services():
    now = datetime.now(TZ)
    start = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    store = FakeStore([ScheduleItem.create(title="组会", time=start)])
    data = build(store)
    assert data["date"] == now.strftime("%Y-%m-%d")
    assert "组会" in data["agenda"]
    assert data["notion_todos"] is None
    assert data["weather_current"] == "晴, 20°C"


def test_notion_failure_is_reported():
    data = build(FakeStore([]), notion=FakeNotion(error=RuntimeError("boom")))
    assert data["notion_todos"] == "获取失败"


def test_notion_empty_is_reported():
    data = build(FakeStore([]), notion=FakeNotion(pending=[]))
    assert data["notion_todos"] == "暂无"


def test_all_day_item_outside_today_is_skipped():
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    store = FakeStore(
        [ScheduleItem.create(title="明天的会", time=tomorrow, all_day=True)]
    )
    data = build(store)
    assert "明天的会" not in data["agenda"]
