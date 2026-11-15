"""MaiBot Plugin: ScheduleAssistant — 日程提醒助手

从 AstrBot 插件 astrbot_plugin_schedule_assistant 迁移（AGPL-3.0）。

功能分工（用户决策）：
- 早安播报：固定格式直发（LLM + markdown，100% 送达）
- 日程提醒（含 Apple 日历事件提前提醒）：Maisaka 自己开口
  （ctx.maisaka.proactive.trigger，intent 注入回复生命周期）
- 洗澡/睡觉/喝水：Maisaka 自己开口（同上，拟人化）
- 日程 CRUD：4 个 @Tool（LLM 工具，支持单时间点 / 时间区间 / 全天）
- Apple 日历双向同步 / Notion 待办 / 天气：外部服务复用
- 消息事件：不做（MaiBot 无官方用户上下文 API，昵称走配置）

关键差异（相对 AstrBot 版）：
- 主动推送：user_id → ctx.chat 查聊天流 → ctx.send.custom/text（无 UMO 路由）
- LLM 人格：ctx.config.get() 读 MaiBot 全局 [personality]（无多人格）
- 存储：ctx.paths.data_dir/schedule_data.json（无框架 KV）
- 定时：APScheduler（on_load 启动 / on_unload 关闭）
"""

import asyncio
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from . import prompts, targets
from .apple_calendar import AppleCalendar
from .constants import (
    DEFAULT_BATH_TIME,
    DEFAULT_SLEEP_TIME,
    DEFAULT_WATER_END,
    DEFAULT_WATER_INTERVAL,
    DEFAULT_WATER_START,
    LOG_PREFIX,
)
from .engine import TimedMessageEngine
from .notion_client import NotionClient
from .reminders.morning import MorningBriefing
from .reminders.schedule import (
    collect_due_schedule_items,
    mark_items_triggered,
)
from .schedule_store import ScheduleStore
from .services.weather import WeatherService
from .tools.schedule_tools import (
    create_schedule,
    delete_schedule,
    list_schedules,
    update_schedule,
)


# ============ 配置模型 ============


class BasicSettingsConfig(PluginConfigBase):
    """基础设置"""

    __ui_label__ = "基础设置"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en-US": {"title": "Basic Settings", "description": "Basic settings"},
        "ja-JP": {"title": "基本設定", "description": "基本設定"},
    }

    timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA 时区（如 Asia/Shanghai），调度与 Apple 日历读写都按它",
        json_schema_extra={
            "label": "时区",
            "hint": "IANA 时区名，如 Asia/Shanghai；非法值回退 Asia/Shanghai",
            "placeholder": "Asia/Shanghai",
            "i18n": {
                "en-US": {
                    "label": "Time Zone",
                    "hint": "IANA time zone name, e.g. Asia/Shanghai; "
                    "invalid values fall back to Asia/Shanghai",
                    "placeholder": "Asia/Shanghai",
                },
                "ja-JP": {
                    "label": "タイムゾーン",
                    "hint": "IANA のタイムゾーン名（例: Asia/Shanghai）。"
                    "無効な値は Asia/Shanghai に戻ります",
                    "placeholder": "Asia/Shanghai",
                },
            },
        },
    )
    user_ids: list[str] = Field(
        default_factory=list,
        description="接收自动提醒的用户，每项为 platform:裸ID（如 qq:123456）",
        json_schema_extra={
            "label": "接收提醒的用户",
            "hint": "每项 `platform:裸ID`，如 qq:123456（operator 同款格式）",
            "placeholder": "qq:123456",
            "i18n": {
                "en-US": {
                    "label": "Users Receiving Reminders",
                    "hint": "Each item `platform:裸ID`, e.g. qq:123456 "
                    "(same format as operator)",
                    "placeholder": "qq:123456",
                },
                "ja-JP": {
                    "label": "リマインダーを受け取るユーザー",
                    "hint": "各項目は `platform:裸ID` 形式、例：qq:123456"
                    "（operator と同じ形式）",
                    "placeholder": "qq:123456",
                },
            },
        },
    )


