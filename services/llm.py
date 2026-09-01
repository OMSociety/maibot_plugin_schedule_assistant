"""LLM 服务（MaiBot 插件版）— 封装 ctx.llm.generate，支持人格注入与熔断"""

import logging
import time

from ..constants import LOG_PREFIX

logger = logging.getLogger(__name__)

_LLM_CIRCUIT_BREAKER_TTL = 300  # 5分钟


class LLMService:
    """LLM 服务，封装 MaiBot 的 ctx.llm.generate 接口

    人格处理：MaiBot 无 AstrBot 式多人格，只有全局单人格（bot_config.toml
    [personality] 三字段：personality / behavior_style / reply_style）。
    通过 ctx.config.get() 读取（host 端读 global_config），拼进 system prompt，
    使播报语气与 bot 日常一致。读取失败降级为纯提示词。
    """

    def __init__(self, ctx, config: dict | None = None):
        """
        Args:
            ctx: MaiBot 插件实例（提供 self.ctx.llm / self.ctx.config / self.ctx.logger）
            config: 插件配置
        """
        self._ctx = ctx
        self.config = config or {}
        self._fallback_template = ""
        # 实例级断路器：记录最近一次失败时间，熔断期内直接返回 fallback
        self._llm_failure_time = 0.0
        self._persona_cache: tuple[str, float] | None = None  # (text, ts)

    def set_fallback_template(self, template: str):
        """设置 LLM 失败时的 fallback 模板文案"""
        self._fallback_template = template

    async def _get_persona_prompt(self) -> str:
        """读取 MaiBot 全局人格（[personality] 三字段），带 60s 缓存

        读取失败返回空字符串（降级为纯提示词）。
        """
        now = time.monotonic()
        if self._persona_cache and now - self._persona_cache[1] < 60:
            return self._persona_cache[0]
        try:
            cfg = self._ctx.ctx.config
            personality = await cfg.get("personality.personality", "")
            behavior = await cfg.get("personality.behavior_style", "")
            reply_style = await cfg.get("personality.reply_style", "")
            parts = []
            if personality:
                parts.append(f"【人格】{personality}")
            if behavior:
                parts.append(f"【行为风格】{behavior}")
            if reply_style:
                parts.append(f"【表达风格】{reply_style}")
            text = "\n".join(parts)
            self._persona_cache = (text, now)
            return text
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取全局人格失败: {e}")
            return ""

    async def generate(
        self,
        prompt: str,
        use_persona: bool = True,
        history: str = "",
        umo: str | None = None,
        extra_system: str = "",
    ) -> str:
        """生成 LLM 回复

        Args:
            prompt: 用户输入的 prompt
            use_persona: 是否拼入 MaiBot 全局人格
            history: 近期对话历史，拼到 system 末尾
            umo: 兼容保留（MaiBot 版不使用）
            extra_system: 追加到 system_prompt 末尾的补充指令（如播报格式特例）

        Returns:
            LLM 生成的文本
        """
        system_prompt = await self._get_persona_prompt() if use_persona else ""
        if history:
            history_section = "\n\n【近期对话】\n" + history
            system_prompt = (system_prompt or "") + history_section
        if extra_system:
            system_prompt = (system_prompt or "") + "\n\n" + extra_system

        # 熔断检查
        if self._llm_failure_time and (
            time.time() - self._llm_failure_time < _LLM_CIRCUIT_BREAKER_TTL
        ):
            logger.warning(
                f"{LOG_PREFIX} LLM 处于熔断期（上次失败于 "
                f"{time.time() - self._llm_failure_time:.0f} 秒前），跳过调用"
            )
            return self._fallback_template or ""

        try:
            if system_prompt:
                result = await self._ctx.ctx.llm.generate(
                    prompt=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ]
                )
            else:
                result = await self._ctx.ctx.llm.generate(prompt=prompt)
            self._llm_failure_time = 0.0  # 成功，重置断路器
            text = (result or {}).get("response") or ""
            return text.strip()
        except Exception as e:
            logger.error(f"{LOG_PREFIX} LLM 生成失败: {e}")
            self._llm_failure_time = time.time()
            if self._fallback_template:
                logger.warning(f"{LOG_PREFIX} LLM 失败，使用 fallback 模板")
                return self._fallback_template
            return ""
