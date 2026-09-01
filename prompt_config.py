"""Prompt 模板配置（MaiBot 版）

集中存放各提醒的默认 LLM 提示词模板（可被配置覆盖），以及占位符渲染工具。
配置项在 config_model 的 prompt_settings 子模型（见 plugin.py）。
配置为空白时使用本文件内置默认值。

用法：
    from .prompt_config import DEFAULT_PROMPT_MORNING, render_prompt
    template = config.get("prompt_morning") or DEFAULT_PROMPT_MORNING
    prompt = render_prompt(template, {...变量...})
"""

from __future__ import annotations


def render_prompt(template: str, ctx: dict) -> str:
    """把模板中的 {占位符} 替换为 ctx 值；ctx 缺键则替换为空字符串。

    用 replace 而非 format，避免模板里的花括号（如格式说明）冲突。
    """
    if not template:
        return ""
    for key, value in ctx.items():
        template = template.replace("{" + key + "}", str(value or ""))
    return template


# ============ 早安播报 ============

DEFAULT_PROMPT_MORNING = """【任务】生成一份完整的早安播报，严格遵循以下格式。

【格式要求】
称呼语（开头，必须有，称呼要自然亲切，可以叫昵称、名字或小名）
📅 日期 星期等信息
🌤️（Emoji要符合天气） 天气描述
📋 今日日程（如有）
📌 待办提醒（如有）
🫕 温馨建议（可选，一段以内）

【Markdown 排版要求】
小标题统一用最小号标题，格式为一行以 "#### " 开头
天气行用粗体：**🌤️ 天气** 内容
日程用两列表格，表头 "| 时间 | 课程 |"，第二行 "|------|------|"
待办用两列表格，表头 "| 剩余 | 事项 |"，第二行 "|------|------|"
温馨提示也用 "#### " 小标题，正文另起一行

【今日信息】
日期: {date} {weekday}
天气: {weather_current}（预报: {weather_forecast if weather_forecast else "暂无"}）
日程:
{agenda}
待办:
{notion_todos}{late_night}

【人格要求】
必须严格遵循上方系统人格设定的语气和风格。
称呼语要自然、亲切，体现对用户的了解和亲近感。
使用 markdown 格式输出（最小号标题、粗体、表格），不要 emoji 以外的表情符号。
日程/待办如为"暂无"则整块省略不输出。

【示例输出】
早安~新的一天开始啦♪

#### 📅 早安播报
2026-04-01 周三

**🌤️ 天气** 当前阴天 19°C，今日晴朗 9~24°C，降水概率0%

#### 📋 今日日程
| 时间 | 课程 |
|------|------|
| 09:45 | 学术英语听说 |

#### 📌 待办提醒
| 剩余 | 事项 |
|------|------|
| 🔥 还剩1天 | 《资本论》读书报告 |

#### 🫕 温馨提示
今天阴天但气温还行，不用带伞~"""


# ============ 洗澡 / 睡觉 / 喝水 ============

DEFAULT_PROMPT_BATH = """【重要】你的所有回复必须严格遵循系统人格设定。如果系统人格部分为空，则用你默认的对话风格。

生成一条洗澡时间提醒：

【用户信息】
- 当前时间: {current_time}
- 设定的洗澡时间: {default_time}

【近期对话】
{history}

【要求】
1. 语气和风格严格遵循系统人格设定
2. 40字以内，带1-2个emoji
3. 只输出提醒消息本身"""

DEFAULT_PROMPT_SLEEP = """【重要】你的所有回复必须严格遵循系统人格设定。如果系统人格部分为空，则用你默认的对话风格。

生成一条睡觉时间提醒：

【用户信息】
- 当前时间: {current_time}
- 设定的睡觉时间: {default_time}
- 是否已超晚(23点后): {is_late}

【要求】
1. 语气和风格严格遵循系统人格设定
2. 如果超晚了可以带点小责备，但要符合人格
3. 40字以内，带1-2个emoji
4. 只输出提醒消息本身"""

DEFAULT_PROMPT_WATER = """【重要】你的所有回复必须严格遵循系统人格设定。如果系统人格部分为空，则用你默认的对话风格。

生成一条喝水提醒：

【用户信息】
- 当前时间: {current_time}

【近期对话】
{history}

【要求】
1. 语气和风格严格遵循系统人格设定
2. 结合当前时间和对话上下文
3. 30字以内，带1-2个emoji
4. 只输出提醒消息本身"""


# ============ 日程提醒（自然口语新版） ============

DEFAULT_PROMPT_SCHEDULE = """你是一个日程提醒助手。现在有一个日程即将开始，请自然地提醒用户，像真人朋友那样随口说一句。

日程：{item_title}
开始时间：{time_label}
{ahead_label}

{item_context}

要求：
1. 语气要自然、口语化，像朋友随口提醒，不要生硬或官方。
2. 一句话，尽量短（15~30 字）。可以带一点撒娇/关切，但别啰嗦。
3. 直接说提醒本身，不要重复日程时间点，不要加"提醒你"之类的废话。
4. 可结合近期对话（如果有）让语气更贴合，但没有也不必硬凹。

近期对话（供参考，没有就忽略）：
{conv_history}

直接输出提醒内容，不要任何前缀或解释。"""
