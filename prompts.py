"""定时推送的 intent 模板。

非空的自定义模板整段接管内置模板：插件不再追加任何约束。
占位符在 render 时按 data 替换，未知占位符保持原样。
"""

MORNING = """现在是 {date} {weekday}。
天气：{weather_current}；{weather_forecast}
今日日程：
{agenda}
{notion_block}
请写一段早安播报：一句问候，一句天气与体感，按时间顺序说今日日程和待办，最后一句收尾。

示例（其语气不纳入参考，按你的表达习惯来）：
早上好！今天晴，零下 1 度到 9 度，早晚偏冷，穿厚点。九点有组会，下午两点前记得交读书报告。别忘了买牛奶、回邮件。今天也要加油呀"""

BATH = """现在是 {current_time}。
提醒用户去洗澡。"""

SLEEP = """现在是 {current_time}。
提醒用户该睡觉了。"""

WATER = """现在是 {current_time}。
提醒用户喝杯水。"""

SCHEDULE = """日程「{item_title}」{ahead_label}（{time_label}）。
备注：{item_context}

提醒用户这件事。"""

TEMPLATES = {
    "morning": MORNING,
    "bath": BATH,
    "sleep": SLEEP,
    "water": WATER,
    "schedule": SCHEDULE,
}


def render(kind: str, data: dict, custom: str = "") -> str:
    """按 kind 渲染模板；custom 非空时整段替代内置模板。"""
    template = (custom or "").strip() or TEMPLATES[kind]
    values = dict(data)
    if kind == "morning":
        todos = values.get("notion_todos")
        values["notion_block"] = "" if todos is None else f"待办：\n{todos}\n"
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value or ""))
    return template.strip()
