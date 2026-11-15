"""日程管理工具核心逻辑（MaiBot 插件版）

纯逻辑函数：创建/删除/查看/修改日程。@Tool 装饰器在 plugin.py 定义，
本模块提供实现，接收 (plugin, 参数..., message dict)。

时间参数一律为 ISO 8601 本地时间：``2026-11-16T09:00`` 或 ``2026-11-16T09:00:00``；
全天日程只填日期 ``2026-11-16`` 并把 ``all_day`` 置真。过去的开始时间会被拒绝。
"""

from datetime import date, datetime, time, timedelta

from ..notices import time_label, weekday_label
from ..schedule_store import ScheduleItem
from ..targets import parse_user_target

FALLBACK_ERROR = "操作失败，请稍后重试"
MAX_YEAR = 2100


def _now(plugin) -> datetime:
    return datetime.now(plugin._timezone)


def _log(plugin):
    return plugin.ctx.logger


def _target_from_message(plugin, message: dict | None) -> str:
    """把消息的 platform 与发送者裸 ID 拼成存储/推送用的完整目标。"""
    if isinstance(message, dict):
        platform = str(message.get("platform") or "").strip()
        user_info = message.get("user_info") or {}
        if not user_info:
            user_info = (message.get("message_info") or {}).get("user_info") or {}
        bare = str(user_info.get("user_id") or "").strip()
        if bare:
            if platform:
                return f"{platform}:{bare}"
            for raw in plugin.config.basic.user_ids or []:
                candidate_platform, candidate_bare = parse_user_target(str(raw))
                if candidate_bare == bare:
                    return f"{candidate_platform}:{candidate_bare}"
            return f"qq:{bare}"
    targets = plugin.config.basic.user_ids or []
    if targets:
        platform, bare = parse_user_target(str(targets[0]))
        return f"{platform}:{bare}"
    return ""


def _parse_start(text: str, all_day: bool, now: datetime) -> datetime:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("开始时间不能为空")
    if all_day:
        day = date.fromisoformat(raw[:10])
        return datetime.combine(day, time.min)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(now.tzinfo).replace(tzinfo=None)
    return parsed


def _parse_end(text: str, all_day: bool, now: datetime) -> datetime | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if all_day:
        day = date.fromisoformat(raw[:10])
        return datetime.combine(day, time.max)
    return _parse_start(raw, False, now)


def _time_text(moment: datetime, all_day: bool) -> str:
    return moment.strftime("%Y-%m-%d") if all_day else moment.strftime("%Y-%m-%d %H:%M")


def _validate_start(start: datetime, all_day: bool, now: datetime) -> str | None:
    now_naive = now.replace(tzinfo=None)
    if all_day:
        if start.date() < now_naive.date():
            return "日期已过去，请给出今天或之后的日期。"
    elif start < now_naive - timedelta(minutes=1):
        return "开始时间已过去，请给出将来时间。"
    if start.year > MAX_YEAR:
        return f"时间超出可接受范围（最晚 {MAX_YEAR} 年）。"
    return None


def _find_items(items: list[ScheduleItem], schedule_id: str, keyword: str):
    if schedule_id:
        return [item for item in items if item.id == schedule_id]
    if keyword:
        return [item for item in items if keyword in item.title]
    return []


def _multiple_matches(matches: list[ScheduleItem]) -> str:
    lines = ["找到多个匹配日程，请用 schedule_id 精确指定："]
    for item in matches:
        lines.append(f"　[{item.id}] {item.title} · {item.time}")
    return "\n".join(lines)


def _candidate_line(item: ScheduleItem) -> str:
    return f"　[{item.id}] {item.title} · {item.time} {item.context}".rstrip()


async def _cancel_pending(plugin, target: str, item_id: str) -> None:
    canceller = getattr(plugin, "cancel_item_scan", None)
    if canceller is not None:
        await canceller(target, item_id)


async def create_schedule(
    plugin,
    title: str,
    start: str,
    end: str,
    all_day: bool,
    description: str,
    message: dict | None,
) -> str:
    target = _target_from_message(plugin, message)
    if not target:
        return "无法确定目标会话，请先在配置 user_ids 中登记。"
    title = (title or "").strip()
    if not title:
        return "日程标题不能为空。"
    now = _now(plugin)
    try:
        start_dt = _parse_start(start, all_day, now)
        end_dt = _parse_end(end, all_day, now)
    except ValueError:
        return "时间格式无效：请使用 ISO 8601，如 2026-11-16T09:00（全天只填 2026-11-16）。"
    problem = _validate_start(start_dt, all_day, now)
    if problem:
        return problem
    if end_dt is not None and end_dt <= start_dt:
        return "结束时间必须晚于开始时间。"
    item = ScheduleItem.create(
        title=title,
        time=_time_text(start_dt, all_day),
        end_time=None if all_day or end_dt is None else _time_text(end_dt, False),
        context=(description or "").strip(),
        all_day=all_day,
    )
    apple = getattr(plugin, "apple", None)
    if apple is not None:
        try:
            uid = await apple.create_event(
                summary=title,
                start=start_dt,
                end=end_dt,
                description=(description or "").strip(),
                all_day=all_day,
            )
            if uid:
                item.apple_uid = uid
        except Exception as exc:  # noqa: BLE001
            _log(plugin).warning(f"[schedule] 写入 Apple 日历失败: {exc}")
    await plugin.store.add_item(target, item)
    label = time_label(start_dt, item.end_time, all_day, plugin._timezone)
    day = f"{start_dt:%Y-%m-%d} {weekday_label(start_dt)}"
    suffix = "，已同步到 Apple 日历" if item.apple_uid else ""
    return f"已创建日程「{title}」，时间：{day} {label} ✅{suffix}"


