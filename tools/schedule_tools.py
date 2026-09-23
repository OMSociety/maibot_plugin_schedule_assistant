"""日程管理工具核心逻辑（MaiBot 插件版）

纯逻辑函数：创建/删除/查看/修改日程。@Tool 装饰器在 plugin.py 定义，
本模块提供实现，接收 (plugin, 参数..., message dict)。

时间表达式支持三种形态（_parse_schedule_time 统一解析）：
- 单时间点：「2024-01-15 14:30」「明天9点」「今天晚上8点」（先前模式，保留）
- 时间区间：「明天9点到11点」「2026-09-10 09:00~11:00」，结束只给时刻时继承开始日期
- 全天日程：「明天全天」「2026-09-10」这类纯日期输入

与 AstrBot 版的差异：
- 用户身份：从 message dict 提取（AstrBot 用 event.get_sender_id）
- Apple 日历写入/删除：通过 plugin 实例（保留原逻辑）
"""

import logging
import re
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from ..messaging import parse_user_target
from ..reminders.schedule import parse_item_time
from ..schedule_store import ScheduleItem

logger = logging.getLogger(__name__)

# 中文星期 → weekday()（周一=0）
_WEEKDAY_MAP = {
    "周一": 0,
    "周二": 1,
    "周三": 2,
    "周四": 3,
    "周五": 4,
    "周六": 5,
    "周日": 6,
    "周天": 6,
    "星期一": 0,
    "星期二": 1,
    "星期三": 2,
    "星期四": 3,
    "星期五": 4,
    "星期六": 5,
    "星期日": 6,
    "星期天": 6,
}

# 时刻表达式：「9点」「3点半」「14:30」「下午3点」「晚上8点30」（可带秒）
_CLOCK_TOKEN = (
    r"(?:凌晨|上午|早上|早晨|中午|下午|傍晚|晚上|晚间)?\s*"
    r"\d{1,2}\s*(?::\d{1,2}(?::\d{2})?|：\d{1,2}|点半|点\s*\d{0,2})\s*分?"
)
_CLOCK_SPLIT_RE = re.compile(rf"^(.*?)\s*({_CLOCK_TOKEN})\s*$")
_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{1,2}))?(?::\d{2})?$")
# 「09:00-11:00」中缀连字符 → 「到」（日期里的连字符不受影响）
_HYPHEN_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})")
_RANGE_SEPS = ("到", "至", "~", " - ")


def _extract_user_id(plugin, message: dict | None) -> str:
    """从消息 dict 提取用户 ID（兜底用插件默认用户）"""
    if message and isinstance(message, dict):
        user_info = message.get("user_info") or {}
        uid = user_info.get("user_id") or ""
        if uid:
            return str(uid)
    # 兜底：配置的默认用户（可能为 platform:id，取裸 ID 作存储键）
    try:
        if plugin.config and plugin.config.basic and plugin.config.basic.user_ids:
            _, uid = parse_user_target(plugin.config.basic.user_ids[0], "qq")
            return uid
    except Exception:
        pass
    return ""


# ============ 时间解析（点 / 区间 / 全天） ============


def _parse_clock(text: str) -> tuple[int, int] | None:
    """解析时刻：「9点」「3点半」「14:30」「下午3点」「晚上8点30」→ (时, 分)"""
    s = (text or "").strip()
    if not s:
        return None
    period = None
    for kw, p in (
        ("凌晨", "am"),
        ("上午", "am"),
        ("早上", "am"),
        ("早晨", "am"),
        ("中午", "noon"),
        ("下午", "pm"),
        ("傍晚", "pm"),
        ("晚上", "pm"),
        ("晚间", "pm"),
    ):
        if kw in s:
            period = p
            s = s.replace(kw, "")
            break
    s = (
        s.replace("：", ":")
        .replace("点半", ":30")
        .replace("点", ":")
        .replace("分", "")
        .strip()
    )
    if s.endswith(":"):
        s += "00"
    m = _CLOCK_RE.match(s)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if minute > 59:
        return None
    if period in ("pm", "noon") and hour < 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return hour, minute


