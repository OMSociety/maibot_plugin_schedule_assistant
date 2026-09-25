"""日程工具时间解析与 CRUD 落库的纯逻辑测试（点 / 区间 / 全天）。

覆盖：_parse_clock 中文时刻表达、_parse_schedule_time 三种形态、
create/update 落库字段与错误分支（结束早于开始、只给结束时间）、
改期重置防重标记、Apple 写入/回写成败文案、list_schedules 的全天/区间展示
与 days 数字回退。
plugin 实例用 types.SimpleNamespace 伪造，存储走 tmp_path 真实文件。
"""

import asyncio
import types
from datetime import datetime, timedelta

import pytest

from schedule_assistant.schedule_store import ScheduleItem, ScheduleStore
from schedule_assistant.tools.schedule_tools import (
    _parse_clock,
    _parse_schedule_time,
    create_schedule,
    list_schedules,
    update_schedule,
)

MSG = {"user_info": {"user_id": "123"}}


class FakeApple:
    """记录 create_event / update_event 调用参数的 Apple 日历替身

    create_uid=None 表示写入失败（等价于 AppleCalendar 收到 4xx 时返回 None）；
    update_ok 控制 update_event 的成败。
    """

    def __init__(self, create_uid="uid-1", update_ok=True):
        self.calls = []
        self.update_calls = []
        self.create_uid = create_uid
        self.update_ok = update_ok

    async def create_event(
        self, summary, start, end=None, calendar_id=None, description="", all_day=False
    ):
        self.calls.append(
            {"summary": summary, "start": start, "end": end, "all_day": all_day}
        )
        return self.create_uid

    async def update_event(
        self,
        uid,
        summary,
        start,
        end=None,
        calendar_id=None,
        description="",
        all_day=False,
    ):
        self.update_calls.append(
            {
                "uid": uid,
                "summary": summary,
                "start": start,
                "end": end,
                "all_day": all_day,
            }
        )
        return self.update_ok


def _plugin(tmp_path, apple=None):
    store = ScheduleStore()
    store.set_data_dir(tmp_path)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            calendar_sync=types.SimpleNamespace(
                enable_apple_calendar_sync=apple is not None
            )
        ),
        apple_calendar=apple,
        store=store,
    )


class TestParseClock:
    """中文时刻表达式"""

    def test_hour_only(self):
        assert _parse_clock("9点") == (9, 0)

    def test_half_hour(self):
        assert _parse_clock("3点半") == (3, 30)

    def test_colon(self):
        assert _parse_clock("14:30") == (14, 30)

    def test_pm(self):
        assert _parse_clock("下午3点") == (15, 0)

    def test_pm_with_minutes(self):
        assert _parse_clock("晚上8点30") == (20, 30)

    def test_am_12_is_midnight(self):
        assert _parse_clock("上午12点") == (0, 0)

    def test_invalid(self):
        assert _parse_clock("25点") is None
        assert _parse_clock("abc") is None
        assert _parse_clock("") is None


