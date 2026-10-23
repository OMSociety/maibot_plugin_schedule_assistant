"""日程提醒防重标记与插件扫描编排的测试。

覆盖两根对外不变量：
1. 防重标记只在「提醒确实发出去了」之后落盘 —— 先落标记再触发时，
   Maisaka 开口失败（最常见是用户当前不在活跃 stream）会让该日程本期永久不再提醒；
2. 插件 _schedule_reminder_scan 的编排顺序：触发失败不落标记、触发成功才落标记。

plugin.py 依赖 maibot_sdk（插件运行依赖）。本文件在 SDK 缺失时用最小替身撑住导入，
替身只提供导入与构造所需接口，不模拟 SDK 行为；装有真 SDK 的环境走真包。

两种环境下插件都拿不到 Runner 注入的上下文，因此 _make_plugin 统一补一份 ctx
（真 SDK 的 ctx 是读 _ctx 的 property，未经注入即抛 RuntimeError；替身则在构造时
自带一个），使日志调用在两种环境下都成立。
"""

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone

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
    sdk.Tool = lambda *a, **kw: lambda fn: fn
    sdk_types = types.ModuleType("maibot_sdk.types")
    sdk_types.ToolParameterInfo = _ConfigBase
    sdk_types.ToolParamType = _ToolParamType
    sdk.types = sdk_types
    sys.modules.setdefault("maibot_sdk", sdk)
    sys.modules.setdefault("maibot_sdk.types", sdk_types)


_install_sdk_stub()

from schedule_assistant.plugin import ScheduleAssistantPlugin  # noqa: E402


class _FakeMessaging:
    def __init__(self, users, send_ok=True):
        self._users = users
        self._send_ok = send_ok
        self.sent: list[tuple[str, str]] = []

    async def resolve_target_users(self, include_known_users=False):
        return list(self._users)

    async def send_to_user(self, user_id, message, platform_id=None, markdown=None):
        self.sent.append((user_id, message))
        return self._send_ok


