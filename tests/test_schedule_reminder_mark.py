"""日程提醒防重标记与插件扫描编排的测试。

覆盖两根对外不变量：
1. 防重标记只在「提醒确实发出去了」之后落盘 —— 先落标记再触发时，
   Maisaka 开口失败（最常见是用户当前不在活跃 stream）会让该日程本期永久不再提醒；
2. 插件 _schedule_reminder_scan 的编排顺序：触发失败不落标记、触发成功才落标记。

plugin.py 依赖 maibot_sdk（插件运行依赖，本机测试环境未安装），本文件用最小替身
注入后再导入 —— 替身只提供导入与构造所需接口，不模拟 SDK 行为。
"""

import asyncio
import sys
import types
from datetime import datetime, timedelta

import pytest

from schedule_assistant.reminders.schedule import (
    collect_due_schedule_items,
    mark_schedule_items_triggered,
)
from schedule_assistant.schedule_store import ScheduleItem, ScheduleStore


def _store(tmp_path):
    store = ScheduleStore()
    store.set_data_dir(tmp_path)
    return store


def _at(minutes: int) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


def _add(store, user_id="u", **kw):
    item = ScheduleItem(
        type=kw.pop("type", "schedule"),
        title=kw.pop("title", "测试日程"),
        **kw,
    )
    asyncio.run(store.add_item(user_id, item))
    return item


class TestDeferredMark:
    """标记延迟落盘：collect 只挑选，mark 由调用方在触发成功后调用"""

    def test_default_collect_does_not_persist(self, tmp_path):
        """默认（不传 mark_triggered）只挑选不落盘 —— 安全默认，忘传参数也不会丢提醒"""
        store = _store(tmp_path)
        item = _add(store, time=_at(5))

        due = asyncio.run(collect_due_schedule_items(store, "u", 10))
        assert len(due) == 1
        assert due[0]["item_id"] == item.id

        revived = asyncio.run(store.list_all_items("u"))[0]
        assert revived.last_triggered is None  # 未落盘

    def test_failed_trigger_keeps_reminder_available(self, tmp_path):
        """触发失败的那一轮不落标记 → 下一轮扫描仍能选到（不再永久丢失）"""
        store = _store(tmp_path)
        _add(store, time=_at(5))

        # 第一轮：开口失败，调用方不调用 mark
        first = asyncio.run(collect_due_schedule_items(store, "u", 10))
        assert len(first) == 1

        # 第二轮（用户回到活跃 stream）：仍能提醒
        second = asyncio.run(collect_due_schedule_items(store, "u", 10))
        assert len(second) == 1
        assert second[0]["item_id"] == first[0]["item_id"]

    def test_mark_after_success_persists_and_dedups(self, tmp_path):
        store = _store(tmp_path)
        _add(store, time=_at(5))

        due = asyncio.run(collect_due_schedule_items(store, "u", 10))
        asyncio.run(mark_schedule_items_triggered(store, "u", due))

        revived = asyncio.run(store.list_all_items("u"))[0]
        assert revived.last_triggered
        # 落标记后即防重
        assert asyncio.run(collect_due_schedule_items(store, "u", 10)) == []

    def test_mark_is_idempotent(self, tmp_path):
        """已打过标记的条目不重复回写，保留先落盘的时间戳（并发/重复调用的幂等）"""
        store = _store(tmp_path)
        item = _add(store, time=_at(5))
        due = asyncio.run(collect_due_schedule_items(store, "u", 10))

        # 标记落盘前，另一条路径已给同一条目打上更早的标记
        revived = asyncio.run(store.list_all_items("u"))[0]
        revived.last_triggered = "2026-01-01T00:00:00"
        asyncio.run(store.update_item("u", revived))

        asyncio.run(mark_schedule_items_triggered(store, "u", due))
        after = asyncio.run(store.list_all_items("u"))[0]
        assert after.id == item.id
        assert after.last_triggered == "2026-01-01T00:00:00"  # 未被覆盖

    def test_mark_only_touches_listed_items(self, tmp_path):
        """只标记这一轮挑选出来的那一条，同用户的其他日程不受影响"""
        store = _store(tmp_path)
        due_item = _add(store, title="到点的", time=_at(5))
        other = _add(store, title="还早的", time=_at(300))
        due = asyncio.run(collect_due_schedule_items(store, "u", 10))
        assert [d["item_id"] for d in due] == [due_item.id]

        asyncio.run(mark_schedule_items_triggered(store, "u", due))
        items = {i.id: i for i in asyncio.run(store.list_all_items("u"))}
        assert items[due_item.id].last_triggered
        assert items[other.id].last_triggered is None

    def test_inline_mark_is_explicit_opt_in(self, tmp_path):
        """内联标记是显式选择：挑选即等于已提醒、没有回调环节的调用方才传 True"""
        store = _store(tmp_path)
        _add(store, time=_at(5))
        due = asyncio.run(
            collect_due_schedule_items(store, "u", 10, mark_triggered=True)
        )
        assert len(due) == 1
        assert asyncio.run(store.list_all_items("u"))[0].last_triggered