class TestParseScheduleTime:
    """单时间点 / 时间区间 / 全天"""

    def test_point_iso(self):
        start, end, all_day = _parse_schedule_time("2026-09-10 14:30")
        assert start == datetime(2026, 9, 10, 14, 30)
        assert end is None
        assert all_day is False

    def test_point_relative(self):
        start, end, all_day = _parse_schedule_time("明天9点")
        assert start is not None
        assert start.date() == (datetime.now() + timedelta(days=1)).date()
        assert (start.hour, start.minute) == (9, 0)
        assert end is None
        assert all_day is False

    def test_range_clock_end(self):
        start, end, all_day = _parse_schedule_time("明天9点到11点")
        assert start is not None and end is not None
        assert (start.hour, start.minute) == (9, 0)
        assert (end.hour, end.minute) == (11, 0)
        assert end.date() == start.date()
        assert all_day is False

    def test_range_cross_midnight(self):
        """「23点到1点」结束视为次日凌晨"""
        start, end, all_day = _parse_schedule_time("23点到1点")
        assert start is not None and end is not None
        assert end - start == timedelta(hours=2)
        assert all_day is False

    def test_range_hyphen(self):
        start, end, all_day = _parse_schedule_time("2026-09-10 09:00-11:00")
        assert start == datetime(2026, 9, 10, 9, 0)
        assert end == datetime(2026, 9, 10, 11, 0)
        assert all_day is False

    def test_range_tilde(self):
        start, end, all_day = _parse_schedule_time("09:00~11:00")
        assert start is not None and end is not None
        assert end - start == timedelta(hours=2)

    def test_equal_end_not_cross_day(self):
        """「9点到9点」等值区间不跨天（等值交给调用方报错）"""
        start, end, all_day = _parse_schedule_time("2026-09-10 9点到9点")
        assert end == start
        assert all_day is False

    def test_date_only_range_is_all_day(self):
        """两端纯日期的区间（「明天到后天」）按全天处理，多日取开始日"""
        start, end, all_day = _parse_schedule_time("2026-09-10到2026-09-12")
        assert start == datetime(2026, 9, 10, 0, 0)
        assert end is None
        assert all_day is True

    def test_all_day_flag_wins_over_range(self):
        """「全天」关键词优先于区间时刻（「…9点到11点全天」按全天处理）"""
        start, end, all_day = _parse_schedule_time("2026-09-10 9点到11点全天")
        assert start == datetime(2026, 9, 10, 0, 0)
        assert end is None
        assert all_day is True

    def test_all_day_bare_date(self):
        """纯日期输入即全天"""
        start, end, all_day = _parse_schedule_time("明天")
        assert start is not None
        assert start.date() == (datetime.now() + timedelta(days=1)).date()
        assert (start.hour, start.minute) == (0, 0)
        assert end is None
        assert all_day is True

    def test_all_day_keyword(self):
        start, end, all_day = _parse_schedule_time("明天全天")
        assert all_day is True
        assert end is None
        assert start is not None

    def test_invalid(self):
        assert _parse_schedule_time("随便说说") == (None, None, False)
        assert _parse_schedule_time("") == (None, None, False)


