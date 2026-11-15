<div align="center">

<h1><img src="https://raw.githubusercontent.com/OMSociety/maibot_plugin_schedule_assistant/main/logo.png" width="192" alt="ScheduleAssistant Logo"><br>Schedule Assistant 日程提醒助手</h1>

**贴心日程管家** —— 早安播报 · 日程智能提醒 · 习惯提醒 · Apple 日历同步 · Notion 待办

[![Version](https://img.shields.io/github/v/tag/OMSociety/maibot_plugin_schedule_assistant?label=version&color=blue)](https://github.com/OMSociety/maibot_plugin_schedule_assistant/releases)
[![MaiBot](https://img.shields.io/badge/MaiBot-%E2%89%A51.4.5-green.svg)](https://github.com/Mai-with-u/MaiBot)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/OMSociety/maibot_plugin_schedule_assistant)](https://github.com/OMSociety/maibot_plugin_schedule_assistant/stargazers)
[![Issues](https://img.shields.io/github/issues/OMSociety/maibot_plugin_schedule_assistant)](https://github.com/OMSociety/maibot_plugin_schedule_assistant/issues)

[核心特性](#核心特性) • [功能概览](#功能概览) • [快速开始](#快速开始) • [配置项说明](#配置项说明) • [LLM 可调用工具](#llm-可调用工具) • [常见问题](#常见问题) • [更新日志](CHANGELOG.md)

</div>

> 本项目由 AI 编写 · 由 AstrBot 插件 [OMSociety/astrbot_plugin_schedule_assistant](https://github.com/OMSociety/astrbot_plugin_schedule_assistant) 迁移而来

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **早安播报** | 每天固定时间把天气 / 今日日程 / Notion 待办交给 Maisaka，由它自然地说一段早安 |
| **日程智能提醒** | 日程临近时把提醒意图交给 Maisaka，由它在会话里拟人化开口，覆盖本地日程与 Apple 日历事件 |
| **习惯提醒** | 洗澡 / 睡觉 / 喝水，同样由 Maisaka 基于人格决定怎么说 |
| **日程管理** | LLM 自然语言创建 / 删除 / 查询 / 修改日程（支持时间区间「9点到11点」与全天日程） |
| **Apple 日历同步** | iCloud CalDAV 双向同步（读取 / 写入 / 删除事件），读写统一使用配置的时区 |
| **Notion 待办** | 待办同步进早安数据（经第三方 Maton 网关中转），失败与空列表分别呈现 |

---

## 功能概览

### 推送一律交给 Maisaka

早安、洗澡、睡觉、喝水、日程提醒都不会由插件直接发送消息，而是调用 `ctx.maisaka.proactive.trigger()`，把意图写进会话的回复生命周期，由 Maisaka 结合当前聊天关系、记忆与人格决定是否开口、怎么说。插件不拦截、不补发。

这意味着：**到点不等于一定收到**。Maisaka 可能选择不打扰；插件只负责把「该提醒什么」准确交给它。

### 早安播报

每天 `morning_report_time` 组装日期、天气、未来 24 小时的本地与 Apple 日程、Notion 待办，渲染成 intent 交给 Maisaka。数据缺失时跳过对应部分，不编造。

### 日程提醒

日程临近时（提前 `schedule_reminder_minutes` 分钟）扫描到期条目，把标题、时间、剩余时间与备注交给 Maisaka；同一轮多个到期日程合并为一次请求。触发成功后写入防重标记，不会同一日程重复轰炸。

### 习惯提醒

洗澡 / 睡觉 / 喝水到点后交给 Maisaka 决定怎么说；喝水在 `water_start_time`–`water_end_time` 之间按 `water_interval` 提醒。

### 日程管理（LLM 工具）

直接说「明天9点开会」/「明天9点到11点开组会」/「后天全天团建」/「删掉后天的组会」，bot 自动调工具。时间支持单点、区间、全天三种形态。

---

## 快速开始

### 第一步：安装

MaiBot WebUI → 插件市场 → 搜索 `schedule_assistant`

### 第二步：配置

1. **基础设置**：填写 `user_ids`（接收提醒的用户列表）与时区
2. **日程提醒**：开启 `enable_schedule_reminder`，设提前量
3. **习惯提醒**：默认开启，可调时间
4. **（可选）外部服务**：心知天气 Key、Notion、Apple 日历

> **`user_ids` 填什么**：每项填 `platform:裸ID`，和全局 `operator`/`permission` 同一种格式（如 `qq:123456`）。`qq`=NapCat（QQ 协议）；接其它适配器填它上报的平台名。自包含、可混平台。

> **时区**：`timezone` 填 IANA 时区名（如 `Asia/Shanghai`）。日程时间、调度、Apple 日历读写都按它解释；填了非法值会告警并回退 `Asia/Shanghai`。

### 第三步：使用

- 发「帮我记一个日程：周五下午3点开组会」 → 自动创建
- 发「我最近有什么安排」 → 列出日程
- 到点由 Maisaka 自然提醒

---

## 配置项说明

### 基础设置

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `user_ids` | list | `[]` | 接收提醒的用户（每项 `platform:裸ID`，如 `qq:123456`） |
| `timezone` | string | `Asia/Shanghai` | IANA 时区名，日程与 Apple 日历读写统一使用 |

### 日程提醒

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `enable_schedule_reminder` | bool | `false` | 开启日程与 Apple 日历事件的提前提醒 |
| `schedule_reminder_minutes` | int | `10` | 提前提醒分钟数 |
| `schedule_reminder_check_interval` | int | `5` | 扫描间隔（分钟，最小 2，建议不大于提前提醒分钟数） |

### 习惯提醒

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `enable_morning_report` | bool | `true` | 早安播报开关 |
| `morning_report_time` | string | `09:00` | 早安时间 |
| `enable_bath_reminder` | bool | `true` | 洗澡提醒 |
| `bath_time` | string | `22:00` | 洗澡时间 |
| `enable_sleep_reminder` | bool | `true` | 睡觉提醒 |
| `sleep_time` | string | `23:00` | 睡觉时间 |
| `enable_water_reminder` | bool | `true` | 喝水提醒 |
| `water_interval` | int | `90` | 喝水间隔（分钟） |
| `water_start_time` / `water_end_time` | string | `09:30`/`21:30` | 喝水时段 |

### 日历同步

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `enable_apple_calendar_sync` | bool | `false` | Apple 日历双向同步 |
| `apple_calendar_sync_interval` | int | `30` | 同步间隔（分钟） |
| `apple_username` / `apple_app_password` / `apple_calendar_id` | string | `""` | Apple ID / App 专用密码 / 日历 ID |
| `webcal_urls` | list | `[]` | WebCal 共享链接 |

### 外部服务

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `maton_api_key` / `notion_db_ids` | string/list | `""`/`[]` | Notion 待办（经第三方 Maton 网关，密钥发往该第三方） |
| `weather_api_key` / `weather_city` | string | `""`/`北京` | 心知天气 |

### 提示词模板

非空的模板会**整段接管**该类推送的 intent（插件不再追加任何约束）；留空使用内置模板。

| 配置项 | 占位符 |
|:------|:-------|
| `prompt_morning` | `{date} {weekday} {weather_current} {weather_forecast} {agenda} {notion_todos}` |
| `prompt_bath` / `prompt_sleep` | `{current_time}` |
| `prompt_water` | `{current_time}` |
| `prompt_schedule` | `{item_title} {time_label} {ahead_label} {item_context}` |

---

## 数据安全与隐私

### WebCal 订阅地址安全（防 SSRF）

> **提示**：`webcal_urls` 只接受**公网**的 `http://` 或 `https://` 订阅地址（`webcal://` 自动转 `https://`）。插件会拒绝 `localhost`、内网（如 `192.168.x` / `10.x`）、链路本地、云元数据（`169.254.169.254`）等地址（防 SSRF）。请勿填写内网或本机地址。
>
> **注意**：安全边界：对**域名**仅做主机名黑名单校验，**请求前不解析 DNS**，因此理论上仍存在 DNS rebinding（域名在校验通过后被解析到内网地址）的残余风险；对 **IP 字面量**则会完整校验私网 / 环回 / 链路本地 / 保留段。若部署环境对安全要求较高，建议在网络层（防火墙 / 出站代理白名单）再挡一道。

### Notion 待办经第三方 Maton 网关中转（非直连）

本插件的 Notion 待办功能**默认通过第三方服务 [Maton](https://maton.ai) 的网关 `gateway.maton.ai` 中转**，**并非直连 Notion 官方 API**：

- 你在「外部服务 → Maton API Key」里填的 `maton_api_key`，会被作为 `Authorization: Bearer <key>` 头发送给 `gateway.maton.ai`（第三方服务器），而不是 `api.notion.com`；
- 也就是说，**这个 Key（及其对应的 Notion 访问权限）会离开你的机器，交给 Maton 这个第三方**；
- 因此，Maton 能访问插件为拉取待办而查询的数据：你配置的「事务」「阅读」数据库里的**标题、状态（进度）、截止日期**等字段内容。

## LLM 可调用工具

| 工具 | 说明 | 关键参数 |
|:-----|:-----|:---------|
| `create_schedule` | 创建日程（单点 / 区间 / 全天） | title / start / end / all_day / description |
| `delete_schedule` | 删除日程 | schedule_id / title_keyword |
| `list_schedules` | 查看未来几天的日程 | days（缺省 7） |
| `update_schedule` | 修改日程（单点 / 区间 / 全天） | schedule_id / title / start / end / all_day |

> **时间写法**：`start` / `end` 使用 ISO 8601 本地时间（`2026-11-16T09:00`）；全天日程只填日期（`2026-11-16`）并把 `all_day` 设为 `true`。过去的开始时间会被拒绝；中文口语由模型换算成 ISO 后再调用工具。

```
用户: 帮我记一个日程，明天下午3点开组会，记得带电脑
-> create_schedule(title="组会", start="2026-09-02T15:00", description="记得带电脑")
    ✅ 已创建日程「组会」，时间：09-02 15:00

用户: 明天9点到11点开组会
-> create_schedule(title="组会", start="2026-09-02T09:00", end="2026-09-02T11:00")
    ✅ 已创建日程「组会」，时间：09-02 09:00-11:00

用户: 后天全天团建
-> create_schedule(title="团建", start="2026-09-03", all_day=true)
    ✅ 已创建日程「团建」，时间：09-03 全天

用户: 我下周有什么安排
-> list_schedules(days=7)
    📋 最近 7 天的日程：
    ━━━ 09-01 周一 ━━━
      ⏰ 14:30 │ 学术英语
```

---

## 常见问题

**Q：提醒没收到？**
A：主动推送通过 `user_ids`（`platform:裸ID`）定位你的**私聊流**（插件用 `get_stream_by_user_id` 取到聊天流再交给 Maisaka）。请确认：
- `user_ids` 填 `platform:裸ID`（如 `qq:123456`，`qq`=NapCat）；
- 你**必须先私聊过 bot**（才有聊天流）；
- 群聊主动推送暂不支持。

**Q：为什么到点没有收到提醒？**
A：所有推送都交给 Maisaka 决定是否开口（`proactive.trigger`），它会结合人格与语境选择不打扰，插件不补发、不拦截。这是 2.0 的设计取舍；需要「一定送达」的提醒暂时无法由本插件保证。

**Q：Apple 日历的日程也能提前提醒吗？**
A：能，需要同时满足：① `enable_schedule_reminder` 开启并设好提前量；② `enable_apple_calendar_sync` 开启并配好 Apple ID / App 专用密码或 WebCal 地址。每轮提醒扫描前会触发一次同步；同步只增量合并，**不会删除同步窗口（前 1 天到后 7 天）之外的本地日程**，手机上新加或改期的事件也能赶上提前提醒。

**Q：Apple 日历怎么配？**
A：两种方式任选：Apple ID + **App 专用密码**（appleid.apple.com → 安全性 → App 专用密码），填 `apple_username / apple_app_password`；或只填 `webcal_urls` 订阅公开日历。读写时区由 `timezone` 决定。

**Q：数据存在哪？**
A：MaiBot 插件数据目录 `data/plugins/omsociety.schedule-assistant/schedule_data.json`。

## 支持与致谢

如果这个插件对你有帮助，欢迎点亮 Star，有问题和建议请提交 [Issue](https://github.com/OMSociety/maibot_plugin_schedule_assistant/issues) 或 [Pull Request](https://github.com/OMSociety/maibot_plugin_schedule_assistant/pulls)。

- [MaiBot](https://github.com/Mai-with-u/MaiBot) 开源聊天机器人框架
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 上游 AstrBot 插件框架

## 许可证与作者

本项目采用 **AGPL-3.0** 开源协议。

[@OMSociety](https://github.com/OMSociety)
