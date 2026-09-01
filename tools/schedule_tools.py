"""日程管理工具核心逻辑（MaiBot 插件版）

纯逻辑函数：创建/删除/查看/修改日程。@Tool 装饰器在 plugin.py 定义，
本模块提供实现，接收 (plugin, 参数..., message dict)。

与 AstrBot 版的差异：
- 用户身份：从 message dict 提取（AstrBot 用 event.get_sender_id）
- Apple 日历写入/删除：通过 plugin 实例（保留原逻辑）
"""

import logging
from datetime import datetime, timedelta

from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta

from ..schedule_store import ScheduleItem

logger = logging.getLogger(__name__)


def _extract_user_id(plugin, message: dict | None) -> str:
    """从消息 dict 提取用户 ID（兜底用插件默认用户）"""
    if message and isinstance(message, dict):
        user_info = message.get("user_info") or {}
        uid = user_info.get("user_id") or ""
        if uid:
            return str(uid)
    # 兜底：配置的默认用户
    try:
        if plugin.config and plugin.config.basic and plugin.config.basic.user_ids:
            return str(plugin.config.basic.user_ids[0])
    except Exception:
        pass
    return ""


def _parse_datetime(datetime_str: str) -> datetime | None:
    """解析自然语言时间（明天9点/后天下午3点/今天/具体日期）"""
    now = datetime.now()
    datetime_str = (datetime_str or "").strip()
    if not datetime_str:
        return None
    try:
        if "明天" in datetime_str:
            time_part = datetime_str.replace("明天", "").strip()
            dt = now + relativedelta(days=1)
            if time_part:
                t = datetime.strptime(
                    time_part.replace("点", ":00").replace("：", ":"), "%H:%M"
                )
                dt = dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            return dt
        elif "后天" in datetime_str:
            time_part = datetime_str.replace("后天", "").strip()
            dt = now + relativedelta(days=2)
            if time_part:
                t = datetime.strptime(
                    time_part.replace("点", ":00").replace("：", ":"), "%H:%M"
                )
                dt = dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            return dt
        elif "今天" in datetime_str:
            time_part = datetime_str.replace("今天", "").strip()
            dt = now
            if time_part:
                t = datetime.strptime(
                    time_part.replace("点", ":00").replace("：", ":"), "%H:%M"
                )
                dt = dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            return dt
        else:
            return date_parser.parse(datetime_str)
    except (ValueError, TypeError):
        return None


async def create_schedule(
    plugin, title: str, datetime_str: str, description: str, message: dict | None
) -> str:
    """创建日程"""
    try:
        title = (title or "").strip()
        datetime_str = (datetime_str or "").strip()
        if not title or not datetime_str:
            return "请提供日程标题和时间"

        dt = _parse_datetime(datetime_str)
        if dt is None:
            return "时间格式无法解析，请使用如「2024-01-15 14:30」「明天9点」"

        user_id = _extract_user_id(plugin, message)
        if not user_id:
            return "无法确定用户身份"

        item = ScheduleItem(
            type="schedule",
            title=title,
            time=dt.strftime("%Y-%m-%d %H:%M"),
            context=(description or "").strip(),
        )

        # Apple 日历写入（需开启同步且已配置）；记录返回的 UID 供删除时回写
        apple_msg = ""
        try:
            if (
                plugin.config.calendar_sync.enable_apple_calendar_sync
                and plugin.apple_calendar
            ):
                created_uid = await plugin.apple_calendar.create_event(
                    summary=title,
                    start=dt,
                    description=description or "",
                )
                if created_uid:
                    item.apple_uid = created_uid
                apple_msg = "，已同步到 Apple 日历"
        except Exception as e:
            logger.warning(f"Apple 日历写入失败: {e}")

        await plugin.store.add_item(user_id, item)

        return (
            f"已创建日程「{title}」，时间：{dt.strftime('%m-%d %H:%M')} ✅{apple_msg}"
        )
    except Exception as e:
        logger.error(f"创建日程失败: {e}")
        return f"创建日程失败: {e}"