class ScheduleReminderSettingsConfig(PluginConfigBase):
    """日程提醒"""

    __ui_label__ = "日程提醒"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en-US": {"title": "Schedule Reminders", "description": "Schedule reminders"},
        "ja-JP": {
            "title": "スケジュールリマインダー",
            "description": "スケジュールのリマインダー",
        },
    }

    enable_schedule_reminder: bool = Field(
        default=False,
        description="开启日程与 Apple 日历事件的提前提醒（由 Maisaka 拟人开口）",
        json_schema_extra={
            "label": "开启日程提醒",
            "i18n": {
                "en-US": {
                    "label": "Enable Schedule Reminders",
                    "hint": "Advance reminders for schedules and Apple Calendar "
                    "events (spoken by Maisaka)",
                },
                "ja-JP": {
                    "label": "スケジュールリマインダーを有効化",
                    "hint": "スケジュールと Apple カレンダーの予定を事前にリマインド"
                    "します（Maisaka が自ら話しかけます）",
                },
            },
        },
    )
    schedule_reminder_minutes: int = Field(
        default=10,
        description="日程提前提醒分钟数",
        json_schema_extra={
            "label": "提前提醒（分钟）",
            "i18n": {
                "en-US": {
                    "label": "Remind Ahead (minutes)",
                    "hint": "Minutes before the schedule to send the reminder",
                },
                "ja-JP": {
                    "label": "事前リマインド（分）",
                    "hint": "スケジュールの何分前にリマインドするか",
                },
            },
        },
    )
    schedule_reminder_check_interval: int = Field(
        default=5,
        description="日程提醒扫描间隔（分钟），最小 2",
        json_schema_extra={
            "label": "扫描间隔（分钟）",
            "hint": "最小 2，建议不大于提前提醒分钟数",
            "i18n": {
                "en-US": {
                    "label": "Scan Interval (minutes)",
                    "hint": "Minimum: 2; keep it no greater than the remind-ahead minutes",
                },
                "ja-JP": {
                    "label": "スキャン間隔（分）",
                    "hint": "最小 2、事前リマインド分数以下を推奨",
                },
            },
        },
    )


class HabitReminderSettingsConfig(PluginConfigBase):
    """习惯提醒"""

    __ui_label__ = "习惯提醒"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en-US": {"title": "Habit Reminders", "description": "Habit reminders"},
        "ja-JP": {"title": "習慣リマインダー", "description": "習慣リマインダー"},
    }

    enable_morning_report: bool = Field(
        default=True,
        description="开启早安播报",
        json_schema_extra={
            "label": "开启早安播报",
            "i18n": {
                "en-US": {
                    "label": "Enable Morning Briefing",
                    "hint": "Enable the morning briefing",
                },
                "ja-JP": {
                    "label": "おはよう配信を有効化",
                    "hint": "おはよう配信を有効にします",
                },
            },
        },
    )
    morning_report_time: str = Field(
        default="09:00",
        description="早安播报时间（HH:MM）",
        json_schema_extra={
            "label": "早安时间",
            "placeholder": "09:00",
            "i18n": {
                "en-US": {
                    "label": "Morning Briefing Time",
                    "hint": "Morning briefing time (HH:MM)",
                    "placeholder": "09:00",
                },
                "ja-JP": {
                    "label": "おはよう配信の時刻",
                    "hint": "おはよう配信の時刻（HH:MM）",
                    "placeholder": "09:00",
                },
            },
        },
    )
    enable_bath_reminder: bool = Field(
        default=True,
        description="开启洗澡提醒（Maisaka 开口）",
        json_schema_extra={
            "label": "开启洗澡提醒",
            "i18n": {
                "en-US": {
                    "label": "Enable Bath Reminder",
                    "hint": "Enable the bath reminder (spoken by Maisaka)",
                },
                "ja-JP": {
                    "label": "お風呂リマインダーを有効化",
                    "hint": "お風呂リマインダーを有効化します（Maisaka が自ら話しかけます）",
                },
            },
        },
    )
    bath_time: str = Field(
        default=DEFAULT_BATH_TIME,
        description="洗澡提醒时间（HH:MM）",
        json_schema_extra={
            "label": "洗澡时间",
            "placeholder": "HH:MM",
            "i18n": {
                "en-US": {
                    "label": "Bath Time",
                    "hint": "Bath reminder time (HH:MM)",
                    "placeholder": "HH:MM",
                },
                "ja-JP": {
                    "label": "お風呂の時間",
                    "hint": "お風呂リマインダーの時刻（HH:MM）",
                    "placeholder": "HH:MM",
                },
            },
        },
    )
    enable_sleep_reminder: bool = Field(
        default=True,
        description="开启睡觉提醒（Maisaka 开口）",
        json_schema_extra={
            "label": "开启睡觉提醒",
            "i18n": {
                "en-US": {
                    "label": "Enable Sleep Reminder",
                    "hint": "Enable the sleep reminder (spoken by Maisaka)",
                },
                "ja-JP": {
                    "label": "就寝リマインダーを有効化",
                    "hint": "就寝リマインダーを有効化します（Maisaka が自ら話しかけます）",
                },
            },
        },
    )
    sleep_time: str = Field(
        default=DEFAULT_SLEEP_TIME,
        description="睡觉提醒时间（HH:MM）",
        json_schema_extra={
            "label": "睡觉时间",
            "placeholder": "HH:MM",
            "i18n": {
                "en-US": {
                    "label": "Sleep Time",
                    "hint": "Sleep reminder time (HH:MM)",
                    "placeholder": "HH:MM",
                },
                "ja-JP": {
                    "label": "就寝時間",
                    "hint": "就寝リマインダーの時刻（HH:MM）",
                    "placeholder": "HH:MM",
                },
            },
        },
    )
    enable_water_reminder: bool = Field(
        default=True,
        description="开启喝水提醒（Maisaka 开口）",
        json_schema_extra={
            "label": "开启喝水提醒",
            "i18n": {
                "en-US": {
                    "label": "Enable Water Reminder",
                    "hint": "Enable the water reminder (spoken by Maisaka)",
                },
                "ja-JP": {
                    "label": "水分補給リマインダーを有効化",
                    "hint": "水分補給リマインダーを有効化します（Maisaka が自ら話しかけます）",
                },
            },
        },
    )
    water_interval: int = Field(
        default=DEFAULT_WATER_INTERVAL,
        description="喝水提醒间隔（分钟）",
        json_schema_extra={
            "label": "喝水间隔（分钟）",
            "i18n": {
                "en-US": {
                    "label": "Water Reminder Interval (minutes)",
                    "hint": "Interval between water reminders (minutes)",
                },
                "ja-JP": {
                    "label": "水分補給リマインダーの間隔（分）",
                    "hint": "水分補給リマインダーの間隔（分）",
                },
            },
        },
    )
    water_start_time: str = Field(
        default=DEFAULT_WATER_START,
        description="喝水提醒开始时间（HH:MM）",
        json_schema_extra={
            "label": "喝水开始时间",
            "placeholder": "HH:MM",
            "i18n": {
                "en-US": {
                    "label": "Water Reminder Start Time",
                    "hint": "Start time of the water reminder window (HH:MM)",
                    "placeholder": "HH:MM",
                },
                "ja-JP": {
                    "label": "水分補給の開始時刻",
                    "hint": "水分補給リマインダーの開始時刻（HH:MM）",
                    "placeholder": "HH:MM",
                },
            },
        },
    )
    water_end_time: str = Field(
        default=DEFAULT_WATER_END,
        description="喝水提醒结束时间（HH:MM）",
        json_schema_extra={
            "label": "喝水结束时间",
            "placeholder": "HH:MM",
            "i18n": {
                "en-US": {
                    "label": "Water Reminder End Time",
                    "hint": "End time of the water reminder window (HH:MM)",
                    "placeholder": "HH:MM",
                },
                "ja-JP": {
                    "label": "水分補給の終了時刻",
                    "hint": "水分補給リマインダーの終了時刻（HH:MM）",
                    "placeholder": "HH:MM",
                },
            },
        },
    )


