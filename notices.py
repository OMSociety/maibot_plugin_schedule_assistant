"""推送内容里共用的格式化函数（只管文本，不做 IO）。"""

from datetime import datetime
from zoneinfo import ZoneInfo

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def weekday_label(moment: datetime) -> str:
    return WEEKDAYS[moment.weekday()]


def parse_item_time(value: str, tz: ZoneInfo | None = None) -> datetime | None:
    """解析日程时间；带时区的 ISO 串先换算到 tz 再转成朴素本地时间。"""
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is not None:
            if tz is not None:
                parsed = parsed.astimezone(tz)
            return parsed.replace(tzinfo=None)
        return parsed
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def is_all_day(item) -> bool:
    if getattr(item, "all_day", False):
        return True
    text = (getattr(item, "time", "") or "").strip()
    if len(text) == 10 and text.count("-") == 2:
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return True
        except ValueError:
            return False
    return False


def time_label(
    start: datetime, end_value: str | None, all_day: bool, tz: ZoneInfo | None = None
) -> str:
    """全天 / 09:00-10:30 / 09:00。"""
    if all_day:
        return "全天"
    if end_value:
        end = parse_item_time(end_value, tz)
        if end is not None:
            if end.date() == start.date():
                return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
            return f"{start.strftime('%m-%d %H:%M')}→{end.strftime('%m-%d %H:%M')}"
    return start.strftime("%H:%M")


def ahead_label(minutes: int) -> str:
    if minutes <= 0:
        return "马上开始"
    if minutes < 60:
        return f"{minutes} 分钟后开始"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分钟后开始" if rest else f"{hours} 小时后开始"


def format_agenda(entries: list[tuple[str, str]]) -> str:
    """entries: [(时间标签, 标题)]，按传入顺序输出多行文本。"""
    if not entries:
        return "暂无"
    return "\n".join(f"⏰ {label} │ {title}" for label, title in entries)


def format_ddl(ddl_str: str, tz: ZoneInfo | None = None) -> str:
    """把 ISO 截止时间格式化为「今天截止 / 还剩 N 天 / 已逾期 N 天」。"""
    if not ddl_str:
        return ""
    try:
        due = datetime.fromisoformat(str(ddl_str).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    zone = tz or datetime.now().astimezone().tzinfo
    if due.tzinfo is not None and zone is not None:
        due = due.astimezone(zone)
    due_date = due.date()
    today = datetime.now(zone).date() if zone is not None else datetime.now().date()
    diff = (due_date - today).days
    if diff < 0:
        return f"已逾期{-diff}天"
    if diff == 0:
        return "今天截止"
    return f"还剩{diff}天"
