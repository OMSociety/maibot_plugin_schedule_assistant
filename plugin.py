"""MaiBot Plugin: ScheduleAssistant — 日程提醒助手

从 AstrBot 插件 astrbot_plugin_schedule_assistant 迁移（AGPL-3.0）。

功能分工（用户决策，plan v3）：
- 早安播报：固定格式直发（LLM + markdown，100% 送达）
- 日程提醒：固定格式直发（LLM + markdown，100% 送达，保留扫描窗口/防重）
- 洗澡/睡觉/喝水：Maisaka 自己开口（ctx.maisaka.proactive.trigger，拟人化）
- 日程 CRUD：4 个 @Tool（LLM 工具）
- Apple 日历双向同步 / Notion 待办 / 天气：外部服务复用
- 消息事件：不做（MaiBot 无官方用户上下文 API，昵称走配置、日程提醒「近期对话」恒为空）

关键差异（相对 AstrBot 版）：
- 主动推送：user_id → ctx.chat 查聊天流 → ctx.send.custom/text（无 UMO 路由）
- LLM 人格：ctx.config.get() 读 MaiBot 全局 [personality]（无多人格）
- 存储：ctx.paths.data_dir/schedule_data.json（无框架 KV）
- 定时：APScheduler（on_load 启动 / on_unload 关闭）
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .apple_calendar import AppleCalendar
from .constants import (
    DEFAULT_BATH_TIME,
    DEFAULT_SLEEP_TIME,
    DEFAULT_WATER_END,
    DEFAULT_WATER_INTERVAL,
    DEFAULT_WATER_START,
    LOG_PREFIX,
    SCHEDULES_KEY,
)
from .engine import TimedMessageEngine
from .messaging import MessagingService
from .notion_client import NotionClient
from .reminders.briefing import BriefingReminder
from .reminders.habits import BathReminder, SleepReminder, WaterReminder
from .reminders.schedule import ScheduleReminder, check_and_trigger_schedule_reminder
from .schedule_store import ScheduleItem, ScheduleStore
from .services.llm import LLMService
from .services.notion import NotionService
from .services.weather import WeatherService
from .tools.schedule_tools import (
    create_schedule,
    delete_schedule,
    list_schedules,
    update_schedule,
)

logger = logging.getLogger(__name__)


# ============ 配置模型 ============


class BasicSettingsConfig(PluginConfigBase):
    """基础设置"""

    __ui_label__ = "基础设置"

    persona_hint: str = Field(
        default="",
        description="可选语气补充（如：播报时叫用户小名）。人格本体由 MaiBot 全局配置提供",
    )
    user_nickname: str = Field(
        default="", description="用户昵称（播报称呼，留空用「主人」）"
    )
    user_ids: list = Field(
        default_factory=list,
        description="接收自动提醒的用户 ID 列表（QQ 号或平台 UID）",
    )


class ScheduleReminderSettingsConfig(PluginConfigBase):
    """日程提醒"""

    __ui_label__ = "日程提醒"

    enable_schedule_reminder: bool = Field(
        default=False, description="开启日程 LLM 智能提醒"
    )
    schedule_reminder_minutes: int = Field(default=10, description="日程提前提醒分钟数")
    schedule_reminder_check_interval: int = Field(
        default=5, description="日程提醒扫描间隔（分钟），最小 2"
    )


class HabitReminderSettingsConfig(PluginConfigBase):
    """习惯提醒"""

    __ui_label__ = "习惯提醒"

    enable_morning_report: bool = Field(default=True, description="开启早安播报")
    morning_report_time: str = Field(
        default="09:00", description="早安播报时间（HH:MM）"
    )
    enable_bath_reminder: bool = Field(
        default=True, description="开启洗澡提醒（Maisaka 开口）"
    )
    bath_time: str = Field(
        default=DEFAULT_BATH_TIME, description="洗澡提醒时间（HH:MM）"
    )
    enable_sleep_reminder: bool = Field(
        default=True, description="开启睡觉提醒（Maisaka 开口）"
    )
    sleep_time: str = Field(
        default=DEFAULT_SLEEP_TIME, description="睡觉提醒时间（HH:MM）"
    )
    enable_water_reminder: bool = Field(
        default=True, description="开启喝水提醒（Maisaka 开口）"
    )
    water_interval: int = Field(
        default=DEFAULT_WATER_INTERVAL, description="喝水提醒间隔（分钟）"
    )
    water_start_time: str = Field(
        default=DEFAULT_WATER_START, description="喝水提醒开始时间（HH:MM）"
    )
    water_end_time: str = Field(
        default=DEFAULT_WATER_END, description="喝水提醒结束时间（HH:MM）"
    )


class CalendarSyncSettingsConfig(PluginConfigBase):
    """日历同步"""

    __ui_label__ = "日历同步"

    enable_apple_calendar_sync: bool = Field(
        default=False, description="Apple 日历双向同步"
    )
    apple_calendar_sync_interval: int = Field(
        default=30, description="Apple 日历同步间隔（分钟）"
    )
    apple_calendar: dict = Field(
        default_factory=dict,
        description="Apple 日历认证配置（username / app_password / calendar_id）",
    )
    webcal_urls: list = Field(
        default_factory=list, description="WebCal 共享日历链接列表"
    )


class ExternalServicesSettingsConfig(PluginConfigBase):
    """外部服务"""

    __ui_label__ = "外部服务"

    maton_api_key: str = Field(default="", description="Notion API Key（Maton 代理）")
    notion_db_ids: list = Field(
        default_factory=list,
        description="Notion 数据库 ID 列表（可带 事务:/阅读: 前缀）",
    )
    weather_api_key: str = Field(default="", description="心知天气 API Key")
    weather_city: str = Field(default="杭州", description="天气查询城市")


class MessageRenderSettingsConfig(PluginConfigBase):
    """消息渲染"""

    __ui_label__ = "消息渲染"

    markdown_enabled: bool = Field(
        default=True,
        description="提醒/播报启用 Markdown 渲染（QQ 官方适配器通过 qq_markdown 结构化消息发送）",
    )


class PromptSettingsConfig(PluginConfigBase):
    """提醒 Prompt 模板（可定制）"""

    __ui_label__ = "提醒 Prompt 模板"

    prompt_morning: str = Field(
        default="",
        description="早安播报模板。占位符：{username} {date} {weekday} {weather_current} {weather_forecast} {agenda} {notion_todos} {late_night}",
    )
    prompt_schedule: str = Field(
        default="",
        description="日程提醒模板。占位符：{item_title} {time_label} {ahead_label} {item_context} {conv_history}",
    )


class ScheduleAssistantConfig(PluginConfigBase):
    """插件完整配置"""

    __ui_label__ = "日程提醒助手"

    basic: BasicSettingsConfig = Field(
        default_factory=BasicSettingsConfig, description="基础设置"
    )
    schedule_reminder: ScheduleReminderSettingsConfig = Field(
        default_factory=ScheduleReminderSettingsConfig, description="日程提醒"
    )
    habit_reminder: HabitReminderSettingsConfig = Field(
        default_factory=HabitReminderSettingsConfig, description="习惯提醒"
    )
    calendar_sync: CalendarSyncSettingsConfig = Field(
        default_factory=CalendarSyncSettingsConfig, description="日历同步"
    )
    external_services: ExternalServicesSettingsConfig = Field(
        default_factory=ExternalServicesSettingsConfig, description="外部服务"
    )
    message_render: MessageRenderSettingsConfig = Field(
        default_factory=MessageRenderSettingsConfig, description="消息渲染"
    )
    prompt_settings: PromptSettingsConfig = Field(
        default_factory=PromptSettingsConfig, description="提醒 Prompt 模板"
    )


# ============ 插件主类 ============


class ScheduleAssistantPlugin(MaiBotPlugin):
    """日程提醒助手插件"""

    config_model = ScheduleAssistantConfig

    def __init__(self) -> None:
        super().__init__()
        self.store = ScheduleStore()
        self.scheduler: AsyncIOScheduler | None = None
        self.timed_engine: TimedMessageEngine | None = None
        self.messaging: MessagingService | None = None
        self.llm_service: LLMService | None = None
        self.weather_service: WeatherService | None = None
        self.notion_service: NotionService | None = None
        self.apple_calendar: AppleCalendar | None = None
        self.briefing_reminder: BriefingReminder | None = None
        self.bath_reminder: BathReminder | None = None
        self.sleep_reminder: SleepReminder | None = None
        self.water_reminder: WaterReminder | None = None
        self.schedule_reminder: ScheduleReminder | None = None
        self._services_ready = False
        self._services_init_lock = asyncio.Lock()
        self._tasks_registered = False
        self._schedule_reminder_scan_lock = asyncio.Lock()
        self._apple_calendar_sync_lock = asyncio.Lock()
        self._morning_ctx_cache: dict | None = None
        self._morning_ctx_ts = 0.0

    # ── 配置访问辅助（展平嵌套到 dict，兼容既有业务代码）──

    def _flat_config(self) -> dict:
        """把嵌套 config 展平为 dict（basic.user_ids → user_ids 等）"""
        cfg = {}
        c = self.config
        cfg["persona_hint"] = c.basic.persona_hint
        cfg["user_nickname"] = c.basic.user_nickname
        cfg["user_ids"] = c.basic.user_ids or []
        cfg["enable_schedule_reminder"] = c.schedule_reminder.enable_schedule_reminder
        cfg["schedule_reminder_minutes"] = c.schedule_reminder.schedule_reminder_minutes
        cfg["schedule_reminder_check_interval"] = (
            c.schedule_reminder.schedule_reminder_check_interval
        )
        cfg["enable_morning_report"] = c.habit_reminder.enable_morning_report
        cfg["morning_report_time"] = c.habit_reminder.morning_report_time
        cfg["enable_bath_reminder"] = c.habit_reminder.enable_bath_reminder
        cfg["bath_time"] = c.habit_reminder.bath_time
        cfg["enable_sleep_reminder"] = c.habit_reminder.enable_sleep_reminder
        cfg["sleep_time"] = c.habit_reminder.sleep_time
        cfg["enable_water_reminder"] = c.habit_reminder.enable_water_reminder
        cfg["water_interval"] = c.habit_reminder.water_interval
        cfg["water_start_time"] = c.habit_reminder.water_start_time
        cfg["water_end_time"] = c.habit_reminder.water_end_time
        cfg["enable_apple_calendar_sync"] = c.calendar_sync.enable_apple_calendar_sync
        cfg["apple_calendar_sync_interval"] = (
            c.calendar_sync.apple_calendar_sync_interval
        )
        cfg["apple_calendar"] = c.calendar_sync.apple_calendar or {}
        cfg["webcal_urls"] = c.calendar_sync.webcal_urls or []
        cfg["maton_api_key"] = c.external_services.maton_api_key
        cfg["notion_db_ids"] = c.external_services.notion_db_ids or []
        cfg["weather_api_key"] = c.external_services.weather_api_key
        cfg["weather_city"] = c.external_services.weather_city
        cfg["markdown_enabled"] = c.message_render.markdown_enabled
        cfg["prompt_morning"] = c.prompt_settings.prompt_morning or ""
        cfg["prompt_schedule"] = c.prompt_settings.prompt_schedule or ""
        return cfg

    # ── 生命周期 ────────────────────────────────────────

    async def on_load(self) -> None:
        # 存储：注入数据目录（JSON 文件）
        self.store.set_data_dir(str(self.ctx.paths.data_dir))

        # 消息服务与 LLM 服务
        conf = self._flat_config()
        default_user_id = None
        if conf.get("user_ids"):
            default_user_id = str(conf["user_ids"][0])
        self.messaging = MessagingService(
            self,
            conf,
            users_lookup=self.store.get_all_users,
            default_user_id=default_user_id,
        )
        self.llm_service = LLMService(self, conf)

        # 定时调度器
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self.timed_engine = TimedMessageEngine(self.messaging, self.scheduler)

        # 初始化外部服务与提醒组件
        await self._ensure_services()

        # 注册定时任务
        await self._register_tasks()

        self.ctx.logger.info(f"{LOG_PREFIX} 插件加载完成")

    async def on_unload(self) -> None:
        if self.timed_engine:
            self.timed_engine.shutdown()
        if self.apple_calendar:
            try:
                await self.apple_calendar.close()
            except Exception:
                pass
        self.ctx.logger.info(f"{LOG_PREFIX} 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        if scope != "self":
            return
        self.ctx.logger.info(f"{LOG_PREFIX} 配置已更新（热重载生效于下次重启调度）")

    # ── 服务初始化 ──────────────────────────────────────

    async def _ensure_services(self) -> None:
        if self._services_ready:
            return
        async with self._services_init_lock:
            if self._services_ready:
                return
            conf = self._flat_config()

            api_key = conf.get("weather_api_key")
            city = conf.get("weather_city", "杭州")
            if api_key:
                self.weather_service = WeatherService(
                    {"weather_api_key": api_key, "weather_city": city}
                )

            notion_db_ids = conf.get("notion_db_ids", [])
            maton_key = conf.get("maton_api_key")
            if notion_db_ids and maton_key:
                try:
                    transaction_db = ""
                    reading_db = ""
                    for item in notion_db_ids:
                        if isinstance(item, dict):
                            name = item.get("name", "")
                            db_id = item.get("id", "")
                            if name in ("事务", "transaction"):
                                transaction_db = db_id
                            elif name in ("阅读", "reading"):
                                reading_db = db_id
                        elif isinstance(item, str):
                            raw = item.strip()
                            if ":" in raw:
                                name, db_id = raw.split(":", 1)
                                if name.strip().lower() in ("事务", "transaction"):
                                    transaction_db = db_id.strip()
                                elif name.strip().lower() in ("阅读", "reading"):
                                    reading_db = db_id.strip()
                            elif not transaction_db:
                                transaction_db = raw
                            elif not reading_db:
                                reading_db = raw
                    self._notion = NotionClient(maton_key, transaction_db, reading_db)
                    self.notion_service = NotionService(self._notion)
                except Exception as e:
                    self.ctx.logger.warning(f"{LOG_PREFIX} Notion 初始化失败: {e}")
                    self.notion_service = None

            self.briefing_reminder = BriefingReminder(conf, self, self.llm_service)
            self.bath_reminder = BathReminder(
                conf,
                default_user_id=None,
                llm_service=self.llm_service,
                store=self.store,
            )
            self.sleep_reminder = SleepReminder(
                conf,
                default_user_id=None,
                llm_service=self.llm_service,
                store=self.store,
            )
            self.water_reminder = WaterReminder(
                conf,
                default_user_id=None,
                llm_service=self.llm_service,
                store=self.store,
            )
            self.schedule_reminder = ScheduleReminder(self.llm_service, conf)

            if conf.get("enable_apple_calendar_sync"):
                apple_conf = conf.get("apple_calendar", {})
                username = apple_conf.get("username") if apple_conf else None
                app_password = apple_conf.get("app_password") if apple_conf else None
                if username and app_password:
                    self.apple_calendar = AppleCalendar(
                        username=username,
                        app_password=app_password,
                        calendar_id=(apple_conf.get("calendar_id") or "").strip()
                        or None,
                        webcal_urls=conf.get("webcal_urls", []) or [],
                    )

            self._services_ready = True
            self.ctx.logger.info(f"{LOG_PREFIX} 外部服务初始化完成")

    # ── 定时任务注册 ────────────────────────────────────

    async def _register_tasks(self) -> None:
        if self._tasks_registered:
            return
        self._tasks_registered = True
        conf = self._flat_config()
        engine = self.timed_engine

        # 早安播报（固定格式直发）
        if conf.get("enable_morning_report", True):
            morning_time = conf.get("morning_report_time", "09:00")
            engine.register_job(
                "morning_briefing",
                morning_time,
                self._morning_briefing_content,
                prepare=self._prepare_morning_context,
            )

        # 洗澡/睡觉/喝水（Maisaka 自己开口）
        if conf.get("enable_bath_reminder", True):
            engine.register_job(
                "bath_reminder",
                conf.get("bath_time", DEFAULT_BATH_TIME),
                self._maisaka_habit_provider("bath"),
            )
        if conf.get("enable_sleep_reminder", True):
            engine.register_job(
                "sleep_reminder",
                conf.get("sleep_time", DEFAULT_SLEEP_TIME),
                self._maisaka_habit_provider("sleep"),
            )
        if conf.get("enable_water_reminder", True):
            water_interval = conf.get("water_interval", DEFAULT_WATER_INTERVAL)
            engine.register_raw_job(
                "water_reminder",
                ("interval", {"minutes": max(1, int(water_interval))}),
                self._water_reminder_tick,
                max_instances=1,
                coalesce=True,
            )

        # 日程提醒扫描（固定格式直发）
        if conf.get("enable_schedule_reminder"):
            check_interval = max(
                2, int(conf.get("schedule_reminder_check_interval", 5) or 5)
            )
            engine.register_raw_job(
                "schedule_reminder_scan",
                ("interval", {"minutes": check_interval}),
                self._schedule_reminder_scan,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=check_interval * 60,
            )

        # Apple 日历同步
        if conf.get("enable_apple_calendar_sync"):
            sync_interval = conf.get("apple_calendar_sync_interval", 30)
            engine.register_raw_job(
                "apple_calendar_sync",
                ("interval", {"minutes": sync_interval}),
                self._apple_calendar_sync,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )

        # 清理过期临时覆盖
        engine.register_raw_job(
            "clear_expired_overrides",
            CronTrigger(hour=0, minute=5),
            self._clear_expired_overrides,
        )

        engine.start()

    # ── 早安播报（固定格式直发）────────────────────────

    async def _prepare_morning_context(self) -> dict:
        now = time.monotonic()
        if self._morning_ctx_cache and now - self._morning_ctx_ts < 300:
            return self._morning_ctx_cache
        await self._ensure_services()
        weather_current, weather_forecast = "", ""
        if self.weather_service:
            try:
                weather_current, weather_forecast = await self.weather_service.fetch()
            except Exception:
                weather_current, weather_forecast = "", ""
        late_night_text = ""
        if self.apple_calendar:
            try:
                late_night = await self.apple_calendar.get_late_night_events()
                late_night_text = "、".join(
                    [e.get("summary", "无标题") for e in late_night[:3]]
                )
            except Exception:
                late_night_text = ""
        self._morning_ctx_cache = {
            "weather_current": weather_current,
            "weather_forecast": weather_forecast,
            "late_night": late_night_text,
        }
        self._morning_ctx_ts = now
        return self._morning_ctx_cache

    async def _morning_briefing_content(
        self, user_id: str, shared: dict | None = None
    ) -> str | None:
        """早安播报内容生成（只生成不发送，发送由引擎走 messaging）"""
        await self._ensure_services()
        shared = shared or await self._prepare_morning_context()

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        weekday_str = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
            now.weekday()
        ]
        nickname = await self._get_user_nickname(user_id)
        local_text = await self._get_today_local_schedules_text(user_id)
        apple_text = await self._get_today_apple_calendar_text()
        agenda_text = self._merge_today_schedule_blocks(local_text, apple_text)
        notion_text = await self._get_notion_pending_text()

        briefing = await self.briefing_reminder.generate_full_report(
            username=nickname,
            date=date_str,
            weekday=weekday_str,
            weather_current=shared.get("weather_current", ""),
            weather_forecast=shared.get("weather_forecast", ""),
            agenda=agenda_text,
            notion_todos=notion_text,
            late_night=shared.get("late_night", ""),
            user_id=user_id,
        )
        return briefing or None

    # ── 洗澡/睡觉/喝水：Maisaka 自己开口 ──────────────

    def _maisaka_habit_provider(self, habit_type: str):
        """构造 Maisaka 主动提醒 provider（定时引擎调用，触发 Maisaka 开口）"""

        async def provider(user_id: str, shared: Any = None) -> str | None:
            """返回 None（不直发）；实际通过 Maisaka proactive 触发"""
            stream = await self._get_user_stream(user_id)
            if not stream:
                return None
            intents = {
                "bath": "洗澡时间到了，自然地提醒用户去洗澡",
                "sleep": "该睡觉了，温柔地提醒用户早点休息",
                "water": "提醒用户喝口水休息一下",
            }
            reasons = {
                "bath": "bath_reminder",
                "sleep": "sleep_reminder",
                "water": "water_reminder",
            }
            try:
                await self.ctx.maisaka.proactive.trigger(
                    stream_id=stream.stream_id,
                    intent=intents.get(habit_type, "定时提醒"),
                    reason=reasons.get(habit_type, "habit_reminder"),
                    metadata={"source": "schedule_assistant", "habit": habit_type},
                )
            except Exception as e:
                self.ctx.logger.warning(
                    f"{LOG_PREFIX} Maisaka {habit_type} 提醒触发失败: {e}"
                )
            return None  # 不直发

        return provider

    async def _water_reminder_tick(self) -> None:
        """喝水提醒 tick：仅时段内触发 Maisaka"""
        conf = self._flat_config()
        if not conf.get("enable_water_reminder", True):
            return
        now = datetime.now()
        start_h, start_m = map(
            int, conf.get("water_start_time", DEFAULT_WATER_START).split(":")
        )
        end_h, end_m = map(
            int, conf.get("water_end_time", DEFAULT_WATER_END).split(":")
        )
        now_min = now.hour * 60 + now.minute
        start_min = start_h * 60 + start_m
        end_min = end_h * 60 + end_m
        if not (start_min <= now_min <= end_min):
            return
        await self._run_maisaka_habit("water")

    async def _run_maisaka_habit(self, habit_type: str) -> None:
        """对全部目标用户触发 Maisaka 提醒"""
        if not self.messaging:
            return
        for user_id in await self.messaging.resolve_target_users(
            include_known_users=True
        ):
            stream = await self._get_user_stream(user_id)
            if not stream:
                continue
            intents = {
                "bath": "洗澡时间到了，自然地提醒用户去洗澡",
                "sleep": "该睡觉了，温柔地提醒用户早点休息",
                "water": "提醒用户喝口水休息一下",
            }
            reasons = {
                "bath": "bath_reminder",
                "sleep": "sleep_reminder",
                "water": "water_reminder",
            }
            try:
                await self.ctx.maisaka.proactive.trigger(
                    stream_id=stream.stream_id,
                    intent=intents.get(habit_type, "定时提醒"),
                    reason=reasons.get(habit_type, "habit_reminder"),
                    metadata={"source": "schedule_assistant", "habit": habit_type},
                )
            except Exception as e:
                self.ctx.logger.warning(f"{LOG_PREFIX} Maisaka 提醒触发失败: {e}")

    # ── 日程提醒扫描（固定格式直发）────────────────────

    async def _schedule_reminder_scan(self) -> None:
        lock = self._schedule_reminder_scan_lock
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0)
        except asyncio.TimeoutError:
            return
        try:
            await self._ensure_services()
            if not self.schedule_reminder or not self.messaging:
                return

            conf = self._flat_config()
            try:
                minutes_ahead = max(
                    1, int(conf.get("schedule_reminder_minutes", 10) or 10)
                )
            except (ValueError, TypeError):
                minutes_ahead = 10

            for user_id in await self.messaging.resolve_target_users(
                include_known_users=True
            ):
                try:
                    triggered = await check_and_trigger_schedule_reminder(
                        schedule_store=self.store,
                        llm_service=self.llm_service,
                        user_id=user_id,
                        minutes_window=minutes_ahead,
                        minutes_before=minutes_ahead,
                        reminder=self.schedule_reminder,
                    )
                    for item in triggered:
                        if item.get("reminder_text"):
                            await self.messaging.send_to_user(
                                user_id, item["reminder_text"]
                            )
                except Exception as e:
                    self.ctx.logger.warning(
                        f"{LOG_PREFIX} 用户 {user_id} 日程提醒扫描失败: {e}"
                    )
        finally:
            lock.release()

    # ── Apple 日历同步 ─────────────────────────────────

    async def _apple_calendar_sync(self) -> None:
        lock = self._apple_calendar_sync_lock
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0)
        except asyncio.TimeoutError:
            return
        try:
            if not self.apple_calendar or not self.messaging:
                return
            try:
                events = await self.apple_calendar.get_all_events(days=7)
                if not events:
                    return
                for user_id in await self.messaging.resolve_target_users(
                    include_known_users=True
                ):
                    stats = await self.store.sync_from_apple_calendar(user_id, events)
                    if stats.get("added", 0) > 0:
                        self.ctx.logger.info(
                            f"{LOG_PREFIX} Apple→本地同步 user={user_id} "
                            f"added={stats['added']}"
                        )
            except Exception as e:
                self.ctx.logger.error(f"{LOG_PREFIX} Apple Calendar 同步失败: {e}")
        finally:
            lock.release()

    async def _clear_expired_overrides(self) -> None:
        if not self.messaging:
            return
        for user_id in await self.messaging.resolve_target_users(
            include_known_users=True
        ):
            await self.store.clear_expired_overrides(user_id)

    # ── 辅助 ───────────────────────────────────────────

    async def _get_user_stream(self, user_id: str):
        """按用户 ID 查聊天流（用户需私聊过 bot）"""
        try:
            return await self.ctx.chat.get_stream_by_user_id(
                str(user_id), platform="qq"
            )
        except Exception as e:
            self.ctx.logger.debug(f"{LOG_PREFIX} 查聊天流失败 user={user_id}: {e}")
            return None

    async def _get_user_nickname(self, user_id: str) -> str:
        try:
            cached = await self.store.get_user_nickname(user_id)
            cached = (cached or "").strip()
            if cached and not cached.isdigit():
                return cached
        except Exception:
            pass
        fallback = str(self._flat_config().get("user_nickname", "") or "").strip()
        if fallback and not fallback.isdigit():
            return fallback
        return "主人"

    async def _get_user_schedules(self, user_id: str) -> list[ScheduleItem]:
        schedules_dict = await self.store.get_schedules(user_id)
        return schedules_dict.get(SCHEDULES_KEY, [])

    async def _get_today_local_schedules_text(
        self, user_id: str, limit: int = 8
    ) -> str:
        schedules = await self._get_user_schedules(user_id)
        today = datetime.now().date()
        today_items = []
        for s in schedules:
            if not s.time:
                continue
            try:
                dt = datetime.fromisoformat(s.time)
            except Exception:
                try:
                    dt = datetime.strptime(s.time, "%Y-%m-%d %H:%M")
                except Exception:
                    continue
            if dt.date() == today:
                today_items.append((dt, s.title))
        if not today_items:
            return "暂无"
        today_items.sort(key=lambda x: x[0])
        return "\n".join(
            [
                f"⏰ {dt.strftime('%H:%M')} │ {title}"
                for dt, title in today_items[:limit]
            ]
        )

    async def _get_today_apple_calendar_text(self, limit: int = 8) -> str:
        if not self.apple_calendar:
            return "暂无"
        try:
            events = await self.apple_calendar.get_all_events(days=1)
            today = datetime.now().date()
            rows = []
            for e in events:
                start_str = e.get("start", "")
                summary = e.get("summary", "无标题")
                if not start_str:
                    continue
                try:
                    start_dt = datetime.fromisoformat(start_str)
                except Exception:
                    continue
                if start_dt.date() != today:
                    continue
                time_label = "全天" if e.get("all_day") else start_dt.strftime("%H:%M")
                rows.append((start_dt, f"⏰ {time_label} │ {summary}"))
            if not rows:
                return "暂无"
            rows.sort(key=lambda x: x[0])
            return "\n".join([line for _, line in rows[:limit]])
        except Exception as e:
            self.ctx.logger.warning(f"{LOG_PREFIX} Apple 今日日程读取失败: {e}")
            return "获取失败"

    async def _get_notion_pending_text(self, limit: int = 5) -> str:
        if not self.notion_service:
            return "暂无"
        try:
            pending = await self.notion_service.get_pending_tasks()
            if not pending:
                return "暂无"
            lines = []
            for task in pending[:limit]:
                ddl = self.notion_service.format_ddl(task.get("ddl", ""))
                title = task.get("title", "(无标题)")
                lines.append(f"- {ddl} | {title}" if ddl else f"- {title}")
            return "\n".join(lines) if lines else "暂无"
        except Exception:
            return "获取失败"

    def _extract_block_lines(self, block: str) -> list[str]:
        if not block or block in ("暂无", "获取失败"):
            return []
        return [line.strip() for line in block.split("\n") if line.strip()]

    def _merge_today_schedule_blocks(
        self, local_text: str, apple_text: str, limit: int = 12
    ) -> str:
        merged = []
        seen = set()
        for line in self._extract_block_lines(local_text) + self._extract_block_lines(
            apple_text
        ):
            key = " ".join(line.split())
            if key in seen:
                continue
            seen.add(key)
            merged.append(key)
            if len(merged) >= limit:
                break
        if merged:
            return "\n".join(merged)
        if apple_text == "获取失败" and local_text in ("暂无", "", None):
            return "获取失败"
        return "暂无"

    # ── @Tool: 日程管理（4 个）─────────────────────────

    @Tool(
        "create_schedule",
        description="创建新日程。当用户想要添加一个日程安排时调用。",
        brief_description="创建日程",
        detailed_description=(
            "参数说明：\n"
            "- title：string，必填。日程标题/内容。\n"
            "- datetime_str：string，必填。日期时间，支持「2024-01-15 14:30」「明天9点」「后天下午3点」「今天晚上8点」。\n"
            "- description：string，可选。备注描述。"
        ),
        parameters=[
            ToolParameterInfo(
                name="title",
                param_type=ToolParamType.STRING,
                description="日程标题/内容，如「开会」「组会」",
                required=True,
            ),
            ToolParameterInfo(
                name="datetime_str",
                param_type=ToolParamType.STRING,
                description="日期时间，如「2024-01-15 14:30」「明天9点」「后天下午3点」",
                required=True,
            ),
            ToolParameterInfo(
                name="description",
                param_type=ToolParamType.STRING,
                description="可选的备注描述",
                required=False,
            ),
        ],
    )
    async def handle_create_schedule(self, **kwargs):
        await self._ensure_services()
        return await create_schedule(
            self,
            kwargs.get("title", ""),
            kwargs.get("datetime_str", ""),
            kwargs.get("description", ""),
            kwargs.get("message"),
        )

    @Tool(
        "delete_schedule",
        description="删除日程。用于当用户想要取消或删除一个日程时调用。",
        brief_description="删除日程",
        detailed_description=(
            "参数说明：\n"
            "- schedule_id：string，可选。日程ID（精确匹配）。\n"
            "- title_keyword：string，可选。日程标题关键词（模糊匹配）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="schedule_id",
                param_type=ToolParamType.STRING,
                description="日程ID（精确匹配）",
                required=False,
            ),
            ToolParameterInfo(
                name="title_keyword",
                param_type=ToolParamType.STRING,
                description="日程标题关键词（模糊匹配）",
                required=False,
            ),
        ],
    )
    async def handle_delete_schedule(self, **kwargs):
        await self._ensure_services()
        return await delete_schedule(
            self,
            kwargs.get("schedule_id", ""),
            kwargs.get("title_keyword", ""),
            kwargs.get("message"),
        )

    @Tool(
        "list_schedules",
        description="查看日程列表。当用户想要查询日程安排时调用。",
        brief_description="查看日程",
        detailed_description="参数说明：\n- date：string，可选。日期（YYYY-MM-DD），缺省为今天。",
        parameters=[
            ToolParameterInfo(
                name="date",
                param_type=ToolParamType.STRING,
                description="日期（YYYY-MM-DD），缺省为今天",
                required=False,
            ),
        ],
    )
    async def handle_list_schedules(self, **kwargs):
        await self._ensure_services()
        return await list_schedules(self, kwargs.get("date", ""), kwargs.get("message"))

    @Tool(
        "update_schedule",
        description="修改日程。用于当用户想要修改日程的时间或标题时调用。",
        brief_description="修改日程",
        detailed_description=(
            "参数说明：\n"
            "- schedule_id：string，必填。日程ID。\n"
            "- title：string，可选。新标题。\n"
            "- datetime_str：string，可选。新时间。"
        ),
        parameters=[
            ToolParameterInfo(
                name="schedule_id",
                param_type=ToolParamType.STRING,
                description="日程ID",
                required=True,
            ),
            ToolParameterInfo(
                name="title",
                param_type=ToolParamType.STRING,
                description="新标题",
                required=False,
            ),
            ToolParameterInfo(
                name="datetime_str",
                param_type=ToolParamType.STRING,
                description="新时间（如「明天9点」）",
                required=False,
            ),
        ],
    )
    async def handle_update_schedule(self, **kwargs):
        await self._ensure_services()
        return await update_schedule(
            self,
            kwargs.get("schedule_id", ""),
            kwargs.get("title", ""),
            kwargs.get("datetime_str", ""),
            kwargs.get("message"),
        )


def create_plugin():
    return ScheduleAssistantPlugin()