class CalendarSyncSettingsConfig(PluginConfigBase):
    """日历同步"""

    __ui_label__ = "日历同步"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en-US": {"title": "Calendar Sync", "description": "Calendar sync"},
        "ja-JP": {"title": "カレンダー同期", "description": "カレンダー同期"},
    }

    enable_apple_calendar_sync: bool = Field(
        default=False,
        description="Apple 日历双向同步",
        json_schema_extra={
            "label": "Apple 日历双向同步",
            "i18n": {
                "en-US": {
                    "label": "Two-way Apple Calendar Sync",
                    "hint": "Enable two-way sync with Apple Calendar",
                },
                "ja-JP": {
                    "label": "Apple カレンダーの双方向同期",
                    "hint": "Apple カレンダーとの双方向同期を有効にします",
                },
            },
        },
    )
    apple_calendar_sync_interval: int = Field(
        default=30,
        description="Apple 日历同步间隔（分钟）",
        json_schema_extra={
            "label": "同步间隔（分钟）",
            "i18n": {
                "en-US": {
                    "label": "Sync Interval (minutes)",
                    "hint": "Apple Calendar sync interval (minutes)",
                },
                "ja-JP": {
                    "label": "同期間隔（分）",
                    "hint": "Apple カレンダーの同期間隔（分）",
                },
            },
        },
    )
    apple_username: str = Field(
        default="",
        description="Apple ID（iCloud 邮箱）",
        json_schema_extra={
            "label": "Apple ID",
            "placeholder": "iCloud 邮箱",
            "i18n": {
                "en-US": {
                    "label": "Apple ID",
                    "hint": "Apple ID (iCloud email)",
                    "placeholder": "iCloud email",
                },
                "ja-JP": {
                    "label": "Apple ID",
                    "hint": "Apple ID（iCloud のメールアドレス）",
                    "placeholder": "iCloud のメールアドレス",
                },
            },
        },
    )
    apple_app_password: str = Field(
        default="",
        description="App 专用密码",
        json_schema_extra={
            "label": "App 专用密码",
            "i18n": {
                "en-US": {
                    "label": "App-Specific Password",
                    "hint": "App-specific password",
                },
                "ja-JP": {
                    "label": "アプリ専用パスワード",
                    "hint": "アプリ専用パスワード",
                },
            },
        },
    )
    apple_calendar_id: str = Field(
        default="",
        description="日历 ID（可留空）",
        json_schema_extra={
            "label": "日历 ID",
            "hint": "留空使用第一个日历；可用值见日志里的「可用日历」清单",
            "i18n": {
                "en-US": {
                    "label": "Calendar ID",
                    "hint": "Leave empty to use the first calendar; see the "
                    "available-calendar list in the log for valid UUIDs",
                },
                "ja-JP": {
                    "label": "カレンダー ID",
                    "hint": "空欄なら最初のカレンダーを使用。有効な UUID は"
                    "ログの「可用日历」一覧を参照",
                },
            },
        },
    )
    apple_caldav_host: str = Field(
        default="",
        description="CalDAV 服务器地址（可留空）",
        json_schema_extra={
            "label": "CalDAV 服务器地址",
            "placeholder": "caldav.icloud.com",
            "hint": "留空使用 caldav.icloud.com。中国大陆区账号若被拒绝认证，"
            "可填 caldav.icloud.com.cn；若该地址仍不通，填网页版 iCloud 日历"
            "共享链接里自己的主机，形如 pXX-caldav.icloud.com.cn",
            "i18n": {
                "en-US": {
                    "label": "CalDAV Server Host",
                    "hint": "Leave empty to use caldav.icloud.com. Mainland-China "
                    "accounts that get rejected can use caldav.icloud.com.cn; "
                    "if that still fails, use the host shown in your iCloud web "
                    "calendar share link, e.g. pXX-caldav.icloud.com.cn",
                    "placeholder": "caldav.icloud.com",
                },
                "ja-JP": {
                    "label": "CalDAV サーバー",
                    "hint": "空欄なら caldav.icloud.com を使用。中国本土の"
                    "アカウントで認証を拒否される場合は caldav.icloud.com.cn、"
                    "それでも通らない場合は iCloud ウェブ版カレンダーの共有"
                    "リンクにあるホスト（例: pXX-caldav.icloud.com.cn）を入力",
                    "placeholder": "caldav.icloud.com",
                },
            },
        },
    )
    webcal_urls: list[str] = Field(
        default_factory=list,
        description="WebCal 共享日历链接列表",
        json_schema_extra={
            "label": "WebCal 订阅链接",
            "i18n": {
                "en-US": {
                    "label": "WebCal Subscription URLs",
                    "hint": "List of WebCal shared calendar URLs",
                },
                "ja-JP": {
                    "label": "WebCal 購読リンク",
                    "hint": "WebCal 共有カレンダーのリンクリスト",
                },
            },
        },
    )


