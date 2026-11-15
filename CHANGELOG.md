# Changelog

本项目的更改记录在此文件。

All notable changes to this project are documented in this file.

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-11-16

### 新增

- 全托管推送：早安、洗澡、睡觉、喝水与日程提醒统一交给 Maisaka 的 `proactive.trigger` 处理，插件不再直接发送消息，也不再补发。
- 基础设置新增 `timezone`（IANA 时区名，默认 `Asia/Shanghai`），调度、日程时间解释与 Apple 日历读写统一使用该时区；Apple 写入的 `TZID` 跟随配置，不再硬编码。
- 五份 intent 模板：`prompt_morning`、`prompt_bath`、`prompt_sleep`、`prompt_water`、`prompt_schedule`；非空模板整段接管，插件不再追加约束。
- Notion 待办三态：未配置不出现待办行，读取失败显示「获取失败」，成功但为空显示「暂无」。
- 日程目标用户只来自 `user_ids`，配置格式为 `platform:裸ID`。

### 变更

- 早安播报改为装配数据后交给 Maisaka 开口，不再由插件调用 LLM 直发；移除称呼与「昨夜入睡」数据。
- 推送统一为纯文本，移除 Markdown 渲染与 `qq_markdown` 降级路径。
- 日程提醒触发成功即写防重标记，不再依赖补发结果。
- 时间解析修正：「晚上12点」解释为次日 00:00，「中午12点」保持 12:00。
- 插件清单基线升级：宿主 `1.4.5–1.4.99`、SDK `2.23.0–2.23.99`；capabilities 收敛为 `chat.get_stream_by_user_id`、`maisaka.proactive.trigger`、`config.get`。

### 移除

- 移除 `enable_schedule_reminder_fallback` 与插件直发补发链路。
- 移除 `message_render.markdown_enabled`、`persona_hint`、`user_nickname` 配置。
- 移除 habits / water_last 存储键、`_users` 已知用户兜底、`temp_override` / `recur` / `snoozed_until` 字段与清理任务。

### Added

- Fully delegated delivery: morning briefing, bath, sleep, water and schedule reminders are all handed to Maisaka through `proactive.trigger`; the plugin no longer sends messages directly or re-sends them.
- New `timezone` setting (IANA name, default `Asia/Shanghai`) shared by scheduling, schedule-time interpretation and Apple CalDAV reads/writes; the Apple `TZID` now follows the configured zone instead of the hard-coded value.
- Five intent templates: `prompt_morning`, `prompt_bath`, `prompt_sleep`, `prompt_water`, `prompt_schedule`; a non-empty template takes over completely with no plugin-appended constraints.
- Notion todos now distinguish three states: not configured (no todo line), read failure (`获取失败`), and empty (`暂无`).
- Target users come only from `user_ids`, each entry in `platform:bare-id` form.

### Changed

- The morning briefing assembles data and hands it to Maisaka instead of calling an LLM and sending the result itself; the greeting name and late-night data are gone.
- All pushes are plain text; Markdown rendering and the `qq_markdown` fallback path were removed.
- A schedule reminder marks the item as triggered once the proactive request is accepted, independent of any fallback result.
- Time parsing fix: `晚上12点` now means the next day at 00:00, while `中午12点` stays at 12:00.
- Manifest baseline raised to host `1.4.5–1.4.99` and SDK `2.23.0–2.23.99`; capabilities reduced to `chat.get_stream_by_user_id`, `maisaka.proactive.trigger`, `config.get`.

### Removed

- Removed `enable_schedule_reminder_fallback` and the plugin-side direct-send fallback path.
- Removed the `message_render.markdown_enabled`, `persona_hint` and `user_nickname` settings.
- Removed the habits / water_last storage keys, the `_users` known-user fallback, the `temp_override` / `recur` / `snoozed_until` fields, and the expired-override cleanup job.

## [1.3.0] - 2026-11-04

### 新增

