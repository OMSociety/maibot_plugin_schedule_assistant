"""定时调度薄层：只负责把 handler 挂到 APScheduler 上。

触发方式支持：
- CronTrigger 实例
- "HH:MM"（每日定时）
- ("interval", {"minutes": N}) 或 "interval:N"
- 其他 APScheduler trigger 原样透传
"""

from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from .constants import LOG_PREFIX

Handler = Callable[[], Awaitable[None]]


class TimedMessageEngine:
    def __init__(self, scheduler, logger) -> None:
        self.scheduler = scheduler
        self.logger = logger
        self._registered: set[str] = set()

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int] | None:
        try:
            hour, minute = map(int, str(value).split(":", 1))
        except (ValueError, TypeError, AttributeError):
            return None
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None

    def _normalize_trigger(self, trigger: Any) -> Any:
        if isinstance(trigger, CronTrigger):
            return trigger
        if isinstance(trigger, str):
            text = trigger.strip()
            if text.startswith("interval:"):
                try:
                    minutes = max(1, int(text.split(":", 1)[1].strip()))
                except (ValueError, TypeError, IndexError):
                    self.logger.warning(
                        f"{LOG_PREFIX} interval 配置非法: {trigger!r}，按 5 分钟"
                    )
                    minutes = 5
                return ("interval", {"minutes": minutes})
            parsed = self._parse_hhmm(text)
            if parsed is not None:
                return CronTrigger(hour=parsed[0], minute=parsed[1])
            self.logger.warning(f"{LOG_PREFIX} 不支持的触发方式: {trigger!r}")
            return None
        return trigger

    def register(self, name: str, trigger: Any, handler: Handler, **job_opts) -> bool:
        normalized = self._normalize_trigger(trigger)
        if normalized is None:
            return False
        options = {"id": name, "replace_existing": True}
        options.update(job_opts)
        try:
            if (
                isinstance(normalized, tuple)
                and len(normalized) == 2
                and isinstance(normalized[1], dict)
            ):
                trigger_type, trigger_args = normalized
                options.update(trigger_args)
                self.scheduler.add_job(handler, trigger_type, **options)
            else:
                self.scheduler.add_job(handler, normalized, **options)
        except Exception as exc:
            self.logger.error(f"{LOG_PREFIX} 任务注册失败 {name}: {exc}")
            return False
        self._registered.add(name)
        self.logger.info(f"{LOG_PREFIX} 定时任务已注册: {name} trigger={trigger}")
        return True

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        try:
            for name in list(self._registered):
                try:
                    self.scheduler.remove_job(name)
                except Exception:
                    pass
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception as exc:
            self.logger.warning(f"{LOG_PREFIX} 关闭调度器时出错: {exc}")