class TestCreateSchedule:
    """创建日程：字段落库 / Apple 写入参数 / 错误分支"""

    def test_point_mode(self, tmp_path):
        plugin = _plugin(tmp_path)
        res = asyncio.run(
            create_schedule(plugin, "开会", "2026-09-10 14:30", "", "讨论", MSG)
        )
        assert "✅" in res
        assert "09-10 14:30" in res

        item = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert item.time == "2026-09-10 14:30"
        assert item.end_time is None
        assert item.all_day is False
        assert item.context == "讨论"

    def test_range_via_end_param(self, tmp_path):
        plugin = _plugin(tmp_path)
        res = asyncio.run(
            create_schedule(plugin, "组会", "2026-09-10 09:00", "11:00", "", MSG)
        )
        assert "09:00-11:00" in res

        item = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert item.end_time == "2026-09-10 11:00"
        assert item.all_day is False

    def test_range_inline(self, tmp_path):
        plugin = _plugin(tmp_path)
        asyncio.run(
            create_schedule(plugin, "组会", "2026-09-10 09:00到11:00", "", "", MSG)
        )
        item = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert item.time == "2026-09-10 09:00"
        assert item.end_time == "2026-09-10 11:00"

    def test_all_day_bare_date(self, tmp_path):
        plugin = _plugin(tmp_path)
        res = asyncio.run(
            create_schedule(plugin, "全天事项", "2026-09-10", "", "", MSG)
        )
        assert "全天" in res

        item = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert item.time == "2026-09-10"  # date-only
        assert item.all_day is True
        assert item.end_time is None

    def test_all_day_ignores_end_param(self, tmp_path):
        plugin = _plugin(tmp_path)
        asyncio.run(
            create_schedule(plugin, "全天事项", "2026-09-10 全天", "11点", "", MSG)
        )
        item = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert item.all_day is True
        assert item.end_time is None

    def test_end_before_start_rejected(self, tmp_path):
        plugin = _plugin(tmp_path)
        res = asyncio.run(
            create_schedule(
                plugin, "X", "2026-09-10 11:00", "2026-09-10 09:00", "", MSG
            )
        )
        assert res == "结束时间需要晚于开始时间"
        assert asyncio.run(plugin.store.list_all_items("123")) == []

    def test_equal_end_rejected(self, tmp_path):
        """「9点到9点」报错，不按 24 小时区间落库"""
        plugin = _plugin(tmp_path)
        res = asyncio.run(
            create_schedule(plugin, "X", "2026-09-10 9点到9点", "", "", MSG)
        )
        assert res == "结束时间需要晚于开始时间"
        assert asyncio.run(plugin.store.list_all_items("123")) == []

    def test_date_only_range_creates_all_day(self, tmp_path):
        plugin = _plugin(tmp_path)
        res = asyncio.run(
            create_schedule(plugin, "团建", "2026-09-10到2026-09-12", "", "", MSG)
        )
        assert "全天" in res

        item = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert item.time == "2026-09-10"
        assert item.all_day is True
        assert item.end_time is None

    def test_cross_midnight_via_end_param(self, tmp_path):
        plugin = _plugin(tmp_path)
        res = asyncio.run(
            create_schedule(plugin, "夜班", "2026-09-10 23:00", "1点", "", MSG)
        )
        assert "→" in res  # 跨天区间用箭头显示

        item = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert item.end_time == "2026-09-11 01:00"

    def test_apple_write_params(self, tmp_path):
        apple = FakeApple()
        plugin = _plugin(tmp_path, apple=apple)
        res = asyncio.run(
            create_schedule(plugin, "组会", "2026-09-10 09:00到11:00", "", "讨论", MSG)
        )
        assert "已同步到 Apple 日历" in res
        assert apple.calls == [
            {
                "summary": "组会",
                "start": datetime(2026, 9, 10, 9, 0),
                "end": datetime(2026, 9, 10, 11, 0),
                "all_day": False,
            }
        ]
        item = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert item.apple_uid == "uid-1"

    def test_apple_all_day_param(self, tmp_path):
        apple = FakeApple()
        plugin = _plugin(tmp_path, apple=apple)
        asyncio.run(create_schedule(plugin, "全天", "2026-09-10", "", "", MSG))
        assert apple.calls[0]["all_day"] is True

    def test_apple_write_failure_no_uid_no_success_text(self, tmp_path):
        """写入失败（4xx → create_event 返回 None）：不落 apple_uid，也不谎报已同步"""
        apple = FakeApple(create_uid=None)
        plugin = _plugin(tmp_path, apple=apple)
        res = asyncio.run(create_schedule(plugin, "开会", "2026-09-10 14:30", "", "", MSG))
        assert "已同步到 Apple 日历" not in res
        assert "✅" in res  # 本地日程照常创建

        item = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert item.apple_uid is None
        assert apple.calls  # 确实尝试过写入