- CalDAV 服务器地址可配（`apple_caldav_host`，默认 `caldav.icloud.com`）：留空使用默认入口，填了就用填的；默认入口被拒绝认证时自动再试一次中国大陆区入口 `caldav.icloud.com.cn`，并说明实际生效的入口。
- 首次成功发现日历时，日志以 INFO 列出「可用日历（apple_calendar_id 可填下列 UUID）」，逐个给出日历名称与 UUID。

### 修复

- **读取 Apple 日历不再丢弃 HTTP 状态码**：凭据未接受（401）、App 专用密码错误或失效（403）、网络不通、响应里没有 principal URL 分别给出可执行的原因，同一原因只报一次错误，不再笼统显示「CalDAV 未连接，无法创建事件」。
- **读取不完整时不再删除本地日程**：CalDAV 未连接、列出日历失败、某个日历抓取失败、某个事件文件没拉到、WebCal 读取失败，任一环节失败都只做新增与更新，本轮保留本地日程；读取恢复后才恢复删除。
- WebCal 读取按状态码判定成败，403/404 的错误页不再被当成「0 个日程」。
- 工具回执不再虚报成功：没写入 Apple 日历、没更新 Apple 事件、没从 Apple 删除时如实说明。

### 变更

- `get_all_events` 改为返回事件列表与读取问题列表；`sync_from_apple_calendar` 必须显式传入是否允许删除。
- 删除本地日程时以 INFO 说明条数；本轮未获删除授权时以 DEBUG 说明保留。
- 创建、更新、删除时「CalDAV 未连接」由 ERROR 降为 WARNING（失败原因已由发现过程报出）。
- `apple_calendar_id` 的配置提示改为指向日志里的可用日历清单。

### Added

- Configurable CalDAV server address (`apple_caldav_host`, default `caldav.icloud.com`): leave it empty to use the default entry, or fill in your own; when the default entry rejects the credentials the plugin retries the mainland China entry `caldav.icloud.com.cn` once and states which entry is in effect.
- On the first successful calendar discovery the log lists the available calendars at INFO level with their names and UUIDs, so the value for `apple_calendar_id` can be read off directly.

### Fixed

- **Reading Apple calendars no longer discards HTTP status codes**: unaccepted credentials (401), a wrong or revoked app-specific password (403), an unreachable network, and a response without a principal URL now each produce an actionable reason, reported once per cause, instead of the generic "CalDAV not connected, cannot create event".
- **Local schedules are no longer deleted when a read is incomplete**: CalDAV being unreachable, listing calendars failing, one calendar's fetch failing, one event file missing, or a WebCal read failing now all limit the round to additions and updates, keeping local schedules; deletions resume only after reads recover.
- WebCal reads judge success by status code, so 403/404 error pages are no longer treated as "0 events".
- Tool replies no longer claim success prematurely: a schedule that was not written to, updated in, or deleted from Apple Calendar is reported as such.

### Changed

- `get_all_events` now returns the event list together with a list of read problems; `sync_from_apple_calendar` requires an explicit delete permission.
- Deleting local schedules is logged at INFO with the count; a round without delete permission logs at DEBUG why schedules were kept.
- "CalDAV not connected" during create, update, and delete is logged at WARNING instead of ERROR (the cause has already been reported by the discovery step).
- The `apple_calendar_id` hint now points at the available-calendars list in the log.

## [1.2.3] - 2026-10-25

### 变更

- 收窄主程序兼容上界：`host_application.max_version` 由 `1.99.99` 改为 `1.3.99`，兼容范围落定为 MaiBot 1.0.0 至 1.3.x。

### Changed

- Narrowed the host compatibility upper bound: `host_application.max_version` changed from `1.99.99` to `1.3.99`, fixing the supported range at MaiBot 1.0.0 through 1.3.x.

## [1.2.2] - 2026-10-23

### 修复