class ExternalServicesSettingsConfig(PluginConfigBase):
    """外部服务"""

    __ui_label__ = "外部服务"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en-US": {"title": "External Services", "description": "External services"},
        "ja-JP": {"title": "外部サービス", "description": "外部サービス"},
    }

    maton_api_key: str = Field(
        default="",
        description=(
            "Maton API Key（第三方网关）。此密钥会作为 Bearer 头发送给第三方 "
            "gateway.maton.ai，而非直连 Notion 官方 API；详见 README「数据安全与隐私」"
        ),
        json_schema_extra={
            "label": "Maton API Key（第三方网关）",
            "hint": "发往 gateway.maton.ai，非 Notion 官方直连",
            "i18n": {
                "en-US": {
                    "label": "Maton API Key (third-party gateway)",
                    "hint": "Sent to gateway.maton.ai, not the official Notion API",
                },
                "ja-JP": {
                    "label": "Maton API Key（サードパーティーゲートウェイ）",
                    "hint": "gateway.maton.ai に送信され、Notion 公式への直接接続ではありません",
                },
            },
        },
    )
    notion_db_ids: list[str] = Field(
        default_factory=list,
        description="Notion 数据库 ID 列表（可带 事务:/阅读: 前缀）",
        json_schema_extra={
            "label": "Notion 数据库 ID",
            "hint": "可带 事务:/阅读: 前缀",
            "i18n": {
                "en-US": {
                    "label": "Notion Database IDs",
                    "hint": "May carry a 事务:/阅读: prefix "
                    "(transaction:/reading: also accepted)",
                },
                "ja-JP": {
                    "label": "Notion データベース ID",
                    "hint": "先頭に 事务:/阅读: のプレフィックスを付けられます"
                    "（transaction:/reading: も可）",
                },
            },
        },
    )
    weather_api_key: str = Field(
        default="",
        description="心知天气 API Key",
        json_schema_extra={
            "label": "心知天气 API Key",
            "i18n": {
                "en-US": {
                    "label": "Seniverse Weather API Key",
                    "hint": "API Key of the Seniverse weather service",
                },
                "ja-JP": {
                    "label": "心知天気（Seniverse）API Key",
                    "hint": "心知天気（Seniverse）の API Key",
                },
            },
        },
    )
    weather_city: str = Field(
        default="北京",
        description="天气查询城市",
        json_schema_extra={
            "label": "天气城市",
            "i18n": {
                "en-US": {
                    "label": "Weather City",
                    "hint": "City for weather queries",
                },
                "ja-JP": {
                    "label": "天気の取得都市",
                    "hint": "天気を取得する都市",
                },
            },
        },
    )


