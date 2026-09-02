<div align="center">

<img src="https://raw.githubusercontent.com/OMSociety/maibot_plugin_schedule_assistant/main/logo.png" width="120" alt="ScheduleAssistant Logo" />

# 📅 Schedule Assistant 日程提醒助手

**贴心日程管家** —— 早安播报 · 日程智能提醒 · 习惯提醒 · Apple 日历同步 · Notion 待办

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/OMSociety/maibot_plugin_schedule_assistant)
[![MaiBot](https://img.shields.io/badge/MaiBot-%E2%89%A51.0-green.svg)](https://github.com/Mai-with-u/MaiBot)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/OMSociety/maibot_plugin_schedule_assistant)](https://github.com/OMSociety/maibot_plugin_schedule_assistant/stargazers)
[![Issues](https://img.shields.io/github/issues/OMSociety/maibot_plugin_schedule_assistant)](https://github.com/OMSociety/maibot_plugin_schedule_assistant/issues)

[✨ 核心特性](#-核心特性) • [📖 功能概览](#-功能概览) • [🚀 快速开始](#-快速开始) • [⚙️ 配置项说明](#️-配置项说明) • [🛠️ LLM 可调用工具](#️-llm-可调用工具) • [⚠️ 常见问题](#️-常见问题) • [📝 更新日志](CHANGELOG.md)

</div>

> 🎨 本项目由 AI 编写 · 由 AstrBot 插件 [OMSociety/astrbot_plugin_schedule_assistant](https://github.com/OMSociety/astrbot_plugin_schedule_assistant) 迁移而来

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| 🌅 **早安播报** | 每天固定时间推送（天气 / 今日日程 / Notion 待办），Markdown 精美排版 |
| 📌 **日程智能提醒** | LLM 生成自然语言提醒，日程临近时**百分百推送**（固定格式直发） |
| 🚿 **习惯提醒** | 洗澡 / 睡觉 / 喝水，由 Maisaka 基于人格**拟人化开口** |
| 🗓️ **日程管理** | LLM 自然语言创建 / 删除 / 查询 / 修改日程（明天9点、后天下午3点都能懂） |
| 🔄 **Apple 日历同步** | iCloud CalDAV 双向同步（读取 / 写入 / 删除事件） |
| 📋 **Notion 待办** | 待办同步进早安播报（Maton 代理） |

---

## 📖 功能概览

### 早安播报（固定格式）
每天 `morning_report_time` 推送：称呼语 + 日期 + 天气 + 今日日程表格 + 待办表格，Markdown 排版。

### 日程提醒（固定格式）
日程临近时（提前 `schedule_reminder_minutes` 分钟）LLM 生成提醒文本并直接发送。

### 习惯提醒（Maisaka 拟人开口）
洗澡 / 睡觉 / 喝水到点后，调用 `ctx.maisaka.proactive.trigger()` 让 Maisaka 基于人格自己决定怎么说、说多少。

### 日程管理（LLM 工具）
直接说"明天9点开会" / "删掉后天的组会" / "我最近有什么安排"，bot 自动调工具。

---

## 🚀 快速开始

### 第一步：安装

**方式一：插件市场**
- MaiBot WebUI → 插件市场 → 搜索 `schedule_assistant`

**方式二：手动安装**
- 克隆仓库到 MaiBot 的 `plugins/` 目录：

```bash
git clone https://github.com/OMSociety/maibot_plugin_schedule_assistant.git plugins/maibot_plugin_schedule_assistant
```

> 💡 插件依赖（apscheduler / aiohttp / python-dateutil）在 `_manifest.json` 中声明，MaiBot 启动时自动安装。

### 第二步：配置

1. **基础设置**：填写 `user_ids`（接收提醒的用户 ID 列表）

> 🔑 **`user_ids` 填什么**：每项填 **`platform:裸ID`**，和全局 `operator`/`permission` 同一种格式（如 `qq:123456`）。`qq`=QQ 官方/NapCat；接其它适配器填它上报的平台名。也兼容**裸 ID**（如 `123456`），此时用下方 `platform` 作为默认平台——推荐直接写 `platform:ID`，自包含、可混平台。
2. **日程提醒**：开启 `enable_schedule_reminder`，设提前量
3. **习惯提醒**：默认开启，可调时间
4. **（可选）外部服务**：心知天气 Key（早安播报天气）、Notion、Apple 日历

### 第三步：使用

- 发"帮我记一个日程：周五下午3点开组会" → 自动创建
- 发"我最近有什么安排" → 列出日程
- 到点自动收提醒

---

## ⚙️ 配置项说明

| 分组 | 配置项 | 类型 | 默认值 | 说明 |
|:-----|:-------|:-----|:-------|:-----|
| 基础设置 | `persona_hint` | string | `""` | 可选语气补充（人格本体由 MaiBot 全局提供） |
| 基础设置 | `user_nickname` | string | `""` | 播报称呼（留空用「主人」） |
| 基础设置 | `user_ids` | list | `[]` | 接收提醒的用户（每项 `platform:裸ID`，如 `qq:123456`；裸 ID 用下面 platform 默认平台） |
| 基础设置 | `platform` | string | `"qq"` | 默认平台（`user_ids` 里写裸 ID 时用；`qq`=QQ 官方/NapCat，接其它适配器填它上报的平台名） |
| 日程提醒 | `enable_schedule_reminder` | bool | `false` | 开启日程智能提醒 |
| 日程提醒 | `schedule_reminder_minutes` | int | `10` | 提前提醒分钟数 |
| 日程提醒 | `schedule_reminder_check_interval` | int | `5` | 扫描间隔（分钟，最小 2） |
| 习惯提醒 | `enable_morning_report` | bool | `true` | 早安播报开关 |
| 习惯提醒 | `morning_report_time` | string | `09:00` | 早安时间 |
| 习惯提醒 | `enable_bath_reminder` | bool | `true` | 洗澡提醒（Maisaka） |
| 习惯提醒 | `bath_time` | string | `22:00` | 洗澡时间 |
| 习惯提醒 | `enable_sleep_reminder` | bool | `true` | 睡觉提醒（Maisaka） |
| 习惯提醒 | `sleep_time` | string | `23:00` | 睡觉时间 |
| 习惯提醒 | `enable_water_reminder` | bool | `true` | 喝水提醒（Maisaka） |
| 习惯提醒 | `water_interval` | int | `90` | 喝水间隔（分钟） |
| 习惯提醒 | `water_start_time` / `water_end_time` | string | `09:30`/`21:30` | 喝水时段 |
| 日历同步 | `enable_apple_calendar_sync` | bool | `false` | Apple 日历双向同步 |
| 日历同步 | `apple_calendar_sync_interval` | int | `30` | 同步间隔（分钟） |
| 日历同步 | `apple_username` / `apple_app_password` / `apple_calendar_id` | string | `""` | Apple ID / App 专用密码 / 日历 ID |
| 日历同步 | `webcal_urls` | list | `[]` | WebCal 共享链接 |
| 外部服务 | `maton_api_key` / `notion_db_ids` | string/list | `""`/`[]` | Notion 待办 |
| 外部服务 | `weather_api_key` / `weather_city` | string | `""`/`杭州` | 心知天气 |
| 消息渲染 | `markdown_enabled` | bool | `true` | Markdown 渲染（QQ 官方走 qq_markdown 结构化消息） |
| 提醒 Prompt | `prompt_morning` | string | `""` | 早安播报模板。占位符：`{username} {date} {weekday} {weather_current} {weather_forecast} {agenda} {notion_todos} {late_night}` |
| 提醒 Prompt | `prompt_schedule` | string | `""` | 日程提醒模板。占位符：`{item_title} {time_label} {ahead_label} {item_context}` |

> 💡 **WebCal 订阅安全**：`webcal_urls` 只接受公网 `https://` 订阅地址（`webcal://` 自动转 `https://`）。插件会拒绝 `localhost`、内网（如 `192.168.x` / `10.x`）、云元数据（`169.254.169.254`）等地址（防 SSRF）。请勿填写内网或本机地址。

## 🛠️ LLM 可调用工具

| 工具 | 说明 | 关键参数 |
|:-----|:-----|:---------|
| `create_schedule` | 创建日程 | title / datetime_str / description |
| `delete_schedule` | 删除日程 | schedule_id / title_keyword |
| `list_schedules` | 查看日程 | date（缺省今天） |
| `update_schedule` | 修改日程 | schedule_id / title / datetime_str |

```
用户: 帮我记一个日程，明天下午3点开组会，记得带电脑
🤖 → create_schedule(title="组会", datetime_str="明天下午3点", description="记得带电脑")
    ✅ 已创建日程「组会」，时间：09-02 15:00

用户: 我下周有什么安排
🤖 → list_schedules(date="7")
    📋 接下来7天日程（共3个）：
    ━━━ 09-01 周一 ━━━
      ⏰ 14:30 │ 学术英语
```

---

## ⚠️ 常见问题

**Q：提醒没收到？**
A：主动推送通过 `user_ids`（裸用户 ID）定位你的**私聊流**（插件用 `get_stream_by_user_id` 取到 Session ID 再发送）。所以：
- `user_ids` 填 `platform:裸ID`（如 `qq:123456`，`qq`=QQ 官方/NapCat；裸 ID 则用下面 `platform` 默认平台）；
- 你**必须先私聊过 bot**（才有聊天流）；
- 群聊场景暂不支持主动推送（可后续扩展 `get_stream_by_group_id`）。

**Q：为什么洗澡/睡觉/喝水提醒有时没响？**
A：这三类提醒由 Maisaka 拟人化开口（`proactive.trigger`），Maisaka 会根据人格和语境决定是否说话——**可能选择不打扰**。这是设计取舍；早安播报和日程提醒是固定格式直发，**保证送达**。

**Q：播报语气和麦麦日常不一样？**
A：不会。插件通过 `ctx.config.get("personality.*")` 读取 MaiBot 全局人格，播报和日常用同一份人格。

**Q：Apple 日历怎么配？**
A：需要 Apple ID + **App 专用密码**（appleid.apple.com → 安全性 → App 专用密码），填 `apple_username / apple_app_password`。

**Q：数据存在哪？**
A：MaiBot 插件数据目录 `data/plugins/omsociety.schedule-assistant/schedule_data.json`。

---

## ⭐ 支持本项目

如果这个插件对你有帮助，欢迎点亮 Star ⭐，有问题和建议请提交 [Issue](https://github.com/OMSociety/maibot_plugin_schedule_assistant/issues) 或 [Pull Request](https://github.com/OMSociety/maibot_plugin_schedule_assistant/pulls)。

## 🙏 致谢

- [MaiBot](https://github.com/Mai-with-u/MaiBot) 开源聊天机器人框架
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 上游 AstrBot 插件框架

---

## 📜 许可证

本项目采用 **AGPL-3.0** 开源协议。

---

## 👤 作者

[@OMSociety](https://github.com/OMSociety)