class TestUpdateSchedule:
    """修改日程：改期重置防重标记 / 全天互转 / 错误分支"""

    def _seeded_item(self, plugin):
        item = ScheduleItem(
            type="schedule",
            title="组会",
            time="2026-09-10 14:30",
            end_time="2026-09-10 16:30",
        )
        asyncio.run(plugin.store.add_item("123", item))
        return item

    def test_reschedule_resets_last_triggered(self, tmp_path):
        plugin = _plugin(tmp_path)
        item = self._seeded_item(plugin)
        item.last_triggered = datetime.now().isoformat()
        asyncio.run(plugin.store.update_item("123", item))

        res = asyncio.run(
            update_schedule(plugin, item.id, "", "2026-09-11 09:00到11:00", "", MSG)
        )
        assert "✅" in res

        revived = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert revived.time == "2026-09-11 09:00"
        assert revived.end_time == "2026-09-11 11:00"
        assert revived.last_triggered is None  # 改期重新提醒

    def test_title_change_keeps_last_triggered(self, tmp_path):
        plugin = _plugin(tmp_path)
        item = self._seeded_item(plugin)
        item.last_triggered = datetime.now().isoformat()
        asyncio.run(plugin.store.update_item("123", item))

        res = asyncio.run(update_schedule(plugin, item.id, "新标题", "", "", MSG))
        assert "✅" in res

        revived = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert revived.title == "新标题"
        assert revived.last_triggered is not None

    def test_update_to_all_day(self, tmp_path):
        plugin = _plugin(tmp_path)
        item = self._seeded_item(plugin)

        asyncio.run(update_schedule(plugin, item.id, "", "2026-09-13", "", MSG))
        revived = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert revived.time == "2026-09-13"
        assert revived.all_day is True
        assert revived.end_time is None

    def test_end_only_requires_start(self, tmp_path):
        plugin = _plugin(tmp_path)
        item = self._seeded_item(plugin)

        res = asyncio.run(update_schedule(plugin, item.id, "", "", "11点", MSG))
        assert res == "请一并提供开始时间，或直接用「9点到11点」的区间写法"

    def test_end_before_start_rejected(self, tmp_path):
        """结束带日期且早于开始时报错（只给时刻的写法按跨天区间处理）"""
        plugin = _plugin(tmp_path)
        item = self._seeded_item(plugin)

        res = asyncio.run(
            update_schedule(
                plugin, item.id, "", "2026-09-10 11:00", "2026-09-10 09:00", MSG
            )
        )
        assert res == "结束时间需要晚于开始时间"

    def test_no_apple_uid_no_write_back(self, tmp_path):
        """纯本地日程（无 apple_uid）不回写 Apple，也不出现同步文案"""
        apple = FakeApple()
        plugin = _plugin(tmp_path, apple=apple)
        item = self._seeded_item(plugin)

        res = asyncio.run(
            update_schedule(plugin, item.id, "改过的组会", "2026-09-11 09:00", "", MSG)
        )
        assert res == "已修改日程：标题改为「改过的组会」, 时间改为09-11 09:00 ✅"
        assert apple.update_calls == []

    def test_apple_uid_write_back_params(self, tmp_path):
        """带 apple_uid 的改期按原 UID 回写（同 UID 的 PUT 即更新）"""
        apple = FakeApple()
        plugin = _plugin(tmp_path, apple=apple)
        item = self._seeded_item(plugin)
        item.apple_uid = "uid-9"
        asyncio.run(plugin.store.update_item("123", item))

        res = asyncio.run(
            update_schedule(plugin, item.id, "新标题", "2026-09-11 09:00", "", MSG)
        )
        assert "已更新 Apple 日历" in res
        assert apple.update_calls == [
            {
                "uid": "uid-9",
                "summary": "新标题",
                "start": datetime(2026, 9, 11, 9, 0),
                "end": None,
                "all_day": False,
            }
        ]

    def test_apple_uid_write_back_failure_unlinks_apple(self, tmp_path, caplog):
        """回写失败：只提示本地已改，并清空 apple_uid 脱离同步（否则下轮同步覆盖回去）"""
        apple = FakeApple(update_ok=False)
        plugin = _plugin(tmp_path, apple=apple)
        item = self._seeded_item(plugin)
        item.apple_uid = "uid-9"
        asyncio.run(plugin.store.update_item("123", item))

        with caplog.at_level("WARNING"):
            res = asyncio.run(
                update_schedule(plugin, item.id, "新标题", "2026-09-11 09:00", "", MSG)
            )

        assert res == "已修改日程：标题改为「新标题」, 时间改为09-11 09:00 ✅"
        assert "已更新 Apple 日历" not in res
        assert apple.update_calls[0]["uid"] == "uid-9"

        revived = asyncio.run(plugin.store.list_all_items("123"))[0]
        assert revived.title == "新标题"
        assert revived.apple_uid is None  # 脱离 Apple 同步，改动不再被旧值覆盖
        assert "已解除 Apple 同步" in caplog.text

    def test_apple_uid_write_back_exception_unlinks_apple(self, tmp_path):
        """回写抛异常按失败处理：同样清空 apple_uid，不向上冒泡"""
        apple = FakeApple()
        plugin = _plugin(tmp_path, apple=apple)
        item = self._seeded_item(plugin)
        item.apple_uid = "uid-9"
        asyncio.run(plugin.store.update_item("123", item))

        async def boom(*a, **kw):
            raise RuntimeError("boom")

        apple.update_event = boom
        res = asyncio.run(
            update_schedule(plugin, item.id, "新标题", "2026-09-11 09:00", "", MSG)
        )
        assert "已更新 Apple 日历" not in res
        assert asyncio.run(plugin.store.list_all_items("123"))[0].apple_uid is None

    def test_title_only_change_writes_back_same_time(self, tmp_path):
        """只改标题也回写：Apple 事件标题必须同步，否则下轮同步把旧标题写回本地"""
        apple = FakeApple()
        plugin = _plugin(tmp_path, apple=apple)
        item = self._seeded_item(plugin)
        item.apple_uid = "uid-9"
        asyncio.run(plugin.store.update_item("123", item))

        asyncio.run(update_schedule(plugin, item.id, "新标题", "", "", MSG))
        assert apple.update_calls == [
            {
                "uid": "uid-9",
                "summary": "新标题",
                "start": datetime(2026, 9, 10, 14, 30),
                "end": datetime(2026, 9, 10, 16, 30),
                "all_day": False,
            }
        ]


