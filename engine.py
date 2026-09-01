"""
通用定时消息引擎（MaiBot 插件版）

将「定时触发」与「内容生成/发送」解耦：
- 业务（早安播报 / 习惯提醒 / 日程提醒等）通过 register_job 注册为 job
- content_provider 只负责生成内容（str | None），发送统一由引擎走 MessagingService
- 特殊任务（喝水重排、Apple 同步等）可用 register_raw_job 注册自定义 handler

触发方式支持：
- CronTrigger 实例
- "HH:MM" 字符串（每日定时）
- "interval:N" 字符串（每 N 分钟）
- 其他 apscheduler 支持的 trigger（如 "date"、"interval"）原样透传
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from .constants import LOG_PREFIX

logger = logging.getLogger(__name__)

# content_provider: async (user_id: str, shared: Any) -> str | None
ContentProvider = Callable[[str, Any], Awaitable[str | None]]
# prepare: async () -> Any（可选，一次性准备共享数据，如天气/日历）
PrepareHook = Callable[[], Awaitable[Any]]
# raw handler: async (*args) -> None（完全自定义，如喝水重排）
RawHandler = Callable[..., Awaitable[None]]


class TimedMessageEngine:
    """
    通用定时消息引擎

    负责 job 注册与统一执行：解析目标用户 → 生成内容 → 发送。
    内容生成与发送完全解耦，业务只关心「生成什么」，引擎只关心「何时发给谁」。
    """

    def __init__(self, context, config: dict, messaging, scheduler):
        """
        Args:
            context: AstrBot 上下文
            config: 插件配置
            messaging: MessagingService 实例（负责路由/发送/目标用户解析）
            scheduler: AsyncIOScheduler 实例
        """
        self.context = context
        self.config = config
        self.messaging = messaging
        self.scheduler = scheduler
        self._registered_jobs: set[str] = set()

    # ============ 触发方式解析 ============

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int] | None:
        """解析 HH:MM 字符串为 (hour, minute)"""
        try:
            hour, minute = map(int, str(value).split(":", 1))
            return hour, minute
        except (ValueError, TypeError, AttributeError):
            return None

    def _normalize_trigger(self, trigger: Any) -> Any:
        """将触发方式规范化为 apscheduler 可用的 trigger

        支持：
        - CronTrigger 实例：原样返回
        - "HH:MM" 字符串：转为每日 CronTrigger
        - "interval:N" 字符串：转为 ("interval", minutes=N)
        - 其他（"date"/"interval"/CronTrigger 等）：原样透传
        """
        if isinstance(trigger, CronTrigger):
            return trigger
        if isinstance(trigger, str):
            text = trigger.strip()
            if text.startswith("interval:"):
                try:
                    minutes = max(1, int(text.split(":", 1)[1].strip()))
                except (ValueError, TypeError, IndexError):
                    logger.warning(
                        f"{LOG_PREFIX} interval 配置非法: {trigger!r}，使用 5 分钟"
                    )
                    minutes = 5
                return ("interval", {"minutes": minutes})
            parsed = self._parse_hhmm(text)
            if parsed is not None:
                return CronTrigger(hour=parsed[0], minute=parsed[1])
            # 仅白名单透传 APScheduler 内置触发器名，其他字符串一律拒绝
            if text in ("date", "interval", "cron"):
                return text
            logger.warning(f"{LOG_PREFIX} 不支持的触发方式: {trigger!r}")
            return None
        return trigger

    # ============ 注册 ============

    def register_job(
        self,
        name: str,
        trigger: Any,
        content_provider: ContentProvider,
        *,
        prepare: PrepareHook | None = None,
        include_known_users: bool = False,
        **job_opts,
    ) -> bool:
        """注册一个业务定时任务（内容生成与发送解耦）

        Args:
            name: job 唯一 ID
            trigger: 触发方式（CronTrigger / "HH:MM" / "interval:N" / 其他 apscheduler trigger）
            content_provider: async (user_id, shared) -> str | None；
                返回 None 表示该用户本轮不发送
            prepare: 可选 async () -> shared，执行前一次性准备共享数据（如天气），
                结果作为第二个参数传给 content_provider；None 时传入 None
            include_known_users: 目标是否包含存储中的全部已知用户
            **job_opts: 透传给 apscheduler add_job 的选项（max_instances/coalesce 等）

        Returns:
            bool: 是否注册成功（触发方式非法时返回 False）
        """
        normalized = self._normalize_trigger(trigger)
        if normalized is None:
            logger.warning(
                f"{LOG_PREFIX} 任务 {name} 触发方式非法，跳过注册: {trigger!r}"
            )
            return False

        async def _job():
            try:
                shared = await prepare() if prepare else None
                targets = await self.messaging.resolve_target_users(include_known_users)
                if not targets:
                    logger.debug(f"{LOG_PREFIX} 任务 {name} 无目标用户，跳过")
                    return
                for user_id in targets:
                    try:
                        content = await content_provider(str(user_id), shared)
                    except Exception as provider_err:
                        logger.warning(
                            f"{LOG_PREFIX} 任务 {name} 内容生成失败 user={user_id} err={provider_err}"
                        )
                        continue
                    if not content:
                        continue
                    try:
                        await self.messaging.send_to_user(str(user_id), content)
                    except Exception as send_err:
                        logger.warning(
                            f"{LOG_PREFIX} 任务 {name} 发送失败 user={user_id} err={send_err}"
                        )
            except Exception as e:
                logger.error(f"{LOG_PREFIX} 任务 {name} 执行异常: {e}")

        self._add_job(name, _job, normalized, **job_opts)
        return True

    def register_raw_job(
        self, name: str, trigger: Any, handler: RawHandler, **job_opts
    ) -> bool:
        """注册一个完全自定义的定时任务（handler 自行处理目标解析/发送/重排等）

        Args:
            name: job 唯一 ID
            trigger: 触发方式（同上）
            handler: async 回调，直接作为 apscheduler job 执行
            **job_opts: 透传给 apscheduler add_job 的选项

        Returns:
            bool: 是否注册成功
        """
        normalized = self._normalize_trigger(trigger)
        if normalized is None:
            logger.warning(
                f"{LOG_PREFIX} 任务 {name} 触发方式非法，跳过注册: {trigger!r}"
            )
            return False
        self._add_job(name, handler, normalized, **job_opts)
        return True

    def _add_job(self, name: str, func, trigger, **job_opts):
        options = {"id": name, "replace_existing": True}
        options.update(job_opts)
        self.scheduler.add_job(func, trigger, **options)
        self._registered_jobs.add(name)
        logger.info(f"{LOG_PREFIX} 定时任务已注册: {name} trigger={trigger}")

    # ============ 任务管理 ============

    def remove_job(self, name: str) -> None:
        """移除一个任务（不存在时静默忽略）"""
        try:
            self.scheduler.remove_job(name)
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 移除任务失败（可能不存在）: {name} err={e}")
        self._registered_jobs.discard(name)

    def has_job(self, name: str) -> bool:
        return name in self._registered_jobs

    def registered_jobs(self) -> list[str]:
        return sorted(self._registered_jobs)

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        try:
            for name in list(self._registered_jobs):
                self.remove_job(name)
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 关闭调度器时出错: {e}")
