"""早安播报服务（MaiBot 版，prompt 配置化）"""

from ..constants import BROADCAST_MD_OVERRIDE
from ..prompt_config import DEFAULT_PROMPT_MORNING, render_prompt


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

        template = self.config.get("prompt_morning") or DEFAULT_PROMPT_MORNING

        prompt = render_prompt(
            template,
            {
                "username": username,
                "date": date,
                "weekday": weekday,
                "weather_current": weather_current,
                "weather_forecast": weather_forecast or "暂无",
                "agenda": _nl.join(agenda_lines) if agenda_lines else "暂无",
                "notion_todos": _nl.join(notion_lines) if notion_lines else "暂无",
                "late_night": late_night_section,
            },
        )

        return await self.llm_service.generate(
            prompt, umo=user_id, extra_system=BROADCAST_MD_OVERRIDE
        )