async def delete_schedule(
    plugin,
    schedule_id: str,
    title_keyword: str,
    message: dict | None,
) -> str:
    target = _target_from_message(plugin, message)
    if not target:
        return "无法确定目标会话，请先在配置 user_ids 中登记。"
    matches = _find_items(
        await plugin.store.list_items(target), schedule_id, title_keyword
    )
    if not matches:
        return "未找到匹配的日程。"
    if len(matches) > 1:
        return _multiple_matches(matches)
    item = matches[0]
    apple = getattr(plugin, "apple", None)
    if item.apple_uid and apple is not None:
        ok = await apple.delete_event(item.apple_uid)
        if not ok:
            _log(plugin).warning(
                f"[schedule] 删除 Apple 事件失败，保留本地日程 id={item.id}"
            )
            return "删除 Apple 日历事件失败，本地日程已保留，请稍后重试。"
    await plugin.store.remove_item(target, item.id)
    await _cancel_pending(plugin, target, item.id)
    return f"已删除日程「{item.title}」✅"


async def list_schedules(plugin, days, message: dict | None) -> str:
    target = _target_from_message(plugin, message)
    if not target:
        return "无法确定目标会话，请先在配置 user_ids 中登记。"
    try:
        span = int(str(days).strip() or 7)
    except (TypeError, ValueError):
        span = 7
    span = max(1, min(span, 3650))
    now = _now(plugin)
    now_naive = now.replace(tzinfo=None)
    horizon = now_naive + timedelta(days=span)
    entries = []
    for item in await plugin.store.list_items(target):
        start = (
            datetime.fromisoformat(item.time)
            if len(item.time) > 10
            else datetime.combine(date.fromisoformat(item.time), time.min)
        )
        if item.all_day:
            if not (now_naive.date() <= start.date() <= horizon.date()):
                continue
        elif not (now_naive <= start <= horizon):
            continue
        entries.append((start, item))
    entries.sort(key=lambda pair: pair[0])
    if not entries:
        return f"最近 {span} 天没有日程安排。"
    lines = [f"最近 {span} 天的日程："]
    current_day = None
    for start, item in entries:
        if start.date() != current_day:
            current_day = start.date()
            lines.append(f"━━━ {start:%m-%d} {weekday_label(start)} ━━━")
        label = time_label(start, item.end_time, item.all_day, plugin._timezone)
        note = f"（{item.context}）" if item.context else ""
        lines.append(f"　{label} │ {item.title}{note}")
    return "\n".join(lines)


async def update_schedule(
    plugin,
    schedule_id: str,
    title: str,
    start: str,
    end: str,
    all_day: bool,
    message: dict | None,
) -> str:
    target = _target_from_message(plugin, message)
    if not target:
        return "无法确定目标会话，请先在配置 user_ids 中登记。"
    if not schedule_id:
        return "修改日程需要提供 schedule_id。"
    item = await plugin.store.get_item(target, schedule_id)
    if item is None:
        return "未找到该日程，请先查看日程列表确认 ID。"
    changed = False
    if not (start or title.strip() or all_day):
        return "没有需要修改的内容。"
    now = _now(plugin)
    if start:
        try:
            start_dt = _parse_start(start, all_day, now)
            end_dt = _parse_end(end, all_day, now)
        except ValueError:
            return "时间格式无效：请使用 ISO 8601，如 2026-11-16T09:00（全天只填 2026-11-16）。"
        problem = _validate_start(start_dt, all_day, now)
        if problem:
            return problem
        if end_dt is not None and end_dt <= start_dt:
            return "结束时间必须晚于开始时间。"
        item.time = _time_text(start_dt, all_day)
        item.end_time = None if all_day or end_dt is None else _time_text(end_dt, False)
        item.all_day = all_day
        item.last_triggered = None
        changed = True
    elif end:
        return "只给结束时间无法修改，请同时给出开始时间。"
    if title.strip():
        item.title = title.strip()
        changed = True
    if not changed:
        return "没有需要修改的内容。"
    await plugin.store.update_item(target, item)
    await _cancel_pending(plugin, target, item.id)
    apple = getattr(plugin, "apple", None)
    if item.apple_uid and apple is not None:
        start_dt = (
            datetime.fromisoformat(item.time)
            if len(item.time) > 10
            else datetime.combine(date.fromisoformat(item.time), time.min)
        )
        end_dt = datetime.fromisoformat(item.end_time) if item.end_time else None
        uid = await apple.create_event(
            summary=item.title,
            start=start_dt,
            end=end_dt,
            description=item.context,
            all_day=item.all_day,
            uid=item.apple_uid,
        )
        if not uid:
            item.apple_uid = ""
            await plugin.store.update_item(target, item)
            _log(plugin).warning(
                f"[schedule] 更新 Apple 事件失败，已清空 apple_uid id={item.id}"
            )
    label = time_label(
        datetime.fromisoformat(item.time)
        if len(item.time) > 10
        else datetime.combine(date.fromisoformat(item.time), time.min),
        item.end_time,
        item.all_day,
        plugin._timezone,
    )
    return f"已修改日程「{item.title}」，时间：{item.time} {label} ✅"
