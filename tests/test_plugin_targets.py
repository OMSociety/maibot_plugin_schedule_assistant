"""配置目标归一化。"""

from types import SimpleNamespace

from schedule_assistant.plugin import ScheduleAssistantPlugin


def _plugin_with_targets(values):
    return SimpleNamespace(
        config=SimpleNamespace(basic=SimpleNamespace(user_ids=values))
    )


def test_targets_uses_module_helper_not_local_name():
    plugin = _plugin_with_targets(["qq:10001", "telegram:20002"])
    assert ScheduleAssistantPlugin._targets(plugin) == [
        "qq:10001",
        "telegram:20002",
    ]


def test_targets_normalizes_bare_ids_with_default_platform():
    plugin = _plugin_with_targets(["30003"])
    assert ScheduleAssistantPlugin._targets(plugin) == ["qq:30003"]


def test_targets_skips_empty_and_applies_default_platform():
    plugin = _plugin_with_targets(["", "bad-value", "qq:40004"])
    assert ScheduleAssistantPlugin._targets(plugin) == ["qq:bad-value", "qq:40004"]