class TestListSchedulesDisplay:
    """列表展示：全天 / 区间 / 单点"""

    def test_all_day_and_range_shown(self, tmp_path):
        plugin = _plugin(tmp_path)
        day = datetime.now().strftime("%Y-%m-%d")
        asyncio.run(
            plugin.store.add_item(
                "123",
                ScheduleItem(type="schedule", title="全天事项", time=day, all_day=True),
            )
        )
        asyncio.run(
            plugin.store.add_item(
                "123",
                ScheduleItem(
                    type="schedule",
                    title="组会",
                    time=f"{day} 10:00",
                    end_time=f"{day} 11:30",
                ),
            )
        )
        asyncio.run(
            plugin.store.add_item(
                "123",
                ScheduleItem(type="schedule", title="开会", time=f"{day} 09:00"),
            )
        )

        res = asyncio.run(list_schedules(plugin, day, MSG))
        assert "📅 全天 │ 全天事项" in res  # 全天事件不再被静默跳过
        assert "⏰ 10:00-11:30 │ 组会" in res
        assert "⏰ 09:00 │ 开会" in res

    @pytest.mark.parametrize("date_arg", ["0", "0 "])
    def test_zero_days_falls_back_to_window(self, tmp_path, date_arg):
        """date="0" 这类数字必须回退到有效窗口：days=0 会让 future=None，
        now <= dt <= future 直接 TypeError（date=None 走 else 分支反而是安全的）"""
        plugin = _plugin(tmp_path)
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        asyncio.run(
            plugin.store.add_item(
                "123", ScheduleItem(type="schedule", title="明天的会", time=tomorrow)
            )
        )

        res = asyncio.run(list_schedules(plugin, date_arg, MSG))
        assert "查看日程失败" not in res
        assert "明天的会" in res  # 默认窗口（至少 1 天）覆盖到明天
        assert "接下来" in res

    def test_zero_days_empty_store_has_no_error_text(self, tmp_path):
        """库空时 days=0 会提前 return，不得再出现 TypeError 文案"""
        plugin = _plugin(tmp_path)
        res = asyncio.run(list_schedules(plugin, "0", MSG))
        assert res == "最近1天没有日程安排~"
