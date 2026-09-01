"""
日程提醒模块（MaiBot 插件版）

只扫描 schedule 类型的日程，habit 类型（洗澡/睡觉/喝水）由独立定时任务处理，
避免同一条目被多次提醒。LLM 生成端走固定格式直发（100% 回复保证，
见 plugin.py 的 _schedule_reminder_scan）。
"""

import logging
from datetime import datetime
from typing import Any

from ..constants import BROADCAST_MD_OVERRIDE, LOG_PREFIX

logger = logging.getLogger(__name__)


class ScheduleReminder:
    """
    日程 LLM 提醒生成器

    注入信息：
    - 日程名称、时间、备注/描述
    - 提前分钟数
    - 近期对话上下文
    """

    def __init__(self, llm_service):
        self.llm = llm_service

    def _build_prompt(
        self,
        item_title: str,
        item_time: str,
        item_context: str,
        minutes_ahead: int,
        conv_history: str,
    ) -> str:
        """构建 LLM 提醒 prompt"""

        prompt = f"""【重要】你的所有回复必须严格遵循系统人格设定。如果系统人格部分为空，则用你默认的对话风格。。

有一个日程要开始了，生成提醒文本：

日程信息：
  - 名称：{item_title}
  - 时间：{item_time}
  - 备注：{item_context or "（无）"}


提前提醒时间：{minutes_ahead} 分钟

近期对话上下文：
{conv_history}

【要求】
1. 语气和风格严格遵循系统人格设定
2. 自然关心用户，语气温柔
3. 如果备注有具体内容，融入提醒中
4. 30~80 字以内，不要太长
"""
        return prompt.strip()

    async def generate_reminder_text(
        self,
        item_title: str,
        item_time: str,
        item_context: str,
        minutes_ahead: int = 10,
        conv_history: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """生成提醒文本（带 LLM fallback）"""

        conv_str = conv_history or "（无近期对话历史）"

        prompt = self._build_prompt(
            item_title=item_title,
            item_time=item_time,
            item_context=item_context,
            minutes_ahead=minutes_ahead,
            conv_history=conv_str,
        )

        try:
            # prompt 已含 conv_history，不再额外传 history= 避免重复注入
            resp = await self.llm.generate(
                prompt, umo=user_id, extra_system=BROADCAST_MD_OVERRIDE
            )
            text = resp.strip() if resp else None
            if text and len(text) > 5:
                logger.debug(f"{LOG_PREFIX} LLM 提醒生成成功: {text[:30]}...")
                return text
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} LLM 提醒生成失败: {e}")

        return f"📅 提醒：「{item_title}」即将开始，记得准备哦~"


def _parse_time(time_str: str) -> datetime | None:
    """解析时间字符串为 datetime，支持 ISO 格式、时区后缀和普通格式"""
    if not time_str:
        return None
    s = time_str.strip()
    # 优先使用 fromisoformat（原生支持 ISO 8601，含时区）
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, TypeError):
        pass
    # 再尝试普通格式
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%H:%M"]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _is_all_day_event(item) -> bool:
    """判断是否为全天事件"""
    # 优先检查 all_day 标记
    if getattr(item, "all_day", False):
        return True
    # 检查时间格式：YYYY-MM-DD 表示全天
    t = (item.time or "").strip()
    if len(t) == 10 and t.count("-") == 2:
        try:
            datetime.strptime(t, "%Y-%m-%d")
            return True
        except ValueError:
            pass
    return False


async def check_and_trigger_schedule_reminder(
    schedule_store,
    llm_service,
    user_id: str,
    minutes_window: int = 30,
    minutes_before: int = 15,
    reminder: "ScheduleReminder | None" = None,
) -> list[dict[str, Any]]:
    """
    扫描即将到来的日程（仅 schedule 类型）并生成提醒。

    habit 类型（洗澡/睡觉/喝水）由独立定时任务处理，不在此扫描，避免重复提醒。

    提醒时机：
    - 提前提醒：在日程开始前 minutes_before ±2 分钟时触发（可配置，0 表示关闭）
    - 即将开始兜底：前 5 分钟内也会触发
    - 全天事件不触发提前提醒
    """
    # 复用调用方已创建的实例，避免每轮扫描重复构造
    reminder = reminder or ScheduleReminder(llm_service)
    triggered = []
    now = datetime.now()

    all_items = await schedule_store.list_all_items(user_id)

    for item in all_items:
        if not item.enabled:
            continue

        # 跳过习惯类型：洗澡/睡觉/喝水已有独立定时任务，避免重复提醒
        if item.type == "habit":
            continue

        # 跳过全天事件（提前提醒不适用）
        if _is_all_day_event(item):
            continue

        item_dt = _parse_time(item.time)

        if not item_dt:
            continue

        minutes_until = (item_dt - now).total_seconds() / 60

        # 检查是否已触发过（1小时内避免重复）
        if item.last_triggered:
            try:
                last_dt = datetime.fromisoformat(item.last_triggered)
                if (now - last_dt).total_seconds() > 3600:
                    item.last_triggered = None
            except (ValueError, TypeError):
                pass

        if item.last_triggered:
            continue

        # 判断是否需要触发提醒
        should_trigger = False
        trigger_minutes = 0

        # 1. 提前提醒：日程开始前 minutes_before ±2 分钟
        if minutes_before > 0 and abs(minutes_until - minutes_before) <= 2:
            should_trigger = True
            trigger_minutes = int(minutes_until)

        # 2. 即将开始兜底：前 5 分钟内
        if not should_trigger and 0 <= minutes_until <= 5:
            should_trigger = True
            trigger_minutes = int(minutes_until)

        if not should_trigger:
            continue

        conv_history = schedule_store.format_history_for_prompt(
            await schedule_store.get_conversation_history(user_id)
        )

        reminder_text = await reminder.generate_reminder_text(
            item_title=item.title,
            item_time=item.time,
            item_context=item.context,
            minutes_ahead=trigger_minutes,
            conv_history=conv_history,
            user_id=user_id,
        )
        # prompt 已含 conv_history，不再额外传 history= 避免重复注入

        triggered.append(
            {
                "item_id": item.id,
                "title": item.title,
                "reminder_text": reminder_text,
                "minutes_until": trigger_minutes,
                "type": item.type,
            }
        )

        item.last_triggered = now.isoformat()
        await schedule_store.update_item(user_id, item)

    return triggered
