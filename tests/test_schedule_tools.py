"""工具的结构化时间、目标识别与 CRUD 行为。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from schedule_assistant.schedule_store import ScheduleItem
from schedule_assistant.tools import schedule_tools as tools

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 10, 20, 0, tzinfo=TZ)
tools._now = lambda plugin: NOW


class FakeStore:
    def __init__(self):
        self.items: list[ScheduleItem] = []

    async def list_items(self, user_id):
        return [item for item in self.items if item.id]

    async def get_item(self, user_id, item_id):
        return next((item for item in self.items if item.id == item_id), None)

    async def add_item(self, user_id, item):
        self.items.append(item)

    async def update_item(self, user_id, item):
        return True

    async def remove_item(self, user_id, item_id):
        before = len(self.items)
        self.items = [item for item in self.items if item.id != item_id]
        return len(self.items) != before


class FakeApple:
    def __init__(self, uid="apple-1", update_ok=True, delete_ok=True):
        self.uid = uid
        self.update_ok = update_ok
        self.delete_ok = delete_ok
        self.calls: list[tuple] = []

    async def create_event(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self.uid

    async def update_event(self, uid, **kwargs):
        self.calls.append(("update", uid, kwargs))
        return self.update_ok

    async def delete_event(self, uid):
        self.calls.append(("delete", uid))
        return self.delete_ok


def make_plugin(store=None, apple=None):
    plugin = SimpleNamespace(
        _timezone=TZ,
        config=SimpleNamespace(basic=SimpleNamespace(user_ids=["qq:10001"])),
        ctx=SimpleNamespace(logger=SimpleNamespace(warning=lambda *a, **k: None)),
        store=store or FakeStore(),
        apple_calendar=apple,
    )

    def fake_now():
        return NOW

    plugin._now = fake_now
    return plugin


def test_parse_start_iso_and_all_day():
    start = tools._parse_start("2026-09-11T09:00", False, NOW)
    assert start == datetime(2026, 9, 11, 9, 0)
    start = tools._parse_start("2026-09-11", True, NOW)
    assert start == datetime(2026, 9, 11, 0, 0)


def test_validate_start_rejects_past_and_year_overflow():
    assert tools._validate_start(datetime(2026, 9, 10, 19, 0), False, NOW)
    assert tools._validate_start(datetime(2026, 9, 10, 23, 0), False, NOW) is None
    assert tools._validate_start(datetime(2367, 1, 1), False, NOW)
    assert tools._validate_start(datetime(2026, 9, 9), True, NOW)


def test_target_from_message_prefers_platform_and_sender():
    plugin = make_plugin()
    message = {"platform": "qq", "user_info": {"user_id": "10001"}}
    assert tools._target_from_message(plugin, message) == "qq:10001"
    assert tools._target_from_message(plugin, None) == "qq:10001"


def run(coro):
    return asyncio.run(coro)


def test_create_schedule_persists_and_rejects_past():
    plugin = make_plugin()
    ok = run(
        tools.create_schedule(
            plugin, "组会", "2026-09-11T09:00", "2026-09-11T10:00", False, "", None
        )
    )
    assert "已创建日程" in ok
    assert len(plugin.store.items) == 1
    past = run(
        tools.create_schedule(plugin, "旧事", "2026-09-01T09:00", "", False, "", None)
    )
    assert "已过去" in past
    bad = run(tools.create_schedule(plugin, "格式", "明天9点", "", False, "", None))
    assert "时间格式无效" in bad


def test_list_schedules_defaults_to_seven_days_and_hides_past():
    store = FakeStore()
    store.items = [
        ScheduleItem.create(title="未来", time="2026-09-12 09:00"),
        ScheduleItem.create(title="过去", time="2026-09-01 09:00"),
    ]
    plugin = make_plugin(store)
    text = run(tools.list_schedules(plugin, None, None))
    assert "未来" in text and "过去" not in text
    assert "最近 7 天" in text


def test_update_and_delete_work():
    store = FakeStore()
    item = ScheduleItem.create(title="旧标题", time="2026-09-11 09:00")
    store.items = [item]
    plugin = make_plugin(store)
    updated = run(
        tools.update_schedule(
            plugin, item.id, "新标题", "2026-09-12T10:00", "", False, None
        )
    )
    assert "已修改" in updated and item.title == "新标题"
    deleted = run(tools.delete_schedule(plugin, item.id, "", None))
    assert "已删除" in deleted and not store.items


def test_create_schedule_writes_apple_event_and_sets_uid():
    apple = FakeApple()
    plugin = make_plugin(apple=apple)
    text = run(
        tools.create_schedule(
            plugin, "组会", "2026-09-11T09:00", "2026-09-11T10:00", False, "", None
        )
    )
    assert "已同步到 Apple 日历" in text
    assert plugin.store.items[0].apple_uid == "apple-1"
    assert apple.calls[0][0] == "create"


def test_update_schedule_calls_apple_update_event():
    item = ScheduleItem.create(title="旧标题", time="2026-09-11 09:00")
    item.apple_uid = "apple-1"
    store = FakeStore()
    store.items = [item]
    apple = FakeApple()
    plugin = make_plugin(store, apple)
    text = run(
        tools.update_schedule(
            plugin, item.id, "新标题", "2026-09-12T10:00", "", False, None
        )
    )
    assert "已修改" in text
    assert [call[0] for call in apple.calls] == ["update"]
    assert apple.calls[0][1] == "apple-1"
    assert item.apple_uid == "apple-1"


def test_update_schedule_clears_uid_when_apple_rejects():
    item = ScheduleItem.create(title="组会", time="2026-09-11 09:00")
    item.apple_uid = "apple-1"
    store = FakeStore()
    store.items = [item]
    plugin = make_plugin(store, FakeApple(update_ok=False))
    text = run(
        tools.update_schedule(plugin, item.id, "组会（改）", "", "", False, None)
    )
    assert "已修改" in text
    assert item.apple_uid == ""


def test_delete_schedule_deletes_apple_event():
    item = ScheduleItem.create(title="组会", time="2026-09-11 09:00")
    item.apple_uid = "apple-1"
    store = FakeStore()
    store.items = [item]
    apple = FakeApple()
    plugin = make_plugin(store, apple)
    text = run(tools.delete_schedule(plugin, item.id, "", None))
    assert "已删除" in text
    assert apple.calls == [("delete", "apple-1")]
    assert store.items == []


def test_delete_schedule_keeps_local_when_apple_delete_fails():
    item = ScheduleItem.create(title="组会", time="2026-09-11 09:00")
    item.apple_uid = "apple-1"
    store = FakeStore()
    store.items = [item]
    plugin = make_plugin(store, FakeApple(delete_ok=False))
    text = run(tools.delete_schedule(plugin, item.id, "", None))
    assert "本地日程已保留" in text
    assert store.items == [item]