class _NullLogger:
    """SDK ctx.logger 替身：吞掉日志，不改变被测逻辑"""

    def debug(self, *a, **kw):
        pass

    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        pass


def _install_sdk_stub() -> None:
    """装最小 maibot_sdk 替身，只为能 import plugin.py（不模拟 SDK 行为）"""
    try:
        import maibot_sdk  # noqa: F401
    except ImportError:
        pass
    else:
        return  # 有真 SDK 就别装替身

    class _ConfigBase:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _Plugin:
        def __init__(self) -> None:
            self.ctx = types.SimpleNamespace(logger=_NullLogger())

    class _Field:
        def __init__(self, default=None, **kw):
            self.default = default

    class _ToolParamType:
        STRING = "string"

    sdk = types.ModuleType("maibot_sdk")
    sdk.__path__ = []
    sdk.Field = _Field
    sdk.MaiBotPlugin = _Plugin
    sdk.PluginConfigBase = _ConfigBase
    sdk.Tool = lambda *a, **kw: (lambda fn: fn)
    sdk_types = types.ModuleType("maibot_sdk.types")
    sdk_types.ToolParameterInfo = _ConfigBase
    sdk_types.ToolParamType = _ToolParamType
    sdk.types = sdk_types
    sys.modules.setdefault("maibot_sdk", sdk)
    sys.modules.setdefault("maibot_sdk.types", sdk_types)


_install_sdk_stub()

from schedule_assistant.plugin import ScheduleAssistantPlugin  # noqa: E402


class _FakeMessaging:
    def __init__(self, users):
        self._users = users

    async def resolve_target_users(self, include_known_users=False):
        return list(self._users)


def _make_plugin(tmp_path, users=("u",)):
    plugin = ScheduleAssistantPlugin()
    plugin.store = _store(tmp_path)
    plugin.messaging = _FakeMessaging(users)

    async def _noop_sync():
        return None

    async def _noop_services():
        return None

    plugin._apple_calendar_sync = _noop_sync
    plugin._ensure_services = _noop_services
    plugin._flat_config = lambda: {"schedule_reminder_minutes": 10}
    return plugin


class TestScheduleReminderScanWiring:
    """扫描编排：触发失败不落防重标记，触发成功才落"""

    def test_trigger_failure_does_not_mark(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        item = _add(plugin.store, "u", time=_at(5))

        calls = []

        async def fake_maisaka(user_id, items):
            calls.append([it["item_id"] for it in items])
            return False  # 用户不在活跃 stream

        plugin._schedule_reminder_maisaka = fake_maisaka
        asyncio.run(plugin._schedule_reminder_scan())

        assert calls == [[item.id]]  # 确实尝试过开口
        assert asyncio.run(plugin.store.list_all_items("u"))[0].last_triggered is None

        # 下一轮仍会再试（不再是永久丢失）
        asyncio.run(plugin._schedule_reminder_scan())
        assert len(calls) == 2

    def test_trigger_success_marks(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        item = _add(plugin.store, "u", time=_at(5))

        async def fake_maisaka(user_id, items):
            return True

        plugin._schedule_reminder_maisaka = fake_maisaka
        asyncio.run(plugin._schedule_reminder_scan())

        revived = asyncio.run(plugin.store.list_all_items("u"))[0]
        assert revived.id == item.id
        assert revived.last_triggered  # 成功才落盘

        # 已提醒过：下一轮不再触发
        calls = []

        async def spy(user_id, items):
            calls.append(items)
            return True

        plugin._schedule_reminder_maisaka = spy
        asyncio.run(plugin._schedule_reminder_scan())
        assert calls == []

    def test_trigger_exception_does_not_mark(self, tmp_path):
        """开口抛异常同样算失败：标记不落盘，异常被扫描层的 except 兜住"""
        plugin = _make_plugin(tmp_path)
        _add(plugin.store, "u", time=_at(5))

        async def boom(user_id, items):
            raise RuntimeError("boom")

        plugin._schedule_reminder_maisaka = boom
        asyncio.run(plugin._schedule_reminder_scan())
        assert asyncio.run(plugin.store.list_all_items("u"))[0].last_triggered is None
