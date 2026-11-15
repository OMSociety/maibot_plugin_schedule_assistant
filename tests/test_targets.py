"""目标解析与 Maisaka 触发。"""

import asyncio

from schedule_assistant.targets import (
    extract_stream_id,
    parse_user_target,
    trigger_proactive,
)


class FakeProactive:
    def __init__(self, result=None, error=None):
        self.result = {"stream_id": "s1", "queued": True} if result is None else result
        self.error = error
        self.calls = []

    async def trigger(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs)
        return self.result


class FakeChat:
    def __init__(self, stream=None, error=None):
        self.stream = stream
        self.error = error
        self.calls = []

    async def get_stream_by_user_id(self, user_id, platform="qq"):
        if self.error:
            raise self.error
        self.calls.append((platform, user_id))
        return self.stream


class FakeCtx:
    def __init__(self, stream=None, result=None, error=None):
        self.chat = FakeChat(stream=stream)
        self.maisaka = type(
            "Maisaka", (), {"proactive": FakeProactive(result=result, error=error)}
        )()


def test_parse_user_target_splits_platform():
    assert parse_user_target("telegram:42") == ("telegram", "42")
    assert parse_user_target("qq:10001") == ("qq", "10001")


def test_parse_user_target_defaults_to_qq():
    assert parse_user_target("10001") == ("qq", "10001")
    assert parse_user_target("") == ("qq", "")
    assert parse_user_target(None) == ("qq", "")


def test_extract_stream_id_from_dict_and_object():
    assert extract_stream_id({"stream_id": "a"}) == "a"
    assert extract_stream_id({"session_id": "b"}) == "b"
    assert extract_stream_id(type("S", (), {"stream_id": "c"})()) == "c"
    assert extract_stream_id(None) == ""


def test_trigger_proactive_success():
    ctx = FakeCtx(stream={"stream_id": "s1"})
    ok = asyncio.run(trigger_proactive(ctx, "qq:10001", "intent", "reason"))
    assert ok is True
    assert ctx.chat.calls == [("qq", "10001")]
    assert ctx.maisaka.proactive.calls[0]["stream_id"] == "s1"


def test_trigger_proactive_without_stream_returns_false():
    ctx = FakeCtx(stream=None)
    assert asyncio.run(trigger_proactive(ctx, "qq:10001", "intent", "reason")) is False


def test_trigger_proactive_exception_returns_false():
    ctx = FakeCtx(stream={"stream_id": "s1"}, error=RuntimeError("boom"))
    assert asyncio.run(trigger_proactive(ctx, "qq:10001", "intent", "reason")) is False


def test_trigger_proactive_rejected_returns_false():
    ctx = FakeCtx(
        stream={"stream_id": "s1"}, result={"success": False, "error": "busy"}
    )
    assert asyncio.run(trigger_proactive(ctx, "qq:10001", "intent", "reason")) is False
