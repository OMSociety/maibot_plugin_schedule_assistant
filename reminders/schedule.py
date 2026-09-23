"""
日程提醒模块（MaiBot 插件版）

只负责「谁该被提醒、何时算到点」：扫描 schedule 类型日程、判定提前量窗口、
写防重标记，并把到点事件打包成 intent 文本。habit 类型（洗澡/睡觉/喝水）由
独立定时任务处理，全天事件不提前提醒，均不在此扫描。

措辞与发送不在本模块：插件侧把 intent 注入 Maisaka 回复生命周期拟人开口
（见 plugin.py 的 _schedule_reminder_scan / _schedule_reminder_maisaka）。
"""

import logging
from datetime import datetime
from typing import Any

from ..constants import LOG_PREFIX

logger = logging.getLogger(__name__)


def parse_item_time(time_str: str) -> datetime | None:
    """解析日程时间字符串，支持 ISO 格式、时区后缀和普通格式"""
    if not time_str:
        return None
    s = time_str.strip()
    # 优先使用 fromisoformat（原生支持 ISO 8601，含时区）
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, TypeError):
        pass
    # 再尝试普通格式
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%H:%M"]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _is_all_day_event(item) -> bool:
    """判断是否为全天事件"""
    # 优先检查 all_day 标记
    if getattr(item, "all_day", False):
        return True
    # 检查时间格式：YYYY-MM-DD 表示全天
    t = (item.time or "").strip()
    if len(t) == 10 and t.count("-") == 2:
        try:
            datetime.strptime(t, "%Y-%m-%d")
            return True
        except ValueError:
            pass
    return False


async def collect_due_schedule_items(
    schedule_store,
    user_id: str,
    minutes_before: int = 15,
) -> list[dict[str, Any]]:
    """选出即将开始的日程（仅 schedule 类型），并写防重标记。

    提醒时机：开始前 minutes_before 分钟内（0 < 剩余分钟 <= minutes_before）
    触发一次；整个提前量窗口都有效，配合任意扫描间隔都不会漏掉窗口内的事件。

    防重：last_triggered 持久化，同一事件只提醒一次（重启不重发）；事件改期会
    重置该标记——Apple 同步（schedule_store.sync_from_apple_calendar）与工具
    修改（tools/schedule_tools.update_schedule）同口径。

    habit（洗澡/睡觉/喝水）、全天事件、已停用条目不提醒。

    Returns:
        list[dict]: 到点事件（item_id/title/start/end/minutes_until/context/source）
    """
    now = datetime.now()
    due: list[dict[str, Any]] = []

    for item in await schedule_store.list_all_items(user_id):
        if not item.enabled or item.type == "habit":
            continue
        if _is_all_day_event(item):
            continue
        if item.last_triggered:
            continue

        item_dt = parse_item_time(item.time)
        if not item_dt:
            continue

        minutes_until = (item_dt - now).total_seconds() / 60
        if not 0 < minutes_until <= minutes_before:
            continue

        end_dt = parse_item_time(item.end_time) if item.end_time else None
        due.append(
            {
                "item_id": item.id,
                "title": item.title,
                "start": item_dt.strftime("%H:%M"),
                "end": end_dt.strftime("%H:%M") if end_dt else "",
                "minutes_until": int(minutes_until),
                "context": (item.context or "").strip(),
                "source": "apple" if item.apple_uid else "local",
            }
        )
        logger.debug(
            f"{LOG_PREFIX} 日程到点提醒: {item.title} ({item.time}) "
            f"剩余 {int(minutes_until)} 分钟"
        )
        item.last_triggered = now.isoformat()
        await schedule_store.update_item(user_id, item)

    return due


def build_schedule_reminder_intent(items: list[dict[str, Any]]) -> str:
    """把到点日程打包成一条 Maisaka intent（多事件合并为一次开口，避免刷屏）"""
    lines = []
    for it in items:
        title = (it.get("title") or "").strip() or "无标题"
        start = it.get("start") or ""
        end = it.get("end") or ""
        time_label = f"{start}-{end}" if start and end else start
        minutes = it.get("minutes_until") or 0
        if minutes > 0:
            timing = f"{time_label} 开始（约 {minutes} 分钟后）"
        else:
            timing = f"{time_label} 马上开始"
        line = f"- 「{title}」{timing}"
        context = (it.get("context") or "").strip()
        if context:
            line += f"｜{context}"
        lines.append(line)
    return (
        "日程提醒：以下日程快开始了，请自然地随口提醒用户"
        "（简短口语化，可带关切）：\n" + "\n".join(lines)
    )
