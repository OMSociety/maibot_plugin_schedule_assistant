"""插件层 Apple 同步的落库授权测试。

本轮读取不完整（任一环节失败）时只增改、不删本地日程；同一原因只提示一次；
下一轮读全了恢复删除授权。被测函数用替身 self 直接调用，不构造完整插件。
"""

import asyncio
import logging
import types

from schedule_assistant.plugin import ScheduleAssistantPlugin

LOG = logging.getLogger("maibot.test.apple_sync")
EVT = {"uid": "u1", "summary": "组会", "start": "2030-01-01T12:00:00"}


class FakeApple:
    def __init__(self, events, issues):
        self._events = events
        self._issues = issues
        self.days = []

    async def get_all_events(self, days=1):
        self.days.append(days)
        return self._events, self._issues


class FakeMessaging:
    async def resolve_target_users(self, include_known_users=False):
        return ["u1"]


class RecordingStore:
    def __init__(self):
        self.calls = []

    async def sync_from_apple_calendar(self, user_id, events, *, allow_delete):
        self.calls.append(allow_delete)
        return {"added": 0, "updated": 0, "deleted": 0}


def make_plugin(events, issues):
    store = RecordingStore()
    return types.SimpleNamespace(
        apple_calendar=FakeApple(events, issues),
        messaging=FakeMessaging(),
        store=store,
        ctx=types.SimpleNamespace(logger=LOG),
        _last_apple_read_issue=None,
    )


def sync(plugin):
    return asyncio.run(ScheduleAssistantPlugin._refresh_apple_events_to_store(plugin))


def warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


class TestDeletePermission:
    def test_clean_read_allows_delete(self):
        plugin = make_plugin([EVT], [])
        sync(plugin)
        assert plugin.store.calls == [True]
        assert plugin._last_apple_read_issue is None
        assert plugin.apple_calendar.days == [7]

    def test_incomplete_read_blocks_delete(self, caplog):
        plugin = make_plugin([EVT], ["工作日: 抓取日历事件失败（status=403）"])
        with caplog.at_level("WARNING"):
            sync(plugin)
        assert plugin.store.calls == [False]
        notes = warnings(caplog)
        assert len(notes) == 1
        assert "读取不完整" in notes[0] and "不删除本地日程" in notes[0]
        assert plugin._last_apple_read_issue == (
            "工作日: 抓取日历事件失败（status=403）"
        )

    def test_same_reason_not_repeated(self, caplog):
        plugin = make_plugin([EVT], ["CalDAV 未连接"])
        with caplog.at_level("WARNING"):
            sync(plugin)
            sync(plugin)
        assert plugin.store.calls == [False, False]
        assert len(warnings(caplog)) == 1

    def test_recovery_restores_delete(self, caplog):
        plugin = make_plugin([EVT], ["WebCal 读取失败（status=403）"])
        with caplog.at_level("WARNING"):
            sync(plugin)
        plugin.apple_calendar._issues = []
        with caplog.at_level("WARNING"):
            sync(plugin)
        assert plugin.store.calls == [False, True]
        assert plugin._last_apple_read_issue is None

    def test_empty_events_skips_store(self):
        """空事件列表照旧直接返回：交给 store 的空列表防御也不该发生"""
        plugin = make_plugin([], [])
        sync(plugin)
        assert plugin.store.calls == []
