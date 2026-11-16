"""日程到点判定与 intent 组装（不做发送）。"""

from datetime import datetime

from .. import notices


def parse_item_time(value: str, tz=None) -> datetime | None:
    """兼容旧调用：转给 notices.parse_item_time。"""
    return notices.parse_item_time(value, tz)


async def collect_due_schedule_items(
    store,
    user_id: str,
    minutes_before: int = 10,
    now: datetime | None = None,
) -> list[dict]:
    """挑出「开始前 minutes_before 分钟内」且未提醒过的日程。"""
    moment = now or datetime.now()
    if moment.tzinfo is not None:
        # 日程时间在存储里是配置时区的朴素本地时间，比较前把 now 归一到同一口径。
        moment = moment.replace(tzinfo=None)
    tz = moment.tzinfo
    due: list[dict] = []
    for item in await store.list_items(user_id):
        if item.last_triggered:
            continue
        if notices.is_all_day(item):
            continue
        start = notices.parse_item_time(item.time, tz)
        if start is None:
            continue
        minutes_until = (start - moment).total_seconds() / 60
        if minutes_until <= 0 or minutes_until > minutes_before:
            continue
        end = notices.parse_item_time(item.end_time, tz) if item.end_time else None
        due.append(
            {
                "item_id": item.id,
                "title": item.title,
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M") if end else "",
                "time_label": notices.time_label(start, item.end_time, False, tz),
                "ahead_label": notices.ahead_label(int(minutes_until)),
                "minutes_until": int(minutes_until),
                "context": (item.context or "").strip(),
                "source": "apple" if item.apple_uid else "local",
            }
        )
    return due


async def mark_items_triggered(store, user_id: str, items: list[dict]) -> None:
    """触发成功后幂等写 last_triggered。"""
    ids = {item.get("item_id") for item in items}
    stamp = datetime.now().isoformat()
    for item in await store.list_items(user_id):
        if item.id in ids and not item.last_triggered:
            item.last_triggered = stamp
            await store.update_item(user_id, item)


def build_intent(items: list[dict]) -> str:
    """把到点日程合并成一条 Maisaka intent。"""
    lines = []
    for item in items:
        title = (item.get("title") or "").strip() or "无标题"
        start = item.get("start") or ""
        end = item.get("end") or ""
        label = f"{start}-{end}" if start and end else start
        minutes = int(item.get("minutes_until") or 0)
        timing = (
            f"{label} 开始（约 {minutes} 分钟后）"
            if minutes > 0
            else f"{label} 马上开始"
        )
        line = f"- 「{title}」{timing}"
        context = (item.get("context") or "").strip()
        if context:
            line += f"｜{context}"
        lines.append(line)
    return (
        "日程提醒：以下日程快开始了，请自然地随口提醒用户（简短口语化）：\n"
        + "\n".join(lines)
    )
