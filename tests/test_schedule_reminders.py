"""日程提醒的到期判定与扫描目标。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from schedule_assistant import plugin as plugin_module
from schedule_assistant.reminders.schedule import collect_due_schedule_items
from schedule_assistant.schedule_store import ScheduleItem, ScheduleStore

TZ = ZoneInfo("Asia/Shanghai")


def run(coro):
    return asyncio.run(coro)


def make_store(tmp_path):
    store = ScheduleStore()
    store.set_data_dir(str(tmp_path))
    return store


def test_collect_due_accepts_aware_now(tmp_path):
    store = make_store(tmp_path)
    run(
        store.add_item(
            "qq:10001", ScheduleItem.create(title="组会", time="2026-09-10 20:05")
        )
    )
    due = run(
        collect_due_schedule_items(
            store, "qq:10001", 10, datetime(2026, 9, 10, 20, 0, tzinfo=TZ)
        )
    )
    assert len(due) == 1
    assert due[0]["title"] == "组会"
    assert due[0]["minutes_until"] == 5


def test_schedule_scan_reads_full_target(monkeypatch):
    seen: list[str] = []

    async def fake_collect(store, user_id, minutes, now):
        seen.append(user_id)
        return []

    async def noop():
        return None

    monkeypatch.setattr(plugin_module, "collect_due_schedule_items", fake_collect)
    plugin = plugin_module.ScheduleAssistantPlugin.__new__(
        plugin_module.ScheduleAssistantPlugin
    )
    plugin._scan_lock = asyncio.Lock()
    plugin._timezone = TZ
    plugin._plugin_config_instance = SimpleNamespace(
        schedule_reminder=SimpleNamespace(schedule_reminder_minutes=10),
        prompt_settings=SimpleNamespace(prompt_schedule=""),
    )
    plugin._ctx = SimpleNamespace(logger=SimpleNamespace(warning=lambda *a, **k: None))
    plugin.store = object()
    plugin._targets = lambda: ["qq:10001"]
    plugin._ensure_services = noop
    plugin._apple_sync = noop

    run(plugin._schedule_scan())
    assert seen == ["qq:10001"]
