"""工具的时间解析与用户识别。"""

from datetime import datetime
from types import SimpleNamespace

from schedule_assistant.tools.schedule_tools import (
    _extract_user_id,
    _parse_clock,
    _parse_schedule_time,
)

NOW = datetime(2026, 9, 10, 20, 0)


def test_parse_clock_midnight_variants():
    assert _parse_clock("凌晨12点") == (0, 0)
    assert _parse_clock("中午12点") == (12, 0)
    assert _parse_clock("晚上12点") == (0, 0)
    assert _parse_clock("晚上11点半") == (23, 30)


def test_parse_schedule_time_evening_midnight_moves_to_next_day():
    start, end, all_day = _parse_schedule_time("晚上12点", NOW)
    assert all_day is False
    assert start == datetime(2026, 9, 11, 0, 0)
    assert end is None


def test_parse_schedule_time_noon_stays_same_day():
    start, _, _ = _parse_schedule_time("中午12点", NOW)
    assert start == datetime(2026, 9, 10, 12, 0)


def test_parse_schedule_time_range_and_all_day():
    start, end, all_day = _parse_schedule_time("明天9点到11点", NOW)
    assert (start, end, all_day) == (
        datetime(2026, 9, 11, 9, 0),
        datetime(2026, 9, 11, 11, 0),
        False,
    )

    start, end, all_day = _parse_schedule_time("明天全天", NOW)
    assert all_day is True
    assert start == datetime(2026, 9, 11, 0, 0) and end is None


def test_parse_schedule_time_invalid():
    assert _parse_schedule_time("随便什么时候", NOW) == (None, None, False)


def test_extract_user_id_prefers_message_sender():
    plugin = SimpleNamespace(
        config=SimpleNamespace(basic=SimpleNamespace(user_ids=["qq:99999"]))
    )
    message = {"user_info": {"user_id": "12345"}}
    assert _extract_user_id(plugin, message) == "12345"


def test_extract_user_id_falls_back_to_config():
    plugin = SimpleNamespace(
        config=SimpleNamespace(basic=SimpleNamespace(user_ids=["qq:99999"]))
    )
    assert _extract_user_id(plugin, None) == "99999"