class PromptSettingsConfig(PluginConfigBase):
    """定时推送的 intent 模板"""

    __ui_label__ = "定时推送模板"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en-US": {
            "title": "Push Message Templates",
            "description": "Intent templates for scheduled pushes",
        },
        "ja-JP": {
            "title": "定期配信テンプレート",
            "description": "定期配信の intent テンプレート",
        },
    }

    prompt_morning: str = Field(
        default="",
        description="早安播报模板；留空用内置模板，非空时整段接管",
        json_schema_extra={
            "label": "早安播报模板",
            "hint": "留空用内置模板；非空时整段接管，插件不再追加约束",
            "placeholder": "{date} {weekday} {weather_current} {agenda}…",
            "i18n": {
                "en-US": {
                    "label": "Morning Template",
                    "hint": "Leave empty to use the built-in template; a non-empty "
                    "template replaces it entirely with no extra constraints",
                    "placeholder": "{date} {weekday} {weather_current} {agenda}…",
                },
                "ja-JP": {
                    "label": "おはよう配信テンプレート",
                    "hint": "空欄なら内蔵テンプレート。入力すると全文を置き換え、"
                    "追加の制約は付きません",
                    "placeholder": "{date} {weekday} {weather_current} {agenda}…",
                },
            },
        },
    )
    prompt_bath: str = Field(
        default="",
        description="洗澡提醒模板；留空用内置模板，非空时整段接管",
        json_schema_extra={
            "label": "洗澡提醒模板",
            "hint": "留空用内置模板；非空时整段接管",
            "placeholder": "{current_time} {default_time}",
            "i18n": {
                "en-US": {
                    "label": "Bath Template",
                    "hint": "Leave empty to use the built-in template; a non-empty "
                    "template replaces it entirely",
                    "placeholder": "{current_time} {default_time}",
                },
                "ja-JP": {
                    "label": "お風呂リマインダーテンプレート",
                    "hint": "空欄なら内蔵テンプレート。入力すると全文を置き換えます",
                    "placeholder": "{current_time} {default_time}",
                },
            },
        },
    )
    prompt_sleep: str = Field(
        default="",
        description="睡觉提醒模板；留空用内置模板，非空时整段接管",
        json_schema_extra={
            "label": "睡觉提醒模板",
            "hint": "留空用内置模板；非空时整段接管",
            "placeholder": "{current_time} {default_time}",
            "i18n": {
                "en-US": {
                    "label": "Sleep Template",
                    "hint": "Leave empty to use the built-in template; a non-empty "
                    "template replaces it entirely",
                    "placeholder": "{current_time} {default_time}",
                },
                "ja-JP": {
                    "label": "就寝リマインダーテンプレート",
                    "hint": "空欄なら内蔵テンプレート。入力すると全文を置き換えます",
                    "placeholder": "{current_time} {default_time}",
                },
            },
        },
    )
    prompt_water: str = Field(
        default="",
        description="喝水提醒模板；留空用内置模板，非空时整段接管",
        json_schema_extra={
            "label": "喝水提醒模板",
            "hint": "留空用内置模板；非空时整段接管",
            "placeholder": "{current_time}",
            "i18n": {
                "en-US": {
                    "label": "Water Template",
                    "hint": "Leave empty to use the built-in template; a non-empty "
                    "template replaces it entirely",
                    "placeholder": "{current_time}",
                },
                "ja-JP": {
                    "label": "水分補給リマインダーテンプレート",
                    "hint": "空欄なら内蔵テンプレート。入力すると全文を置き換えます",
                    "placeholder": "{current_time}",
                },
            },
        },
    )
    prompt_schedule: str = Field(
        default="",
        description="日程提醒模板；留空用内置模板，非空时整段接管",
        json_schema_extra={
            "label": "日程提醒模板",
            "hint": "留空用内置模板；非空时整段接管",
            "placeholder": "{item_title} {time_label} {ahead_label} …",
            "i18n": {
                "en-US": {
                    "label": "Schedule Template",
                    "hint": "Leave empty to use the built-in template; a non-empty "
                    "template replaces it entirely",
                    "placeholder": "{item_title} {time_label} {ahead_label} …",
                },
                "ja-JP": {
                    "label": "スケジュールリマインダーテンプレート",
                    "hint": "空欄なら内蔵テンプレート。入力すると全文を置き換えます",
                    "placeholder": "{item_title} {time_label} {ahead_label} …",
                },
            },
        },
    )


