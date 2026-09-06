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


def parse_user_target(target: Any, default_platform: str = "qq") -> tuple[str, str]:
    """解析 user_ids 每一项，返回 (平台名, 裸用户ID)。

    支持两种写法（同全局 operator/permission 的 `platform:id`）：
    - ``qq:123456``（自包含，推荐；`:` 前是平台名）
    - ``123456``（裸 ID，沿用 `default_platform` 作为平台，兜底旧配置）
    平台/ID 内一般不含 `:`（QQ openid 十六进制、QQ 号数字、telegram 数字），
    按**第一个** `:` 切分即可。
    """
    t = str(target or "").strip()
    if not t:
        return str(default_platform or "qq"), ""
    if ":" in t:
        platform, user_id = t.split(":", 1)
        return (str(platform).strip() or str(default_platform or "qq")), str(
            user_id
        ).strip()
    return str(default_platform or "qq"), t


class MessagingService:
    """消息发送服务（MaiBot 版：user_id → 聊天流 → ctx.send）"""

    def __init__(
        self,
        ctx,
        config: dict,
        users_lookup=None,
        default_user_id: str | None = None,
    ):
        """
        Args:
            ctx: MaiBot 插件实例（提供 self.ctx.chat / self.ctx.send / self.ctx.logger）
            config: 插件配置
            users_lookup: 可选异步回调 () -> list[str]，返回所有已知用户ID
            default_user_id: 可选默认目标用户ID
        """
        self._ctx = ctx
        self.config = config
        self._users_lookup = users_lookup
        self._default_user_id = str(default_user_id) if default_user_id else None
        # 配置 user_ids 的 platform:id 项登记到这里（裸ID → 平台），
        # 存储/业务一律用裸 ID 作键，发送时按此还原平台。
        # 注意必须遍历配置原始值（platform:id），归一化后平台信息就丢了
        self._platform_by_user: dict[str, str] = {}
        for uid in config.get("user_ids", []) or []:
            if not uid:
                continue
            platform, bare = parse_user_target(uid)
            if bare:
                self._platform_by_user.setdefault(bare, platform)

    # ============ 目标用户解析（保留原接口） ============

    @staticmethod
    def _collect_config_target_ids(config: dict) -> list[str]:
        """读取目标用户名单配置（user_ids 列表），归一化为裸用户 ID。

        裸 ID 是与 @Tool 写入路径一致的存储键；平台前缀由
        _platform_by_user 保留、发送时还原。
        """
        raw = config.get("user_ids", []) or []
        ids = []
        for uid in raw:
            if not uid:
                continue
            _, bare = parse_user_target(uid)
            if bare:
                ids.append(bare)
        return ids

    async def resolve_target_users(
        self, include_known_users: bool = False
    ) -> list[str]:
        """解析目标用户ID列表（配置 user_ids + 默认用户 + 已知用户，去重排序）

        返回裸用户 ID（存储键统一口径）。

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
            _, bare = parse_user_target(self._default_user_id)
            if bare:
                user_ids.add(bare)
        if include_known_users and self._users_lookup:
            try:
                for uid in await self._users_lookup():
                    if not uid:
                        continue
                    _, bare = parse_user_target(uid)
                    if bare:
                        user_ids.add(bare)
            except Exception as e:
                self._ctx.ctx.logger.warning(f"{LOG_PREFIX} 读取已知用户失败: err={e}")
        return sorted(user_ids)

    # ============ 发送 ============

    def _enabled_markdown(self) -> bool:
        """是否启用 markdown 渲染（config markdown_enabled）"""
        return bool(self.config.get("markdown_enabled", True))

    async def _send_to_stream(self, stream, text: str, markdown: bool | None) -> bool:
        """向聊天流发送文本（markdown 优先，降级纯文本）

        Args:
            markdown: None 跟随配置；True/False 显式指定本次行为
        """
        stream_id = extract_stream_id(stream)
        if not stream_id:
            self._ctx.ctx.logger.warning(
                f"{LOG_PREFIX} 无法从聊天流解析 stream_id，跳过发送"
            )
            return False
        use_md = self._enabled_markdown() if markdown is None else markdown
        if not use_md:
            try:
                return bool(await self._ctx.ctx.send.text(text, stream_id))
            except Exception as e:
                self._ctx.ctx.logger.warning(
                    f"{LOG_PREFIX} 发送失败 stream={stream_id} err={e}"
                )
                return False
        try:
            ok = await self._ctx.ctx.send.custom(
                "qq_markdown",
                {"markdown": {"content": text}},
                stream_id,
            )
            if ok:
                return True
        except Exception as e:
            # custom 抛异常与返回 falsy 一样走纯文本降级
            self._ctx.ctx.logger.debug(
                f"{LOG_PREFIX} markdown 发送异常，降级纯文本 stream={stream_id} err={e}"
            )
        try:
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
            user_id: 目标用户ID（裸 ID，或 platform:id 格式——后者优先取平台）
            message: 要发送的消息文本
            platform_id: 兼容保留（MaiBot 版不使用）
            markdown: 是否启用 markdown，None 时跟随配置

        Returns:
            bool: 是否发送成功
        """
        try:
            platform, user_id = parse_user_target(user_id)
            # 配置里登记过平台映射的（qq:123456 写法），按登记还原
            platform = self._platform_by_user.get(user_id, platform)
            stream = await self._ctx.ctx.chat.get_stream_by_user_id(
                str(user_id), platform=platform
            )
            if not stream:
                self._ctx.ctx.logger.warning(
                    f"{LOG_PREFIX} 未找到用户聊天流: user={user_id}（用户可能未私聊过 bot）"
                )
                return False
            return await self._send_to_stream(stream, message, markdown)
        except Exception as e:
            self._ctx.ctx.logger.error(
                f"{LOG_PREFIX} 发送消息异常: user={user_id} err={e}"
            )
            return False
