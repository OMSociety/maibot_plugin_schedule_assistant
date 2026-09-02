"""
统一消息发送模块（MaiBot 插件版）

MaiBot 无 UMO/多平台概念，主动推送 = 用户 ID → 聊天流（ctx.chat）→ 发送（ctx.send）。
保留原插件接口（resolve_target_users / send_to_user），供定时引擎与业务复用。

发送路径：
- markdown_enabled：ctx.send.custom("qq_markdown", {"markdown": {"content": ...}}, stream_id)
  （QQ 官方适配器支持，失败降级纯文本）
- 否则：ctx.send.text(text, stream_id)
"""

from typing import Any

from .constants import LOG_PREFIX

# 兼容旧引用（MaiBot 版无 UMO，MessageTarget 仅作简单封装保留）
COMMON_SESSION_TYPES = (
    "FriendMessage",
    "GroupMessage",
    "TempMessage",
    "ChannelMessage",
)


def extract_stream_id(stream: Any) -> str:
    """从 get_stream_by_user_id 返回的 stream 中提取 stream_id。

    MaiBot 的 get_stream_by_user_id 返回的是 dict（字段 session_id/stream_id），
    不是带 .stream_id 属性的对象；用属性访问会抛 AttributeError，导致发送静默失败。
    兼容 dict 与对象两种形态。
    """
    if not stream:
        return ""
    if isinstance(stream, dict):
        return str(stream.get("session_id") or stream.get("stream_id") or "")
    return str(
        getattr(stream, "stream_id", "") or getattr(stream, "session_id", "") or ""
    )


class MessagingService:
    """消息发送服务（MaiBot 版：user_id → 聊天流 → ctx.send）"""

    def __init__(
        self,
        ctx,
        config: dict,
        platform_lookup=None,
        users_lookup=None,
        default_user_id: str | None = None,
    ):
        """
        Args:
            ctx: MaiBot 插件实例（提供 self.ctx.chat / self.ctx.send / self.ctx.logger）
            config: 插件配置
            platform_lookup: 兼容保留（MaiBot 版不使用，平台由聊天流自带）
            users_lookup: 可选异步回调 () -> list[str]，返回所有已知用户ID
            default_user_id: 可选默认目标用户ID
        """
        self._ctx = ctx
        self.config = config
        self._users_lookup = users_lookup
        self._default_user_id = str(default_user_id) if default_user_id else None

    # ============ 目标用户解析（保留原接口） ============

    @staticmethod
    def _collect_config_target_ids(config: dict) -> list[str]:
        """读取目标用户名单配置（user_ids 列表）"""
        raw = config.get("user_ids", []) or []
        return [str(uid) for uid in raw if uid]

    async def resolve_target_users(
        self, include_known_users: bool = False
    ) -> list[str]:
        """解析目标用户ID列表（配置 user_ids + 默认用户 + 已知用户，去重排序）

        Args:
            include_known_users: 是否包含存储中的全部已知用户
                （定时任务如日程扫描/Apple 同步固定传 True）

        Returns:
            list[str]: 去重排序后的目标用户ID列表
        """
        user_ids: set[str] = set()
        for uid in self._collect_config_target_ids(self.config):
            user_ids.add(str(uid))
        if self._default_user_id:
            user_ids.add(str(self._default_user_id))
        if include_known_users and self._users_lookup:
            try:
                for uid in await self._users_lookup():
                    if uid:
                        user_ids.add(str(uid))
            except Exception as e:
                self._ctx.ctx.logger.warning(f"{LOG_PREFIX} 读取已知用户失败: err={e}")
        return sorted(user_ids)

    # ============ 发送 ============

    def _enabled_markdown(self) -> bool:
        """是否启用 markdown 渲染（config markdown_enabled）"""
        return bool(self.config.get("markdown_enabled", True))

    async def _send_to_stream(self, stream, text: str) -> bool:
        """向聊天流发送文本（markdown 优先，降级纯文本）"""
        stream_id = extract_stream_id(stream)
        if not stream_id:
            self._ctx.ctx.logger.warning(
                f"{LOG_PREFIX} 无法从聊天流解析 stream_id，跳过发送"
            )
            return False
        try:
            if self._enabled_markdown():
                ok = await self._ctx.ctx.send.custom(
                    "qq_markdown",
                    {"markdown": {"content": text}},
                    stream_id,
                )
                if ok:
                    return True
                # 降级纯文本
                return bool(await self._ctx.ctx.send.text(text, stream_id))
            return bool(await self._ctx.ctx.send.text(text, stream_id))
        except Exception as e:
            self._ctx.ctx.logger.warning(
                f"{LOG_PREFIX} 发送失败 stream={stream_id} err={e}"
            )
            return False

    async def send_to_user(
        self,
        user_id: str,
        message: str,
        platform_id: str | None = None,
        markdown: bool | None = None,
    ) -> bool:
        """向指定用户发送私聊消息（user_id → 聊天流 → 发送）

        Args:
            user_id: 目标用户ID（QQ OpenID / 数字 QQ 号）
            message: 要发送的消息文本
            platform_id: 兼容保留（MaiBot 版不使用）
            markdown: 是否启用 markdown，None 时跟随配置

        Returns:
            bool: 是否发送成功
        """
        try:
            stream = await self._ctx.ctx.chat.get_stream_by_user_id(
                str(user_id), platform=self.config.get("platform", "qq")
            )
            if not stream:
                self._ctx.ctx.logger.warning(
                    f"{LOG_PREFIX} 未找到用户聊天流: user={user_id}（用户可能未私聊过 bot）"
                )
                return False
            if markdown is not None:
                old = self.config.get("markdown_enabled", True)
                self.config["markdown_enabled"] = markdown
                ok = await self._send_to_stream(stream, message)
                self.config["markdown_enabled"] = old
                return ok
            return await self._send_to_stream(stream, message)
        except Exception as e:
            self._ctx.ctx.logger.error(
                f"{LOG_PREFIX} 发送消息异常: user={user_id} err={e}"
            )
            return False

    # ============ 兼容接口（MaiBot 版简化/占位） ============

    def remember_user_platform(self, user_id: str, platform_id: str) -> None:
        """兼容保留（MaiBot 版无平台记忆需求，静默）"""

    def _extract_platform_id_from_event(self, event: Any) -> str | None:
        """兼容保留：MaiBot 消息事件无平台 ID 概念，返回 None"""
        return None