def _make_plugin(tmp_path, users=("u",), send_ok=True):
    plugin = ScheduleAssistantPlugin()
    # 插件日志走 self.ctx.logger：真 SDK 的 ctx 是读 _ctx 的 property，不注入即抛
    # RuntimeError；替身的 ctx 在构造时已自备。这里统一补上，两种环境行为一致。
    plugin._ctx = types.SimpleNamespace(logger=_NullLogger())
    plugin.store = _store(tmp_path)
    plugin.messaging = _FakeMessaging(users, send_ok=send_ok)

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

    def test_trigger_success_waits_for_delivery(self, tmp_path):
        """宿主接受了这次主动聊天请求 ≠ 麦麦开口：先记账，防重标记等确认送达"""
        plugin = _make_plugin(tmp_path)
        item = _add(plugin.store, "u", time=_at(5))

        async def fake_maisaka(user_id, items):
            return True

        plugin._schedule_reminder_maisaka = fake_maisaka
        asyncio.run(plugin._schedule_reminder_scan())

        revived = asyncio.run(plugin.store.list_all_items("u"))[0]
        assert revived.id == item.id
        assert revived.last_triggered is None  # 还没确认送达，不落盘
        assert f"u:{item.id}" in plugin._reminder_pending

        # 已请求过：下一轮不再重复请麦麦开口
        calls = []

        async def spy(user_id, items):
            calls.append(items)
            return True

        plugin._schedule_reminder_maisaka = spy
        asyncio.run(plugin._schedule_reminder_scan())
        assert calls == []

    def test_silent_maisaka_gets_fallback(self, tmp_path):
        """等够一轮后仍没观测到麦麦外发 → 插件补发纯文本并落标记"""
        plugin = _make_plugin(tmp_path)
        item = _add(plugin.store, "u", time=_at(5))

        async def fake_maisaka(user_id, items):
            return True

        plugin._schedule_reminder_maisaka = fake_maisaka
        asyncio.run(plugin._schedule_reminder_scan())
        key = f"u:{item.id}"
        plugin._reminder_pending[key] = datetime.now() - timedelta(minutes=5)

        asyncio.run(plugin._schedule_reminder_scan())

        assert len(plugin.messaging.sent) == 1
        user_id, text = plugin.messaging.sent[0]
        assert user_id == "u"
        assert item.title in text  # 补发的是可读提醒，不是空壳
        assert asyncio.run(plugin.store.list_all_items("u"))[0].last_triggered
        assert key not in plugin._reminder_pending

    def test_spoken_maisaka_skips_fallback(self, tmp_path):
        """会话里已有麦麦的消息 → 不补发，但仍落标记关窗"""
        plugin = _make_plugin(tmp_path)
        item = _add(plugin.store, "u", time=_at(5))

        async def fake_maisaka(user_id, items):
            return True

        async def spoke(user_id, since):
            return True

        plugin._schedule_reminder_maisaka = fake_maisaka
        plugin._reminder_spoke_since = spoke
        asyncio.run(plugin._schedule_reminder_scan())
        plugin._reminder_pending[f"u:{item.id}"] = datetime.now() - timedelta(minutes=5)

        asyncio.run(plugin._schedule_reminder_scan())

        assert plugin.messaging.sent == []
        assert asyncio.run(plugin.store.list_all_items("u"))[0].last_triggered

    def test_fallback_send_failure_keeps_pending(self, tmp_path):
        """补发失败不落标记：宽限期内下一轮还能再试"""
        plugin = _make_plugin(tmp_path, send_ok=False)
        item = _add(plugin.store, "u", time=_at(5))

        async def fake_maisaka(user_id, items):
            return True

        plugin._schedule_reminder_maisaka = fake_maisaka
        asyncio.run(plugin._schedule_reminder_scan())
        key = f"u:{item.id}"
        plugin._reminder_pending[key] = datetime.now() - timedelta(minutes=5)

        asyncio.run(plugin._schedule_reminder_scan())

        assert len(plugin.messaging.sent) == 1  # 确实尝试过补发
        assert asyncio.run(plugin.store.list_all_items("u"))[0].last_triggered is None
        assert key in plugin._reminder_pending

    def test_fallback_disabled_keeps_trigger_marks(self, tmp_path):
        """关闭补发时回到旧口径：请求被宿主接受即视为已提醒"""
        plugin = _make_plugin(tmp_path)
        _add(plugin.store, "u", time=_at(5))

        async def fake_maisaka(user_id, items):
            return True

        plugin._schedule_reminder_maisaka = fake_maisaka
        plugin._flat_config = lambda: {
            "schedule_reminder_minutes": 10,
            "enable_schedule_reminder_fallback": False,
        }
        asyncio.run(plugin._schedule_reminder_scan())

        assert asyncio.run(plugin.store.list_all_items("u"))[0].last_triggered
        assert plugin.messaging.sent == []

    def test_trigger_exception_does_not_mark(self, tmp_path):
        """开口抛异常同样算失败：标记不落盘，异常被扫描层的 except 兜住"""
        plugin = _make_plugin(tmp_path)
        _add(plugin.store, "u", time=_at(5))

        async def boom(user_id, items):
            raise RuntimeError("boom")

        plugin._schedule_reminder_maisaka = boom
        asyncio.run(plugin._schedule_reminder_scan())
        assert asyncio.run(plugin.store.list_all_items("u"))[0].last_triggered is None


def _plugin_with_trigger(tmp_path, trigger):
    """插件 + 可注入的 maisaka.proactive.trigger 返回值"""
    plugin = _make_plugin(tmp_path)
    ctx = types.SimpleNamespace(
        logger=_NullLogger(),
        maisaka=types.SimpleNamespace(proactive=types.SimpleNamespace(trigger=trigger)),
    )
    # 真 SDK 的 ctx 是只读 property（读 _ctx），替身则在构造时赋值给 ctx：两边都设
    try:
        plugin.ctx = ctx
    except AttributeError:
        pass
    plugin._ctx = ctx

    async def _stream(user_id):
        return {"session_id": "stream-1"}

    plugin._get_user_stream = _stream
    return plugin


class TestTriggerResult:
    """_trigger_maisaka 读取宿主返回值：拒绝是 success=False 而不是抛异常"""

    def test_rejected_result_is_failure(self, tmp_path):
        async def trigger(**kwargs):
            return {"success": False, "error": "未找到已存在的聊天流: stream-1"}

        plugin = _plugin_with_trigger(tmp_path, trigger)
        assert (
            asyncio.run(plugin._trigger_maisaka("u", "提醒内容", "日程提醒")) is False
        )

    def test_accepted_result_is_success(self, tmp_path):
        async def trigger(**kwargs):
            return {"success": True, "task_id": "proactive:x:1", "queued": True}

        plugin = _plugin_with_trigger(tmp_path, trigger)
        assert asyncio.run(plugin._trigger_maisaka("u", "提醒内容", "日程提醒")) is True

    def test_missing_return_value_is_success(self, tmp_path):
        """旧 SDK 不返回任务详情时按入队成功处理（行为与升级前一致）"""

        async def trigger(**kwargs):
            return None

        plugin = _plugin_with_trigger(tmp_path, trigger)
        assert asyncio.run(plugin._trigger_maisaka("u", "提醒内容", "日程提醒")) is True


