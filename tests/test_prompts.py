"""intent 模板渲染。"""

from schedule_assistant.prompts import render


def test_custom_template_takes_over_whole():
    out = render(
        "bath",
        {"current_time": "22:00", "default_time": "22:00"},
        "自定义 {current_time}",
    )
    assert out == "自定义 22:00"


def test_unknown_placeholder_kept_as_is():
    assert (
        render("water", {"current_time": "09:30"}, "喝 {current_time} {unknown}")
        == "喝 09:30 {unknown}"
    )


def test_builtin_bath_is_plain_intent():
    out = render("bath", {"current_time": "22:05", "default_time": "22:00"})
    assert "22:05" in out
    assert "提醒用户去洗澡" in out
    assert "20:05" not in out


def test_builtin_morning_contains_requirement_and_example():
    out = render(
        "morning",
        {
            "date": "2026-09-10",
            "weekday": "周四",
            "weather_current": "晴, 20°C",
            "weather_forecast": "白天晴",
            "agenda": "⏰ 09:00 │ 组会",
            "notion_todos": "暂无",
        },
    )
    assert "示例（其语气不纳入参考，按你的表达习惯来）" in out
    assert "今天也要加油呀" in out
    assert "组会" in out


def test_schedule_default_is_plain_intent():
    out = render(
        "schedule",
        {
            "item_title": "组会",
            "time_label": "09:00-10:00",
            "ahead_label": "10 分钟后开始",
            "item_context": "",
        },
    )
    assert "组会" in out
    assert "提醒用户这件事" in out
