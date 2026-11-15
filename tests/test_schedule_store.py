"""JSON 日程存储与 Apple 合并。"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from schedule_assistant.schedule_store import ScheduleItem, ScheduleStore

TZ = ZoneInfo("Asia/Shanghai")


def run(coro):
    return asyncio.run(coro)


def make_store(tmp_path) -> ScheduleStore:
    store = ScheduleStore()
    store.set_data_dir(str(tmp_path))
    return store


def test_crud_and_persistence(tmp_path):
    store = make_store(tmp_path)
    item = ScheduleItem.create(title="组会", time="2026-09-10 09:00", context="A101")
    run(store.add_item("u1", item))

    loaded = run(store.get_item("u1", item.id))
    assert loaded is not None and loaded.title == "组会"

    loaded.title = "组会（改）"
    loaded.time = "2026-09-10 10:00"
    assert run(store.update_item("u1", loaded)) is True

    reopened = make_store(tmp_path)
    items = run(reopened.list_items("u1"))
    assert len(items) == 1 and items[0].title == "组会（改）"

    assert run(store.remove_item("u1", item.id)) is True
    assert run(store.list_items("u1")) == []


def test_sessions_are_isolated(tmp_path):
    store = make_store(tmp_path)
    run(store.add_item("u1", ScheduleItem.create(title="A", time="2026-09-10 09:00")))
    run(store.add_item("u2", ScheduleItem.create(title="B", time="2026-09-10 09:00")))
    assert [item.title for item in run(store.list_items("u1"))] == ["A"]
    assert [item.title for item in run(store.list_items("u2"))] == ["B"]


def test_sync_apple_converts_offset_to_local(tmp_path):
    store = make_store(tmp_path)
    now = datetime(2026, 9, 10, 8, 0, tzinfo=TZ)
    events = [
        {
            "uid": "u1",
            "summary": "组会",
            "start": "2026-09-10T01:00:00+00:00",
            "end": "2026-09-10T02:00:00+00:00",
            "all_day": False,
            "description": "",
        }
    ]
    stats = run(
        store.sync_from_apple_calendar("u1", events, allow_delete=True, now=now)
    )
    assert stats["added"] == 1
    item = run(store.list_items("u1"))[0]
    assert item.time == "2026-09-10 09:00"
    assert item.end_time == "2026-09-10 10:00"
    assert item.apple_uid == "u1"


def test_sync_skips_duplicate_uids(tmp_path):
    store = make_store(tmp_path)
    now = datetime(2026, 9, 10, 8, 0, tzinfo=TZ)
    events = [
        {"uid": "u1", "summary": "A", "start": "2026-09-10T09:00:00", "all_day": False},
        {
            "uid": "u1",
            "summary": "A2",
            "start": "2026-09-10T09:00:00",
            "all_day": False,
        },
    ]
    run(store.sync_from_apple_calendar("u1", events, allow_delete=True, now=now))
    assert len(run(store.list_items("u1"))) == 1


def test_sync_empty_events_does_not_delete(tmp_path):
    store = make_store(tmp_path)
    now = datetime(2026, 9, 10, 8, 0, tzinfo=TZ)
    items = [
        {"uid": "u1", "summary": "A", "start": "2026-09-10T09:00:00", "all_day": False}
    ]
    run(store.sync_from_apple_calendar("u1", items, allow_delete=True, now=now))
    stats = run(store.sync_from_apple_calendar("u1", [], allow_delete=True, now=now))
    assert stats == {"added": 0, "updated": 0, "deleted": 0}
    assert len(run(store.list_items("u1"))) == 1


def test_sync_deletes_missing_uid_only_when_allowed(tmp_path):
    store = make_store(tmp_path)
    now = datetime(2026, 9, 10, 8, 0, tzinfo=TZ)
    first = [
        {"uid": "u1", "summary": "A", "start": "2026-09-10T09:00:00", "all_day": False},
        {"uid": "u2", "summary": "B", "start": "2026-09-10T10:00:00", "all_day": False},
    ]
    run(store.sync_from_apple_calendar("u1", first, allow_delete=True, now=now))
    second = [
        {"uid": "u2", "summary": "B", "start": "2026-09-10T10:00:00", "all_day": False}
    ]

    blocked = run(
        store.sync_from_apple_calendar("u1", second, allow_delete=False, now=now)
    )
    assert blocked["deleted"] == 0 and len(run(store.list_items("u1"))) == 2

    allowed = run(
        store.sync_from_apple_calendar("u1", second, allow_delete=True, now=now)
    )
    assert allowed["deleted"] == 1 and len(run(store.list_items("u1"))) == 1
