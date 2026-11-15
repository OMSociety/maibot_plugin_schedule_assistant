"""推送文本的格式化函数。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from schedule_assistant.notices import (
    ahead_label,
    format_agenda,
    format_ddl,
    is_all_day,
    parse_item_time,
    time_label,
    weekday_label,
)

TZ = ZoneInfo("Asia/Shanghai")


def test_weekday_label():
    assert weekday_label(datetime(2026, 9, 10)) == "周四"


def test_parse_item_time_naive_and_aware():
    assert parse_item_time("2026-09-10 09:00") == datetime(2026, 9, 10, 9, 0)
    assert parse_item_time("2026-09-10T01:00:00Z", TZ) == datetime(2026, 9, 10, 9, 0)
    assert parse_item_time("") is None
    assert parse_item_time("not-a-date") is None


def test_is_all_day_by_field_and_text():
    assert is_all_day(SimpleNamespace(all_day=True, time="2026-09-10 09:00")) is True
    assert is_all_day(SimpleNamespace(all_day=False, time="2026-09-10")) is True
    assert is_all_day(SimpleNamespace(all_day=False, time="2026-09-10 09:00")) is False


def test_time_label_forms():
    start = datetime(2026, 9, 10, 9, 0)
    assert time_label(start, None, True) == "全天"
    assert time_label(start, "2026-09-10 10:30", False) == "09:00-10:30"
    assert time_label(start, None, False) == "09:00"


def test_ahead_label():
    assert ahead_label(0) == "马上开始"
    assert ahead_label(10) == "10 分钟后开始"
    assert ahead_label(60) == "1 小时后开始"
    assert ahead_label(90) == "1 小时 30 分钟后开始"


def test_format_agenda():
    assert format_agenda([]) == "暂无"
    assert format_agenda([("09:00", "组会")]) == "⏰ 09:00 │ 组会"


def test_format_ddl_relative_days():
    today = datetime.now(TZ).date()
    assert format_ddl("") == ""
    assert format_ddl(f"{today.isoformat()}T12:00:00+08:00") == "今天截止"
    future = today + timedelta(days=3)
    assert format_ddl(f"{future.isoformat()}T12:00:00+08:00") == "还剩3天"
    past = today - timedelta(days=1)
    assert format_ddl(f"{past.isoformat()}T12:00:00+08:00") == "已逾期1天"