- **日程提醒可能一条都收不到**：麦麦没有开口时，插件此前仍会写入防重标记，这条日程本期不再提醒；开启补发（见下）后改为确认送达才写标记。
- 读取 `maisaka.proactive.trigger` 的返回值：宿主拒绝（如会话已不存在）时返回 `{"success": false}` 而不抛异常，此前会被当作触发成功。

### 新增

- 日程提醒兜底送达（`enable_schedule_reminder_fallback`，默认关闭）：开启后请求麦麦开口并等一轮扫描，若会话里没有麦麦发出的消息，插件直接补发一条纯文本提醒，且只有确认送达才写防重标记。

### Fixed

- **Schedule reminders could get lost entirely**: the de-duplication mark used to be written even when Maisaka never spoke, so that schedule stopped reminding for the whole period; with fallback delivery on (see below) the mark is now written only after delivery is confirmed.
- Read the return value of `maisaka.proactive.trigger`: the host returns `{"success": false}` instead of raising when it rejects the request (e.g. the session is gone), which used to be treated as success.

### Added

- Fallback delivery for schedule reminders (`enable_schedule_reminder_fallback`, off by default): when enabled, the plugin asks Maisaka to speak and waits one scan round, then sends a plain-text reminder itself if no Maisaka message appeared in the session, and only writes the de-duplication mark after delivery is confirmed.

## [1.2.1] - 2026-09-25

### 修复

- **Apple 日历写操作按 HTTP 状态码判定成败**：此前 4xx（凭据失效、日历只读）会被当作成功并写入无效 UID，下一轮同步再据此把本地日程静默删除。
- 创建成功的提示只在真正写入 Apple 日历后出现。
- **修正 iCloud 降级提取路径的三条正则**：此前的双重转义让它们永远匹配不到，XML 解析失败时静默失效且不留日志；相对地址清洗的同型问题一并修正，失败路径改为记录告警。
- 修改带 UID 的日程会把改动写回同一个日历事件，不再被下一轮同步用 Apple 旧值还原。
- **提醒的防重标记改为确认触发成功后才写入**，`collect_due_schedule_items` 的默认行为同步改为「只挑选、不落盘」。
- `list_schedules` 的 `date="0"` 不再抛内部异常。

### 变更

- 提醒触发失败时不再消耗该日程本期的提醒机会（改为下一轮重试）。
- Apple 写回失败时该日程会脱离 Apple 同步：本地改动得以保留，代价是 Apple 侧的旧事件可能在下轮同步中作为新事件拉回，形成一条重复日程。

### Fixed

- **Apple Calendar write operations now judge success by HTTP status code**: a 4xx (invalid credentials, read-only calendar) used to be treated as success and an invalid UID was written, on which the next sync round then silently deleted the local schedule.
- The notice for a successful creation now appears only after the event has really been written to Apple Calendar.
- **Fixed the three regular expressions in the iCloud fallback extraction path**: the previous double escaping made them never match, so they failed silently and left no log when XML parsing failed; the same problem in the relative-address sanitization was fixed as well, and the failure paths now log a warning.
- Editing a schedule that has a UID now writes the change back to the same calendar event, so it is no longer restored to the old Apple value by the next sync round.
- **The reminder de-duplication mark is now written only after the trigger is confirmed successful**, and the default behavior of `collect_due_schedule_items` was changed accordingly to "select only, write nothing".
- `date="0"` for `list_schedules` no longer raises an internal exception.

### Changed

- A failed reminder trigger no longer consumes that schedule's reminder opportunity for the current period (it is retried on the next round).
- When an Apple write-back fails, that schedule leaves Apple sync: the local change is preserved, at the cost that the old event on the Apple side may be pulled back as a new event in the next sync round, forming a duplicate schedule.

## [1.2.0] - 2026-09-23

### 新增

- **Apple 日历事件提前提醒**：开启日程提醒与 Apple 日历同步后，手机侧新建 / 改期的事件同样在开始前提前提醒（扫描前触发一次同步，300 秒内的重复拉取走缓存）。
- **日程工具支持时间区间与全天日程**：创建 / 修改日程可写「明天9点到11点」（区间）、「明天全天」或纯日期（全天）；两端纯日期的区间（「明天到后天」）按全天处理，多日取开始日；全天事件写入 Apple 日历为全天事件，单时间点模式行为不变（Apple 日历按开始后 1 小时）。