def _plugin_with_recent(tmp_path, result=None, raises=False):
    """插件 + 可注入的 message.get_recent 返回值，并替换聊天流查询"""
    plugin = _make_plugin(tmp_path)
    calls = []

    async def call_capability(name, **kwargs):
        calls.append((name, kwargs))
        if raises:
            raise RuntimeError("capability unavailable")
        return result

    ctx = types.SimpleNamespace(logger=_NullLogger(), call_capability=call_capability)
    try:
        plugin.ctx = ctx
    except AttributeError:
        pass
    plugin._ctx = ctx

    async def _stream(user_id):
        return {"session_id": "stream-1"}

    plugin._get_user_stream = _stream
    plugin.capability_calls = calls
    return plugin


def _msg(sender, ts):
    return {"timestamp": ts, "message_info": {"user_info": {"user_id": sender}}}


class TestReminderSpokeSince:
    """开口确认：会话里 since 之后出现「不是用户发的」消息才算麦麦开口"""

    def test_bot_message_counts_as_spoken(self, tmp_path):
        since = datetime.now() - timedelta(minutes=5)
        plugin = _plugin_with_recent(
            tmp_path, [_msg("bot-1", datetime.now().isoformat())]
        )
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is True

    def test_user_message_does_not_count(self, tmp_path):
        since = datetime.now() - timedelta(minutes=5)
        plugin = _plugin_with_recent(tmp_path, [_msg("u", datetime.now().isoformat())])
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is False

    def test_messages_before_since_ignored(self, tmp_path):
        """since 之前的旧消息不能冒充本次开口"""
        since = datetime.now() - timedelta(minutes=5)
        old = (datetime.now() - timedelta(minutes=60)).isoformat()
        plugin = _plugin_with_recent(tmp_path, [_msg("bot-1", old)])
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is False

    def test_utc_iso_timestamp_compared_in_local_time(self, tmp_path):
        """带时区的 ISO 时间要换算到本地再比，否则 UTC 会被当成 8 小时前"""
        since = datetime.now() - timedelta(minutes=5)
        utc_now = datetime.now(timezone.utc).isoformat()
        plugin = _plugin_with_recent(tmp_path, [_msg("bot-1", utc_now)])
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is True

    def test_epoch_timestamp_supported(self, tmp_path):
        since = datetime.now() - timedelta(minutes=5)
        plugin = _plugin_with_recent(
            tmp_path, [_msg("bot-1", datetime.now().timestamp())]
        )
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is True

    def test_bad_timestamp_ignored(self, tmp_path):
        since = datetime.now() - timedelta(minutes=5)
        plugin = _plugin_with_recent(tmp_path, [_msg("bot-1", "not-a-time")])
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is False

    def test_missing_sender_ignored(self, tmp_path):
        since = datetime.now() - timedelta(minutes=5)
        plugin = _plugin_with_recent(
            tmp_path, [{"timestamp": datetime.now().isoformat()}]
        )
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is False

    def test_empty_result_not_spoken(self, tmp_path):
        since = datetime.now() - timedelta(minutes=5)
        plugin = _plugin_with_recent(tmp_path, [])
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is False

    def test_failed_capability_not_spoken(self, tmp_path):
        """能力被拒（denied）时回 success=False：按「没看到」处理，宁可多补一条"""
        since = datetime.now() - timedelta(minutes=5)
        plugin = _plugin_with_recent(
            tmp_path, {"success": False, "error": "未获授权能力: message.get_recent"}
        )
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is False

    def test_capability_exception_not_spoken(self, tmp_path):
        since = datetime.now() - timedelta(minutes=5)
        plugin = _plugin_with_recent(tmp_path, raises=True)
        assert asyncio.run(plugin._reminder_spoke_since("u", since)) is False

    def test_queries_recent_messages_of_stream(self, tmp_path):
        """查询参数对齐宿主能力契约：chat_id=聊天流，hours 为整数且在窗口内"""
        since = datetime.now() - timedelta(minutes=5)
        plugin = _plugin_with_recent(tmp_path, [])
        asyncio.run(plugin._reminder_spoke_since("u", since))

        assert len(plugin.capability_calls) == 1
        name, kwargs = plugin.capability_calls[0]
        assert name == "message.get_recent"
        assert kwargs["chat_id"] == "stream-1"
        assert isinstance(kwargs["hours"], int) and kwargs["hours"] >= 1
