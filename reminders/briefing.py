"""早安播报服务"""

from ..constants import BROADCAST_MD_OVERRIDE


class BriefingReminder:
    def __init__(self, config: dict, context, llm_service):
        self.config = config
        self.context = context
        self.llm_service = llm_service

    async def generate_full_report(
        self,
        username: str,
        date: str,
        weekday: str,
        weather_current: str,
        weather_forecast: str,
        agenda: str,
        notion_todos: str,
        late_night: str = "",
        user_id: str | None = None,
    ) -> str:
        agenda_lines = (
            [ln.strip().replace("|", " ") for ln in agenda.split("\n") if ln.strip()]
            if agenda and agenda not in ("暂无", "获取失败")
            else []
        )
        _nl = chr(10)  # newline char for f-string
        notion_lines = (
            [ln.strip() for ln in notion_todos.split("\n") if ln.strip()]
            if notion_todos and notion_todos not in ("暂无", "获取失败")
            else []
        )

        late_night_section = ""
        if late_night and late_night.strip():
            late_night_section = (
                f"\n熬夜检测: 昨晚有深夜日程（{late_night.strip()}），辛苦了"
            )

        prompt = f"""【任务】生成一份完整的早安播报，严格遵循以下格式。

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
{_nl.join(agenda_lines) if agenda_lines else "暂无"}
待办:
{_nl.join(notion_lines) if notion_lines else "暂无"}{late_night_section}

【人格要求】
必须严格遵循上方系统人格设定的语气和风格。
称呼语要自然、亲切，体现对用户的了解和亲近感。
使用 markdown 格式输出（最小号标题、粗体、表格），不要 emoji 以外的表情符号。
日程/待办如为"暂无"则整块省略不输出。

【示例输出】
早安~新的一天开始啦♪

#### 📅 早安播报
愚人节快乐~ 2026-04-01 周三

**🌤️ 天气** 当前阴天 19°C，今日晴朗 9~24°C，降水概率0%

#### 📋 今日日程
| 时间 | 课程 |
|------|------|
| 09:45 | 学术英语听说 |
| 13:50 | 习近平新时代中国特色社会主义思想概论 |
| 15:35 | 马克思主义哲学史 |
| 19:00 | 学术写作与沟通 |

#### 📌 待办提醒
| 剩余 | 事项 |
|------|------|
| 🔥 还剩1天 | 《资本论》读书报告 |
| 📃 还剩3天 | 学生会面试 |

#### 🫕 温馨提示
今天阴天但气温还行，不用带伞~四门课连轴转辛苦了，中午记得吃点好的补充能量🥺读书报告只剩1天了，合理安排时间哦~"""

        return await self.llm_service.generate(
            prompt, umo=user_id, extra_system=BROADCAST_MD_OVERRIDE
        )
