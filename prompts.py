"""定时推送的 intent 模板。

非空的自定义模板整段接管内置模板：插件不再追加任何约束。
占位符在 render 时按 data 替换，未知占位符保持原样。
"""

MORNING = """现在是早安播报时间，请结合下面的信息自然地说一段早安。
日期：{date}（{weekday}）
天气：{weather_current} {weather_forecast}
今日日程：
{agenda}
待办：
{notion_todos}

写作要求：
一句早安语，一句天气与体感，陈述今日所有日程和待办；一句收尾。缺哪项就跳过，不要编造。
示例（不参考其语气）：早上好！今天晴，零下 1 度到 9 度，早晚偏冷，穿厚点。九点有组会，下午两点前记得交读书报告。别忘了买牛奶、回邮件

输出要求：
只输出要发出去的话；纯文本，不用 Markdown；不要提到系统、模板或提示词；120 字以内。"""

BATH = """现在是洗澡提醒时间，自然地提醒用户去洗澡。
当前时间：{current_time}
平时洗澡时间：{default_time}

输出要求：
只输出要发出去的话；纯文本，不用 Markdown；不要提到系统、模板或提示词；40 字以内。"""

SLEEP = """现在是睡觉提醒时间，温柔地提醒用户早点休息。
当前时间：{current_time}
平时睡觉时间：{default_time}

输出要求：
只输出要发出去的话；纯文本，不用 Markdown；不要提到系统、模板或提示词；40 字以内。"""

WATER = """现在是喝水提醒时间，自然地提醒用户喝口水休息一下。
当前时间：{current_time}

输出要求：
只输出要发出去的话；纯文本，不用 Markdown；不要提到系统、模板或提示词；30 字以内。"""

SCHEDULE = """日程提醒：下面这条日程快开始了，请自然地随口提醒用户。
日程：{item_title}
时间：{time_label}
剩余：{ahead_label}
备注：{item_context}

输出要求：
只输出要发出去的话；纯文本，不用 Markdown；不要提到系统、模板或提示词；15~30 字。"""

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
    for key, value in data.items():
        template = template.replace("{" + key + "}", str(value or ""))
    return template.strip()