def _split_date_clock(text: str) -> tuple[str, str]:
    """拆分「日期部分」与「时刻部分」，返回 (date_part, clock_part)（可能为空串）"""
    m = _CLOCK_SPLIT_RE.match((text or "").strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (text or "").strip(), ""


def _parse_date_part(text: str) -> datetime | None:
    """解析日期部分为当天 0 点：今天/明天/后天/周X/X月X日/YYYY-MM-DD 等"""
    s = (text or "").strip()
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if not s:
        return today0
    rel_days = {"今天": 0, "明天": 1, "后天": 2}
    if s in rel_days:
        return today0 + relativedelta(days=rel_days[s])
    week = s.lstrip("下个本这")
    if week in _WEEKDAY_MAP:
        delta = (_WEEKDAY_MAP[week] - today0.weekday()) % 7
        return today0 + relativedelta(days=delta)
    norm = (
        s.replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("T", "")
        .replace("t", "")
        .strip()
    )
    for fmt in ("%Y-%m-%d", "%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            # %m-%d 补上当年年份再解析（strptime 无年份的 %m-%d 在新 Python 有歧义告警）
            dt = (
                datetime.strptime(f"{today0.year}-{norm}", "%Y-%m-%d")
                if fmt == "%m-%d"
                else datetime.strptime(norm, fmt)
            )
        except ValueError:
            continue
        if fmt == "%m-%d":
            if dt < today0:
                try:
                    dt = dt.replace(year=today0.year + 1)
                except ValueError:  # 次年无此日（如 2-29 落非闰年）
                    return None
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return None


def _parse_when(text: str) -> tuple[datetime | None, bool]:
    """解析单个时间表达式，返回 (datetime, 是否带时刻)。

    纯日期（「明天」「2026-09-10」）返回当天 0 点且 has_time=False；
    「明天9点」「14:30」返回具体时刻且 has_time=True。
    """
    s = (text or "").strip()
    if not s:
        return None, False
    date_part, clock_part = _split_date_clock(s)
    date0 = _parse_date_part(date_part)
    if date0 is None:
        return None, False
    if not clock_part:
        return date0, False
    clock = _parse_clock(clock_part)
    if clock is None:
        return None, False
    return date0.replace(hour=clock[0], minute=clock[1]), True


def _is_clock_only(text: str) -> bool:
    """是否是「只给时刻、没给日期」的表达（如「11点」）"""
    return not _split_date_clock(text or "")[0]


def _parse_range_end(text: str, start: datetime) -> datetime | None:
    """解析区间结束时间：只给时刻时继承开始日期，早于开始视为次日（「23点到1点」）"""
    end, _has_time = _parse_when(text)
    if end is None:
        return None
    if _is_clock_only(text):
        end = end.replace(year=start.year, month=start.month, day=start.day)
        if end < start:
            end += timedelta(days=1)
    return end


def _parse_schedule_time(
    datetime_str: str,
) -> tuple[datetime | None, datetime | None, bool]:
    """解析日程时间，返回 (开始, 结束, 是否全天)。

    - 区间：「明天9点到11点」「2026-09-10 09:00~11:00」→ (开始, 结束, False)
    - 全天：「明天」「明天全天」「2026-09-10」→ (当天 0 点, None, True)；
      两端纯日期的区间（「明天到后天」）同为全天，多日取开始日
    - 单点：「明天9点」→ (开始, None, False)
    """
    s = (datetime_str or "").strip().replace("整天", "全天")
    if not s:
        return None, None, False
    all_day_flag = "全天" in s
    s = s.replace("全天", " ").strip()
    s = _HYPHEN_RANGE_RE.sub(r"\1到\2", s)

    for sep in _RANGE_SEPS:
        if sep in s:
            left, right = s.split(sep, 1)
            left = left.strip().removeprefix("从")
            right = right.strip()
            start, start_has_time = _parse_when(left)
            if start is None:
                return None, None, False
            end, end_has_time = _parse_when(right)
            if end is None:
                return None, None, False
            if all_day_flag or (not start_has_time and not end_has_time):
                # 「全天」或两端纯日期（「明天到后天」）→ 全天；
                # 多日事件简化为开始日全天（与 Apple 同步展示口径一致）
                return (
                    start.replace(hour=0, minute=0, second=0, microsecond=0),
                    None,
                    True,
                )
            if _is_clock_only(right):
                end = end.replace(year=start.year, month=start.month, day=start.day)
                if end < start:
                    end += timedelta(days=1)
            # 结束不晚于开始由调用方报「结束时间需要晚于开始时间」
            return start, end, False

    when, has_time = _parse_when(s)
    if when is None:
        return None, None, False
    if all_day_flag or not has_time:
        return (
            when.replace(hour=0, minute=0, second=0, microsecond=0),
            None,
            True,
        )
    return when, None, False


def _format_when_label(start: datetime, end: datetime | None, all_day: bool) -> str:
    """回执时间标签：09-02 全天 / 09-02 15:00-16:30 / 09-02 15:00"""
    if all_day:
        return f"{start.strftime('%m-%d')} 全天"
    if end:
        if end.date() == start.date():
            return f"{start.strftime('%m-%d %H:%M')}-{end.strftime('%H:%M')}"
        return f"{start.strftime('%m-%d %H:%M')}→{end.strftime('%m-%d %H:%M')}"
    return f"{start.strftime('%m-%d %H:%M')}"


def _format_item_when(item) -> str:
    """日程条目的显示标签：📅 全天 / ⏰ 15:00-16:30 / ⏰ 15:00"""
    time_str = (getattr(item, "time", "") or "").strip()
    if getattr(item, "all_day", False) or len(time_str) == 10:
        return "📅 全天"
    start_dt = parse_item_time(time_str)
    end_dt = parse_item_time(item.end_time) if getattr(item, "end_time", None) else None
    if not start_dt:
        return f"⏰ {time_str}"
    if end_dt:
        if end_dt.date() == start_dt.date():
            return f"⏰ {start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"
        return f"⏰ {start_dt.strftime('%m-%d %H:%M')}→{end_dt.strftime('%m-%d %H:%M')}"
    return f"⏰ {start_dt.strftime('%H:%M')}"


# ============ 日程 CRUD ============


async def create_schedule(
    plugin,
    title: str,
    datetime_str: str,
    end_datetime_str: str,
    description: str,
    message: dict | None,
) -> str:
    """创建日程（单时间点 / 时间区间 / 全天）"""
    try:
        title = (title or "").strip()
        datetime_str = (datetime_str or "").strip()
        end_datetime_str = (end_datetime_str or "").strip()
        if not title or not datetime_str:
            return "请提供日程标题和时间"

        start, end, all_day = _parse_schedule_time(datetime_str)
        if start is None:
            return (
                "时间格式无法解析，请使用如「2024-01-15 14:30」「明天9点」"
                "「明天9点到11点」（区间）「明天全天」（全天）"
            )
        if end_datetime_str and not all_day:
            end = _parse_range_end(end_datetime_str, start)
            if end is None:
                return "结束时间格式无法解析，请使用如「11点」「2024-01-15 16:30」"
        if end is not None and end <= start:
            return "结束时间需要晚于开始时间"

        user_id = _extract_user_id(plugin, message)
        if not user_id:
            return "无法确定用户身份"

        item = ScheduleItem(
            type="schedule",
            title=title,
            time=(
                start.strftime("%Y-%m-%d")
                if all_day
                else start.strftime("%Y-%m-%d %H:%M")
            ),
            end_time=(
                None if all_day or end is None else end.strftime("%Y-%m-%d %H:%M")
            ),
            context=(description or "").strip(),
            all_day=all_day,
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
                    start=start,
                    end=end,
                    description=description or "",
                    all_day=all_day,
                )
                if created_uid:
                    item.apple_uid = created_uid
                apple_msg = "，已同步到 Apple 日历"
        except Exception as e:
            logger.warning(f"Apple 日历写入失败: {e}")

        await plugin.store.add_item(user_id, item)

        return f"已创建日程「{title}」，时间：{_format_when_label(start, end, all_day)} ✅{apple_msg}"
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
                    lines.append(f"  [{s.id}] {s.title} · {_format_item_when(s)}")
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
            dt = parse_item_time(s.time)
            if not dt:
                continue
            is_all_day = bool(s.all_day) or len((s.time or "").strip()) == 10
            if date_filter:
                if dt.strftime("%Y-%m-%d") == date_filter:
                    user_schedules.append((dt, s))
            elif (now <= dt <= future) or (
                is_all_day and future and now.date() <= dt.date() <= future.date()
            ):
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
            lines.append(f"  {_format_item_when(s)} │ {s.title}")
            if s.context:
                lines.append(f"      📝 {s.context}")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"查看日程失败: {e}")
        return f"查看日程失败: {e}"


async def update_schedule(
    plugin,
    schedule_id: str,
    title: str,
    datetime_str: str,
    end_datetime_str: str,
    message: dict | None,
) -> str:
    """修改日程（schedule_id 定位；title/datetime_str/end_datetime_str 为新值）"""
    try:
        schedule_id = (schedule_id or "").strip()
        new_title = (title or "").strip()
        new_datetime = (datetime_str or "").strip()
        new_end_datetime = (end_datetime_str or "").strip()
        if not schedule_id:
            return "请提供要修改的日程ID"
        if not new_title and not new_datetime and not new_end_datetime:
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

        changes = []
        if new_title:
            changes.append(f"标题改为「{new_title}」")

        if new_datetime or new_end_datetime:
            if not new_datetime:
                return "请一并提供开始时间，或直接用「9点到11点」的区间写法"
            start, end, all_day = _parse_schedule_time(new_datetime)
            if start is None:
                return (
                    "时间格式无法解析，请使用如「明天9点」"
                    "「明天9点到11点」（区间）「明天全天」（全天）"
                )
            if new_end_datetime and not all_day:
                end = _parse_range_end(new_end_datetime, start)
                if end is None:
                    return "结束时间格式无法解析，请使用如「11点」「2024-01-15 16:30」"
            if end is not None and end <= start:
                return "结束时间需要晚于开始时间"
            new_time = (
                start.strftime("%Y-%m-%d")
                if all_day
                else start.strftime("%Y-%m-%d %H:%M")
            )
            new_end = None if all_day or end is None else end.strftime("%Y-%m-%d %H:%M")
            if (target.time, target.end_time, target.all_day) != (
                new_time,
                new_end,
                all_day,
            ):
                target.time = new_time
                target.end_time = new_end
                target.all_day = all_day
                # 改期重新提醒（与 Apple 同步改期同口径）
                target.last_triggered = None
            changes.append(f"时间改为{_format_when_label(start, end, all_day)}")

        await plugin.store.update_item(user_id, target)
        return f"已修改日程：{', '.join(changes)} ✅"
    except Exception as e:
        logger.error(f"修改日程失败: {e}")
        return f"修改日程失败: {e}"
