"""目标用户与聊天流解析。

配置项 `user_ids` 的每项格式为 `platform:裸ID`（缺省平台 qq），
发送既可以把整串交给宿主，也可以自己拆出平台与裸 ID。
"""

from typing import Any

from .constants import LOG_PREFIX

DEFAULT_PLATFORM = "qq"


def parse_user_target(
    target: Any, default_platform: str = DEFAULT_PLATFORM
) -> tuple[str, str]:
    """把 `platform:裸ID` 拆成 (platform, 裸ID)；没有冒号时用默认平台。"""
    text = str(target or "").strip()
    if not text:
        return default_platform, ""
    if ":" in text:
        platform, bare = text.split(":", 1)
        platform = platform.strip() or default_platform
        return platform, bare.strip()
    return default_platform, text


def extract_stream_id(stream: Any) -> str:
    """从宿主返回的聊天流对象里取 stream_id（兼容 dict 与对象）。"""
    if isinstance(stream, dict):
        for key in ("stream_id", "session_id", "id"):
            value = stream.get(key)
            if value:
                return str(value)
        return ""
    for attr in ("stream_id", "session_id", "id"):
        value = getattr(stream, attr, None)
        if value:
            return str(value)
    return str(stream or "")


async def get_user_stream(ctx, target: Any) -> Any:
    """按目标用户查聊天流；用户需先与 bot 有过会话。"""
    platform, bare = parse_user_target(target)
    if not bare:
        return None
    try:
        return await ctx.chat.get_stream_by_user_id(bare, platform=platform)
    except Exception:
        return None


async def trigger_proactive(
    ctx,
    target: Any,
    intent: str,
    reason: str,
    logger=None,
    metadata: dict | None = None,
) -> bool:
    """请求 Maisaka 主动开口；返回宿主是否接受了这次主动任务。

    True 只表示宿主排上了一轮回复，不保证麦麦真的开口。
    """
    stream = await get_user_stream(ctx, target)
    stream_id = extract_stream_id(stream)
    if not stream_id:
        return False
    try:
        result = await ctx.maisaka.proactive.trigger(
            stream_id=stream_id,
            intent=intent,
            reason=reason,
            metadata=metadata or {"source": "schedule_assistant"},
        )
    except Exception as exc:
        if logger:
            logger.warning(f"{LOG_PREFIX} Maisaka 主动触发失败 target={target}: {exc}")
        return False
    if isinstance(result, dict) and result.get("success") is False:
        if logger:
            logger.warning(
                f"{LOG_PREFIX} Maisaka 主动触发被拒绝 target={target}: {result.get('error')}"
            )
        return False
    return True
