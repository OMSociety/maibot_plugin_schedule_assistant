"""MessagingService 纯逻辑测试：目标用户归一化、平台映射还原、发送降级。

ctx（chat/send/logger）全部用假对象注入，不依赖 maibot_sdk。
"""

import asyncio
import types

from schedule_assistant.messaging import (
    MessagingService,
    extract_stream_id,
    parse_user_target,
)


class _Logger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))

    def info(self, msg, *a, **k):
        pass

    def debug(self, msg, *a, **k):
        pass


def _make_plugin(chat=None, send=None):
    return types.SimpleNamespace(
        ctx=types.SimpleNamespace(chat=chat, send=send, logger=_Logger())
    )


class TestParseUserTarget:
    """operator 格式解析：platform:id / 裸 ID"""

    def test_prefixed(self):
        assert parse_user_target("qq:123456") == ("qq", "123456")

    def test_bare_uses_default_platform(self):
        assert parse_user_target("123456") == ("qq", "123456")

    def test_bare_custom_default(self):
        assert parse_user_target("abc", default_platform="telegram") == (
            "telegram",
            "abc",
        )

    def test_empty_platform_falls_back(self):
        assert parse_user_target(":123") == ("qq", "123")

    def test_empty_target(self):
        assert parse_user_target("") == ("qq", "")
        assert parse_user_target(None) == ("qq", "")

    def test_first_colon_splits(self):
        """按第一个 : 切分（ID 内一般不含 :）"""
        assert parse_user_target("qq:12:34") == ("qq", "12:34")


class TestExtractStreamId:
    """get_stream_by_user_id 返回 dict 而非对象（曾致发送静默失败）"""

    def test_dict_session_id(self):
        assert extract_stream_id({"session_id": "s1"}) == "s1"

    def test_dict_stream_id_fallback(self):
        assert extract_stream_id({"stream_id": "s2"}) == "s2"

    def test_object_attrs(self):
        stream = types.SimpleNamespace(stream_id="s3")
        assert extract_stream_id(stream) == "s3"

    def test_none_and_empty(self):
        assert extract_stream_id(None) == ""
        assert extract_stream_id({}) == ""


class TestPlatformByUser:
    """配置 user_ids 的 platform:id 登记到 _platform_by_user（裸 ID → 平台）"""

    def test_registration(self):
        """带前缀的按登记平台；裸 ID 登记默认平台 qq"""
        svc = MessagingService(
            _make_plugin(), {"user_ids": ["qq:a", "telegram:b", "c", ""]}
        )
        assert svc._platform_by_user == {"a": "qq", "b": "telegram", "c": "qq"}

    def test_no_duplicate_overwrite(self):
        """同一裸 ID 多条配置时保留首个登记"""
        svc = MessagingService(_make_plugin(), {"user_ids": ["qq:a", "telegram:a"]})
        assert svc._platform_by_user == {"a": "qq"}


class TestResolveTargetUsers:
    """目标用户解析：配置 + 默认用户 + 已知用户，全部归一化为裸 ID 并去重"""

    def test_config_ids_normalized(self):
        svc = MessagingService(_make_plugin(), {"user_ids": ["qq:1", "2"]})
        assert asyncio.run(svc.resolve_target_users()) == ["1", "2"]

    def test_include_known_users(self):
        async def lookup():
            return ["qq:3", "4", "", "3"]

        svc = MessagingService(
            _make_plugin(),
            {"user_ids": ["qq:1"]},
            users_lookup=lookup,
        )
        assert asyncio.run(svc.resolve_target_users(include_known_users=True)) == [
            "1",
            "3",
            "4",
        ]

    def test_lookup_failure_keeps_config_ids(self):
        async def lookup():
            raise RuntimeError("boom")

        plugin = _make_plugin()
        svc = MessagingService(plugin, {"user_ids": ["qq:1"]}, users_lookup=lookup)
        assert asyncio.run(svc.resolve_target_users(include_known_users=True)) == ["1"]
        assert plugin.ctx.logger.warnings


class TestSendToUser:
    """发送路径：stream 提取、markdown 降级、无聊天流失败"""

    def _chat(self, stream={"session_id": "stream-1"}):
        class _Chat:
            def __init__(self):
                self.calls = []

            async def get_stream_by_user_id(self, uid, platform=None):
                self.calls.append((uid, platform))
                return stream

        return _Chat()

    class _Send:
        def __init__(self):
            self.texts = []
            self.customs = []

        async def text(self, text, stream_id):
            self.texts.append((text, stream_id))
            return True

        async def custom(self, kind, payload, stream_id):
            self.customs.append((kind, payload, stream_id))
            return True

    def test_plain_text_success(self):
        chat, send = self._chat(), self._Send()
        svc = MessagingService(
            _make_plugin(chat, send),
            {"user_ids": ["qq:1"], "markdown_enabled": False},
        )
        ok = asyncio.run(svc.send_to_user("1", "早上好"))
        assert ok is True
        assert chat.calls == [("1", "qq")]
        assert send.texts == [("早上好", "stream-1")]
        assert send.customs == []

    def test_markdown_success_no_fallback(self):
        """markdown 开启且 custom 成功时不发纯文本"""
        chat, send = self._chat(), self._Send()
        svc = MessagingService(
            _make_plugin(chat, send), {"user_ids": ["qq:1"], "markdown_enabled": True}
        )
        assert asyncio.run(svc.send_to_user("1", "早上好")) is True
        assert send.texts == []
        assert len(send.customs) == 1
        assert send.customs[0][0] == "qq_markdown"

    def test_markdown_success_and_fallback(self):
        """markdown 发送异常时降级纯文本（修复回归：异常路径也要降级）"""

        class _FailingCustomSend(self._Send):
            async def custom(self, kind, payload, stream_id):
                raise RuntimeError("send failed")

        chat = self._chat()
        send = _FailingCustomSend()
        svc = MessagingService(
            _make_plugin(chat, send), {"user_ids": ["qq:1"], "markdown_enabled": True}
        )
        ok = asyncio.run(svc.send_to_user("1", "早上好"))
        assert ok is True
        assert send.texts == [("早上好", "stream-1")]

    def test_markdown_disabled_sends_plain(self):
        chat, send = self._chat(), self._Send()
        svc = MessagingService(
            _make_plugin(chat, send),
            {"user_ids": ["qq:1"], "markdown_enabled": False},
        )
        assert asyncio.run(svc.send_to_user("1", "hi")) is True
        assert send.customs == []
        assert len(send.texts) == 1

    def test_no_stream_fails(self):
        chat, send = self._chat(stream=None), self._Send()
        plugin = _make_plugin(chat, send)
        svc = MessagingService(plugin, {"user_ids": ["qq:1"]})
        ok = asyncio.run(svc.send_to_user("1", "hi"))
        assert ok is False
        assert send.texts == []
        assert plugin.ctx.logger.warnings
