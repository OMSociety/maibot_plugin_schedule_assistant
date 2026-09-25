"""日程提醒扫描与 intent 打包的纯逻辑测试。

覆盖：提前量窗口判定（0 < 剩余 <= minutes_before）、防重标记持久化、
habit / 全天 / 停用条目跳过、已开始事件不提醒、source 字段、
parse_item_time 各格式、build_schedule_reminder_intent 的时间/语境拼装。
全部不依赖 SDK，直接用 tmp_path 建真实存储验证。
"""

import asyncio
from datetime import datetime, timedelta

from schedule_assistant.reminders.schedule import (
    build_schedule_reminder_intent,
    collect_due_schedule_items,
    parse_item_time,
)
from schedule_assistant.schedule_store import ScheduleItem, ScheduleStore


def _store(tmp_path):
    store = ScheduleStore()
    store.set_data_dir(tmp_path)
    return store


def _at(minutes: int) -> str:
    """now+N 分钟的时间串（截断到分钟，断言窗口留了余量）"""
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


def _add(store, user_id="u", **kw):
    item = ScheduleItem(
        type=kw.pop("type", "schedule"),
        title=kw.pop("title", "测试日程"),
        **kw,
    )
    asyncio.run(store.add_item(user_id, item))
    return item


def _collect(store, user_id="u", minutes=10):
    """按「无回调、挑选即已提醒」口径取到点日程（显式启用内联标记）"""
    return asyncio.run(
        collect_due_schedule_items(store, user_id, minutes, mark_triggered=True)
    )


class TestCollectDueScheduleItems:
    """到点判定与防重"""

    def test_within_window_due_then_dedup(self, tmp_path):
        store = _store(tmp_path)
        item = _add(store, time=_at(5))

        due = _collect(store)
        assert len(due) == 1
        assert due[0]["item_id"] == item.id
        assert due[0]["title"] == "测试日程"
        assert 3 <= due[0]["minutes_until"] <= 5

        # 防重：同一事件第二次扫描不再触发
        assert _collect(store) == []

    def test_last_triggered_persisted(self, tmp_path):
        store = _store(tmp_path)
        item = _add(store, time=_at(5))
        _collect(store)

        revived = asyncio.run(store.list_all_items("u"))[0]
        assert revived.id == item.id
        assert revived.last_triggered  # 持久化写盘，重启不重发

    def test_outside_window_not_due(self, tmp_path):
        store = _store(tmp_path)
        _add(store, time=_at(30))
        assert _collect(store) == []

    def test_boundary_inclusive(self, tmp_path):
        """提前量窗口上边界含 minutes_before 本身（扫描间隔不大于提前量时必达）"""
        store = _store(tmp_path)
        _add(store, time=_at(10))
        due = _collect(store)
        assert len(due) == 1

    def test_already_started_not_due(self, tmp_path):
        store = _store(tmp_path)
        _add(store, time=_at(-5))
        assert _collect(store) == []

    def test_habit_skipped(self, tmp_path):
        """habit（洗澡/睡觉/喝水）走独立定时任务，不进日程提醒"""
        store = _store(tmp_path)
        _add(store, type="habit", title="喝水", time=_at(5))
        assert _collect(store) == []

    def test_all_day_skipped(self, tmp_path):
        store = _store(tmp_path)
        day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        _add(store, title="全天事项", time=day, all_day=True)
        assert _collect(store) == []

    def test_disabled_skipped(self, tmp_path):
        store = _store(tmp_path)
        _add(store, time=_at(5), enabled=False)
        assert _collect(store) == []

    def test_end_time_and_context_fields(self, tmp_path):
        store = _store(tmp_path)
        _add(
            store,
            title="组会",
            time=_at(5),
            end_time=_at(65),
            context="记得带电脑",
        )
        due = _collect(store)
        assert due[0]["context"] == "记得带电脑"
        assert due[0]["end"]  # 区间日程带结束时刻
        assert ":" in due[0]["end"]

    def test_source_field(self, tmp_path):
        store = _store(tmp_path)
        _add(store, title="本地", time=_at(5))
        _add(store, title="苹果", time=_at(6), apple_uid="uid-1")
        due = _collect(store)
        sources = {d["title"]: d["source"] for d in due}
        assert sources == {"本地": "local", "苹果": "apple"}


class TestParseItemTime:
    """时间串解析：ISO / 普通格式 / date-only / 仅时刻 / 非法"""

    def test_iso_with_z(self):
        dt = parse_item_time("2026-09-10T14:30:00Z")
        assert dt is not None
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (
            2026,
            9,
            10,
            14,
            30,
        )

    def test_plain_format(self):
        dt = parse_item_time("2026-09-10 14:30")
        assert dt is not None
        assert (dt.hour, dt.minute) == (14, 30)

    def test_date_only(self):
        dt = parse_item_time("2026-09-10")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 9, 10)

    def test_clock_only(self):
        dt = parse_item_time("14:30")
        assert dt is not None
        assert (dt.hour, dt.minute) == (14, 30)

    def test_invalid(self):
        assert parse_item_time("随便说说") is None
        assert parse_item_time("") is None


class TestBuildScheduleReminderIntent:
    """intent 打包：时间标签 / 语境后缀 / 多事件合并为一条"""

    def test_single_with_range_and_context(self):
        intent = build_schedule_reminder_intent(
            [
                {
                    "title": "组会",
                    "start": "14:30",
                    "end": "16:30",
                    "minutes_until": 8,
                    "context": "记得带电脑",
                }
            ]
        )
        assert intent.startswith("日程提醒：")
        assert "- 「组会」14:30-16:30 开始（约 8 分钟后）｜记得带电脑" in intent

    def test_single_point_no_context(self):
        intent = build_schedule_reminder_intent(
            [{"title": "开会", "start": "15:00", "end": "", "minutes_until": 3}]
        )
        assert "- 「开会」15:00 开始（约 3 分钟后）" in intent
        assert "｜" not in intent

    def test_zero_minutes(self):
        intent = build_schedule_reminder_intent(
            [{"title": "开会", "start": "15:00", "end": "", "minutes_until": 0}]
        )
        assert "马上开始" in intent

    def test_multi_items_bundled(self):
        intent = build_schedule_reminder_intent(
            [
                {"title": "组会", "start": "14:30", "end": "16:30", "minutes_until": 8},
                {"title": "开会", "start": "15:00", "end": "", "minutes_until": 3},
            ]
        )
        assert intent.count("日程提醒：") == 1  # 多事件合并为一次开口
        assert "「组会」" in intent
        assert "「开会」" in intent