### 修复

- **修复定时扫描空转**：锁获取写法 `asyncio.wait_for(lock.acquire(), timeout=0)` 在该语义下必超时（空闲锁也一样），日程提醒扫描与 Apple 定时同步此前整体不执行；现改为非阻塞抢锁，锁被占用时跳过本轮。
- 修复提前量窗口漏提醒：判定由「开始前 N 分钟 ±2 分钟」的窄窗口改为「开始前 N 分钟内均触发」（窗口含上边界，扫描间隔不大于提前提醒分钟数时必达）。
- 修复长提前量下同一事件重复提醒：防重标记此前 1 小时后自动失效，提前量大于约 1 小时时事件会在开始前 5 分钟兜底窗口被再次提醒；现防重标记持续有效（事件改期时重置，改期重新提醒）。
- 修复全天 / 日期型日程在「查看日程」中被静默跳过的问题。
- 修复「明天下午3点」这类带时段词的口语时间表达解析失败的问题。

### 变更

- 日程提醒（本地日程与 Apple 日历事件）改为 Maisaka 拟人开口（`maisaka.proactive.trigger`，intent 注入回复生命周期），同一轮的多个事件合并为一次提醒；早安播报保持固定格式直发。
- 早安播报与日程列表的时间标签支持区间（15:00-16:30）与全天显示。
- 扫描间隔配置提示补充「建议不大于提前提醒分钟数」。

### 移除

- 配置项「日程提醒模板」（`prompt_schedule`）：日程提醒措辞由 Maisaka 按人格生成。

## [1.1.1] - 2026-09-18

### 修复

- **修复 LLM 文本生成在 maibot-plugin-sdk 2.8.1 上全部失败**：`ctx.llm.generate` 的模型任务槽改由 `task_name` 传入（此前用 `model="replyer"`）。SDK 每次调用都会带上 `task_name`，宿主据此把 `model` 参数当作具体模型名去 `[[models]]` 查找，因此报「未找到名为 'replyer' 的模型」，早安播报、日程播报与提醒文案均退化为兜底文本。

## [1.1.0] - 2026-09-14

### 新增

- **配置界面多语言（en-US / ja-JP）**：WebUI 配置页的字段标题、提示与占位文本、分区标题与分区描述按界面语言自动切换（宿主界面语言支持中文/英文/日文/韩文，无对应译文时回退中文），manifest 声明 `supported_locales`。
- 新增配置界面 i18n 回归测试：离线生成配置 Schema，校验各语言覆盖率、数值一致与技术标识符保全。

### 修复

- 修复 Apple 日历同步可能重复处理同一事件实例的问题：uid 去重提前到解析阶段，无论当轮分支结果如何都标记已处理，重复实例在下一轮循环即被拦下。

### 变更

- 天气查询默认城市由「杭州」改为「北京」（配置默认值与兜底值同步调整）。

## [1.0.1] - 2026-09-06

### 修复

- **修复存储键身份不一致**：定时路径曾以配置原值（如 `qq:123456`）作存储键，而 LLM 工具写入用裸 ID，导致早安播报读不到工具创建的日程。现统一以裸 ID 为键（发送时按配置还原平台），并对旧数据键自动迁移。
- 修复 QQ 官方 markdown 发送抛异常时不降级纯文本的问题（此前仅覆盖返回失败值的路径）。

### 变更

- 移除已无调用的死代码：整条未启用的 LLM 习惯提醒路径与存储层 8 个零调用方法、消息模块兼容桩（行为不变）。
- LLM 熔断期兜底文案改为随每次生成显式传入，防止多提醒场景串用。
- Maisaka 主动触发逻辑两处重复实现合并为一。