async def delete_schedule(
    plugin, schedule_id: str, title_keyword: str, message: dict | None
) -> str:
    """删除日程"""
    try:
        schedule_id = (schedule_id or "").strip()
        title_keyword = (title_keyword or "").strip()
        if not schedule_id and not title_keyword:
            return "请提供日程ID或标题关键词"

        user_id = _extract_user_id(plugin, message)
        if not user_id:
            return "无法确定用户身份"

        # 先定位要删除的日程（拿 apple_uid 用于回写 Apple 日历）
        schedules_dict = await plugin.store.get_schedules(user_id)
        all_items = schedules_dict.get("schedules", []) + schedules_dict.get(
            "habits", []
        )
        target = None
        if schedule_id:
            target = next((s for s in all_items if s.id == schedule_id), None)
            if target is None:
                return "未找到指定日程"
        else:
            matches = [s for s in all_items if title_keyword in s.title]
            if not matches:
                return f"没有找到包含「{title_keyword}」的日程"
            elif len(matches) == 1:
                target = matches[0]
            else:
                lines = ["找到多个匹配日程，请提供更具体的信息："]
                for s in matches:
                    lines.append(f"  [{s.id}] {s.title} @ {s.time}")
                return "\n".join(lines)

        # 删除本地日程
        removed = await plugin.store.remove_item(user_id, target.id)
        if not removed:
            return "未找到指定日程"

        # 回写 Apple 日历（若该日程来自 Apple 且同步开启）
        apple_msg = ""
        if (
            target.apple_uid
            and plugin.config.calendar_sync.enable_apple_calendar_sync
            and plugin.apple_calendar
        ):
            try:
                ok = await plugin.apple_calendar.delete_event(target.apple_uid)
                apple_msg = "，已从 Apple 日历删除" if ok else ""
            except Exception as e:
                logger.warning(f"Apple 日历删除回写失败: {e}")

        return f"已删除日程「{target.title}」✅{apple_msg}"
    except Exception as e:
        logger.error(f"删除日程失败: {e}")
        return f"删除日程失败: {e}"


async def list_schedules(plugin, date: str, message: dict | None) -> str:
    """查看日程（date 缺省为今天；兼容 days 数字形式）"""
    try:
        user_id = _extract_user_id(plugin, message)
        if not user_id:
            return "无法确定用户身份"

        schedules_dict = await plugin.store.get_schedules(user_id)
        all_items = schedules_dict.get("schedules", []) + schedules_dict.get(
            "habits", []
        )

        # date 参数：YYYY-MM-DD 或 days 数字（如 "7"）
        days = 7
        if date:
            d = (date or "").strip()
            if d.isdigit():
                days = int(d)
                date_filter = None
            else:
                date_filter = d
                days = 0
        else:
            date_filter = None

        now = datetime.now()
        future = now + timedelta(days=days) if days > 0 else None

        user_schedules = []
        for s in all_items:
            if not s.time:
                continue
            try:
                dt = datetime.strptime(s.time, "%Y-%m-%d %H:%M")
            except Exception:
                continue
            if date_filter:
                if dt.strftime("%Y-%m-%d") == date_filter:
                    user_schedules.append((dt, s))
            elif now <= dt <= future:
                user_schedules.append((dt, s))

        if not user_schedules:
            if date_filter:
                return f"{date_filter} 没有日程安排~"
            return f"最近{days}天没有日程安排~"

        user_schedules.sort(key=lambda x: x[0])

        if date_filter:
            lines = [f"📋 {date_filter} 日程（共{len(user_schedules)}个）：", ""]
        else:
            lines = [f"📋 接下来{days}天日程（共{len(user_schedules)}个）：", ""]
        current_date = None
        for dt, s in user_schedules:
            date_str = dt.strftime("%m-%d")
            if date_str != current_date:
                current_date = date_str
                weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
                    dt.weekday()
                ]
                lines.append(f"━━━ {date_str} {weekday} ━━━")
            lines.append(f"  ⏰ {dt.strftime('%H:%M')} │ {s.title}")
            if s.context:
                lines.append(f"      📝 {s.context}")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"查看日程失败: {e}")
        return f"查看日程失败: {e}"


async def update_schedule(
    plugin, schedule_id: str, title: str, datetime_str: str, message: dict | None
) -> str:
    """修改日程（schedule_id 定位；title/datetime_str 为新值）"""
    try:
        schedule_id = (schedule_id or "").strip()
        new_title = (title or "").strip()
        new_datetime = (datetime_str or "").strip()
        if not schedule_id:
            return "请提供要修改的日程ID"
        if not new_title and not new_datetime:
            return "请提供要修改的内容（新标题/新时间）"

        user_id = _extract_user_id(plugin, message)
        if not user_id:
            return "无法确定用户身份"

        schedules_dict = await plugin.store.get_schedules(user_id)
        all_items = schedules_dict.get("schedules", []) + schedules_dict.get(
            "habits", []
        )
        matches = [s for s in all_items if s.id == schedule_id]
        if not matches:
            return "没有找到匹配的日程"
        target = matches[0]

        if new_title:
            target.title = new_title
        if new_datetime:
            dt = _parse_datetime(new_datetime)
            if dt is None:
                return "时间格式无法解析，请使用如「明天9点」「2024-01-15 14:30」"
            target.time = dt.strftime("%Y-%m-%d %H:%M")

        await plugin.store.update_item(user_id, target)

        changes = []
        if new_title:
            changes.append(f"标题改为「{new_title}」")
        if new_datetime:
            changes.append(f"时间改为{new_datetime}")
        return f"已修改日程：{', '.join(changes)} ✅"
    except Exception as e:
        logger.error(f"修改日程失败: {e}")
        return f"修改日程失败: {e}"