class PluginBaseConfig(PluginConfigBase):
    """插件基础配置"""

    __ui_label__ = "插件基础设置"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en-US": {"title": "Plugin Basics", "description": "Plugin base configuration"},
        "ja-JP": {"title": "プラグイン基本設定", "description": "プラグインの基本設定"},
    }

    config_version: str = Field(
        default="2.0.0",
        description="配置版本号",
        json_schema_extra={
            "label": "配置版本",
            "disabled": True,
            "i18n": {
                "en-US": {
                    "label": "Config Version",
                    "hint": "Configuration version number",
                },
                "ja-JP": {
                    "label": "設定バージョン",
                    "hint": "設定のバージョン番号",
                },
            },
        },
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            "i18n": {
                "en-US": {
                    "label": "Enable Plugin",
                    "hint": "Whether to enable the plugin",
                },
                "ja-JP": {
                    "label": "プラグインを有効化",
                    "hint": "プラグインを有効にするかどうか",
                },
            },
        },
    )


class ScheduleAssistantConfig(PluginConfigBase):
    """插件完整配置"""

    __ui_label__ = "日程提醒助手"

    plugin: PluginBaseConfig = Field(
        default_factory=PluginBaseConfig, description="插件基础配置"
    )
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
    prompt_settings: PromptSettingsConfig = Field(
        default_factory=PromptSettingsConfig, description="早安播报模板"
    )


# ============ 插件主类 ============


def _message_timestamp(msg: dict) -> datetime | None:
    """从宿主序列化消息里取发送时间（MessageDict.timestamp 为字符串，兼容 ISO 与时间戳）"""
    raw = msg.get("timestamp")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.fromtimestamp(float(text))
            except (OverflowError, OSError, ValueError):
                return None
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    return None


def _message_sender_id(msg: dict) -> str:
    """从宿主序列化消息里取发送者 ID（message_info.user_info.user_id）"""
    info = msg.get("message_info")
    if not isinstance(info, dict):
        return ""
    user_info = info.get("user_info")
    if not isinstance(user_info, dict):
        return ""
    return str(user_info.get("user_id") or "")


class ScheduleAssistantPlugin(MaiBotPlugin):
    """日程提醒助手插件"""

    config_model = ScheduleAssistantConfig

    def __init__(self) -> None:
        super().__init__()
        self.store = ScheduleStore()
        self.scheduler: AsyncIOScheduler | None = None
        self.timed_engine: TimedMessageEngine | None = None
        self.weather_service: WeatherService | None = None
        self.notion_client: NotionClient | None = None
        self.apple_calendar: AppleCalendar | None = None
        self.morning: MorningBriefing | None = None
        self._timezone: ZoneInfo = ZoneInfo("Asia/Shanghai")
        self._services_ready = False
        self._services_lock = asyncio.Lock()
        self._tasks_registered = False
        self._scan_lock = asyncio.Lock()
        self._apple_lock = asyncio.Lock()
        self._last_apple_issue: str | None = None

    def _targets(self) -> list[str]:
        return [
            str(item).strip()
            for item in (self.config.basic.user_ids or [])
            if str(item).strip()
        ]

    def _resolve_timezone(self) -> ZoneInfo:
        name = str(self.config.basic.timezone or "").strip() or "Asia/Shanghai"
        try:
            return ZoneInfo(name)
        except Exception:
            self.ctx.logger.warning(
                f"{LOG_PREFIX} 时区 {name!r} 无法识别，回退 Asia/Shanghai"
            )
            return ZoneInfo("Asia/Shanghai")

    async def on_load(self) -> None:
        self.store.set_data_dir(str(self.ctx.paths.data_dir))
        self.store.logger = self.ctx.logger
        self._timezone = self._resolve_timezone()
        self.scheduler = AsyncIOScheduler(timezone=self._timezone)
        self.timed_engine = TimedMessageEngine(self.scheduler, self.ctx.logger)
        await self._ensure_services()
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
        if scope == "self":
            self.ctx.logger.info(
                f"{LOG_PREFIX} 配置已更新（时间/开关类改动需重载插件生效）"
            )

    # ── 服务初始化 ──────────────────────────────────────

    async def _ensure_services(self) -> None:
        if self._services_ready:
            return
        async with self._services_lock:
            if self._services_ready:
                return
            conf = self.config
            if conf.external_services.weather_api_key:
                self.weather_service = WeatherService(
                    {
                        "weather_api_key": conf.external_services.weather_api_key,
                        "weather_city": conf.external_services.weather_city,
                    }
                )

            notion_ids = conf.external_services.notion_db_ids or []
            maton_key = conf.external_services.maton_api_key
            if notion_ids and maton_key:
                transaction_db = reading_db = ""
                for item in notion_ids:
                    if isinstance(item, dict):
                        name = str(item.get("name", "")).strip().lower()
                        db_id = str(item.get("id", "")).strip()
                    elif isinstance(item, str):
                        raw = item.strip()
                        if ":" in raw:
                            name, db_id = (part.strip() for part in raw.split(":", 1))
                            name, db_id = name.lower(), db_id
                        else:
                            name, db_id = "", raw
                    else:
                        continue
                    if name in ("事务", "transaction"):
                        transaction_db = db_id
                    elif name in ("阅读", "reading"):
                        reading_db = db_id
                    elif not transaction_db:
                        transaction_db = db_id
                    elif not reading_db:
                        reading_db = db_id
                self.notion_client = NotionClient(
                    maton_key,
                    transaction_db,
                    reading_db,
                    logger=self.ctx.logger,
                    timezone=self._timezone,
                )

            if conf.calendar_sync.enable_apple_calendar_sync:
                if (
                    conf.calendar_sync.apple_username
                    and conf.calendar_sync.apple_app_password
                ):
                    self.apple_calendar = AppleCalendar(
                        username=conf.calendar_sync.apple_username,
                        app_password=conf.calendar_sync.apple_app_password,
                        calendar_id=conf.calendar_sync.apple_calendar_id.strip()
                        or None,
                        webcal_urls=list(conf.calendar_sync.webcal_urls or []),
                        caldav_host=conf.calendar_sync.apple_caldav_host.strip()
                        or None,
                        timezone=self._timezone,
                    )
                else:
                    self.ctx.logger.warning(
                        f"{LOG_PREFIX} 已开启 Apple 同步但缺少账号或 App 专用密码"
                    )

            self.morning = MorningBriefing(
                self.store,
                self.weather_service,
                self.notion_client,
                self.apple_calendar,
                self._timezone,
            )
            self._services_ready = True
            self.ctx.logger.info(f"{LOG_PREFIX} 外部服务初始化完成")

    # ── 定时任务注册 ────────────────────────────────────

    async def _register_tasks(self) -> None:
        if self._tasks_registered or not self.timed_engine:
            return
        self._tasks_registered = True
        conf = self.config
        habit = conf.habit_reminder

        if habit.enable_morning_report:
            self.timed_engine.register(
                "morning_briefing", habit.morning_report_time, self._on_morning
            )
        if habit.enable_bath_reminder:
            self.timed_engine.register("bath_reminder", habit.bath_time, self._on_bath)
        if habit.enable_sleep_reminder:
            self.timed_engine.register(
                "sleep_reminder", habit.sleep_time, self._on_sleep
            )
        if habit.enable_water_reminder:
            self.timed_engine.register(
                "water_reminder",
                ("interval", {"minutes": max(1, int(habit.water_interval))}),
                self._water_tick,
                max_instances=1,
                coalesce=True,
            )

        if conf.schedule_reminder.enable_schedule_reminder:
            interval = max(
                2, int(conf.schedule_reminder.schedule_reminder_check_interval or 5)
            )
            self.timed_engine.register(
                "schedule_reminder_scan",
                ("interval", {"minutes": interval}),
                self._schedule_scan,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=interval * 60,
            )

        if conf.calendar_sync.enable_apple_calendar_sync and self.apple_calendar:
            interval = max(
                5, int(conf.calendar_sync.apple_calendar_sync_interval or 30)
            )
            self.timed_engine.register(
                "apple_calendar_sync",
                ("interval", {"minutes": interval}),
                self._apple_sync,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )

        self.timed_engine.start()

    # ── 推送：全部交给 Maisaka ──────────────────────────

    async def _push(self, target: str, intent: str, reason: str, kind: str) -> bool:
        return await targets.trigger_proactive(
            self.ctx,
            target,
            intent,
            reason,
            logger=self.ctx.logger,
            metadata={"source": "schedule_assistant", "kind": kind},
        )

    async def _on_morning(self) -> None:
        await self._ensure_services()
        if not self.morning:
            return
        custom = self.config.prompt_settings.prompt_morning
        for target in self._targets():
            _, bare = targets.parse_user_target(target)
            if not bare:
                continue
            try:
                data = await self.morning.build(bare)
            except Exception as exc:
                self.ctx.logger.warning(
                    f"{LOG_PREFIX} 早安数据构建失败 target={target}: {exc}"
                )
                continue
            intent = prompts.render("morning", data, custom)
            await self._push(target, intent, "morning_briefing", "morning")

    async def _on_bath(self) -> None:
        await self._on_habit(
            "bath", self.config.habit_reminder.bath_time, "bath_reminder"
        )

    async def _on_sleep(self) -> None:
        await self._on_habit(
            "sleep", self.config.habit_reminder.sleep_time, "sleep_reminder"
        )

    async def _on_habit(self, kind: str, default_time: str, reason: str) -> None:
        now = datetime.now(self._timezone)
        data = {
            "current_time": now.strftime("%H:%M"),
            "default_time": default_time,
        }
        custom = getattr(self.config.prompt_settings, f"prompt_{kind}", "")
        intent = prompts.render(kind, data, custom)
        for target in self._targets():
            await self._push(target, intent, reason, kind)

    async def _water_tick(self) -> None:
        habit = self.config.habit_reminder
        if not habit.enable_water_reminder:
            return
        now = datetime.now(self._timezone)
        try:
            start_h, start_m = map(int, habit.water_start_time.split(":"))
            end_h, end_m = map(int, habit.water_end_time.split(":"))
        except (ValueError, AttributeError):
            start_h, start_m, end_h, end_m = 9, 30, 21, 30
        now_min = now.hour * 60 + now.minute
        if not (start_h * 60 + start_m <= now_min <= end_h * 60 + end_m):
            return
        data = {"current_time": now.strftime("%H:%M")}
        intent = prompts.render("water", data, self.config.prompt_settings.prompt_water)
        for target in self._targets():
            await self._push(target, intent, "water_reminder", "water")

    # ── 日程提醒 ────────────────────────────────────────

    async def _schedule_scan(self) -> None:
        async with self._scan_lock:
            await self._ensure_services()
            await self._apple_sync()
            now = datetime.now(self._timezone)
            minutes = max(
                1, int(self.config.schedule_reminder.schedule_reminder_minutes or 10)
            )
            for target in self._targets():
                _, bare = targets.parse_user_target(target)
                if not bare:
                    continue
                try:
                    due = await collect_due_schedule_items(
                        self.store, bare, minutes, now
                    )
                except Exception as exc:
                    self.ctx.logger.warning(
                        f"{LOG_PREFIX} 日程扫描失败 target={target}: {exc}"
                    )
                    continue
                if not due:
                    continue
                custom = self.config.prompt_settings.prompt_schedule
                intent = "\n".join(
                    prompts.render(
                        "schedule",
                        {
                            "item_title": item.get("title", ""),
                            "time_label": item.get("time_label", ""),
                            "ahead_label": item.get("ahead_label", ""),
                            "item_context": item.get("context", ""),
                        },
                        custom,
                    )
                    for item in due
                )
                ok = await self._push(target, intent, "schedule_reminder", "schedule")
                if ok:
                    await mark_items_triggered(self.store, bare, due)

    # ── Apple 日历同步 ─────────────────────────────────

    async def _apple_sync(self) -> None:
        async with self._apple_lock:
            if not self.apple_calendar:
                return
            try:
                events, issues = await self.apple_calendar.get_all_events(days=7)
            except Exception as exc:
                self.ctx.logger.error(f"{LOG_PREFIX} Apple 日历读取失败: {exc}")
                return
            if not events:
                return
            reason = "；".join(issues)
            if reason and reason != self._last_apple_issue:
                self.ctx.logger.warning(
                    f"{LOG_PREFIX} Apple 日历读取不完整（{reason}），本轮只做新增与更新"
                )
            self._last_apple_issue = reason or None
            now = datetime.now(self._timezone)
            for target in self._targets():
                _, bare = targets.parse_user_target(target)
                if not bare:
                    continue
                try:
                    stats = await self.store.sync_from_apple_calendar(
                        bare, events, allow_delete=not issues, now=now
                    )
                except Exception as exc:
                    self.ctx.logger.warning(
                        f"{LOG_PREFIX} Apple 同步写入失败 target={target}: {exc}"
                    )
                    continue
                if stats["added"] or stats["updated"] or stats["deleted"]:
                    self.ctx.logger.info(
                        f"{LOG_PREFIX} Apple→本地同步 target={target} {stats}"
                    )

    # ── @Tool: 日程管理（4 个）─────────────────────────

    @Tool(
        "create_schedule",
        description="创建新日程。当用户想要添加一个日程安排时调用。",
        brief_description="创建日程",
        detailed_description=(
            "参数说明：\n"
            "- title：string，必填。日程标题/内容。\n"
            "- datetime_str：string，必填。日期时间，支持「2024-01-15 14:30」「明天9点」"
            "「后天下午3点」「今天晚上8点」；也支持区间「明天9点到11点」，"
            "以及全天「明天全天」（纯日期如「明天」「2024-01-15」即全天）。\n"
            "- end_datetime_str：string，可选。区间日程的结束时间（如「11点」"
            "「2024-01-15 16:30」）；不填则为单时间点。\n"
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
                description="日期时间，如「2024-01-15 14:30」「明天9点」「明天9点到11点」"
                "（区间）「明天全天」（全天）",
                required=True,
            ),
            ToolParameterInfo(
                name="end_datetime_str",
                param_type=ToolParamType.STRING,
                description="可选的结束时间（如「11点」「2024-01-15 16:30」），区间日程用",
                required=False,
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
            kwargs.get("end_datetime_str", ""),
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
            "- datetime_str：string，可选。新时间，支持区间「明天9点到11点」与"
            "全天「明天全天」（纯日期即全天）。\n"
            "- end_datetime_str：string，可选。区间日程的新结束时间，需与 datetime_str 一起给。"
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
                description="新时间（如「明天9点」「明天9点到11点」「明天全天」）",
                required=False,
            ),
            ToolParameterInfo(
                name="end_datetime_str",
                param_type=ToolParamType.STRING,
                description="区间日程的新结束时间（如「11点」），需与 datetime_str 一起给",
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
            kwargs.get("end_datetime_str", ""),
            kwargs.get("message"),
        )


def create_plugin():
    return ScheduleAssistantPlugin()
