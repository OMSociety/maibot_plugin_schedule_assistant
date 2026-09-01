"""
Markdown 渲染与降级管线

两级策略（render 返回 (content, kind)）：
- native : 平台原生解析 md → 原文直发（qq_official / discord / telegram 等）
- plain  : strip 后纯文本（markdown_it → HTML → markdownify）

所有外部依赖（markdown_it / markdownify）均为函数内惰性导入，
模块被 import 时不产生任何副作用，默认路径（无 md 语法 / markdown_enabled=False）
完全不走本模块的逻辑。
"""

from __future__ import annotations

import re

# 平台原生解析 markdown 的默认集合
DEFAULT_NATIVE_PLATFORMS = {
    "qq_official",
    "qq_official_webhook",
    "discord",
    "slack",
    "kook",
    "telegram",
    "matrix",
    "lark",
    "dingtalk",
    "satori",
    "qqguild",
}

# 检测 markdown 语法的启发式正则
_MD_PATTERNS = (
    re.compile(r"\*\*[^*]+\*\*"),  # 加粗
    re.compile(r"(?<!\w)#{1,6}\s"),  # 标题
    re.compile(r"^\s*[-*+]\s+", re.MULTILINE),  # 无序列表
    re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE),  # 有序列表
    re.compile(r"\[[^\]]+\]\([^)]+\)"),  # 链接
    re.compile(r"^\s*>\s?", re.MULTILINE),  # 引用
    re.compile(r"`[^`]+`"),  # 行内代码
    re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE),  # 表格行
)


def _has_markdown_syntax(text: str) -> bool:
    """启发式检测文本是否含 markdown 语法"""
    if not text or not isinstance(text, str):
        return False
    return any(p.search(text) for p in _MD_PATTERNS)


def _strip_md_regex(text: str) -> str:
    """正则粗剥兜底（库不可用时的保底方案）"""
    t = text
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)  # 图片
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)  # 链接
    t = re.sub(r"`([^`]+)`", r"\1", t)  # 行内代码
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.MULTILINE)  # 标题
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.MULTILINE)  # 列表符号
    t = re.sub(r"^\s*>\s?", "", t, flags=re.MULTILINE)  # 引用
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)  # 加粗
    t = re.sub(r"^\s*\|.*\|\s*$", "", t, flags=re.MULTILINE)  # 表格行（丢弃）
    return _squeeze_blank_lines(t)


def _squeeze_blank_lines(text: str) -> str:
    """压缩连续空行，去掉行尾空白"""
    out: list[str] = []
    blank = False
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            if blank:
                continue
            blank = True
            out.append("")
        else:
            blank = False
            out.append(line)
    return "\n".join(out).strip()


def strip_markdown(text: str) -> str:
    """markdown → 纯文本（markdown_it 渲染 HTML → markdownify 转文本）

    库异常时自动回退到正则粗剥，保证永不抛错。
    """
    if not text:
        return ""
    try:
        import markdownify
        from markdown_it import MarkdownIt

        html = MarkdownIt().render(text)
        plain = markdownify.markdownify(
            html,
            heading_style="ATX",
            bullets="*",
            # 去掉会还原成 markdown 标记的标签，输出纯文本
            strip=[
                "img",
                "a",
                "strong",
                "em",
                "b",
                "i",
                "u",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            ],
        )
        result = _squeeze_blank_lines(plain)
        return result if result else _strip_md_regex(text)
    except Exception:
        return _strip_md_regex(text)


def render_qq_active(text: str) -> str:
    """QQ 官方平台降级排版：md → QQ 展示友好的纯文本

    当 qq_markdown_enabled=false 强制关闭 QQ 原生 md 时使用，
    将 md 排版为近似观感的纯文本：标题转【】、列表转 ·、表格转 键：值。
    """
    if not text:
        return ""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", s)
        if m:
            lines.append("【" + m.group(1).strip() + "】")
            continue
        m = re.match(r"^[-*+]\s+(.*)$", s)
        if m:
            lines.append("· " + m.group(1).strip())
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", s)
        if m:
            lines.append(s)
            continue
        # 表格行：| a | b | → 两列转「a：b」，多列转「a｜b｜c」；分隔行丢弃
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 3:
            cells = [c.strip() for c in s.split("|")[1:-1]]
            if cells and not any(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                if len(cells) == 2:
                    lines.append(f"{cells[0]}：{cells[1]}")
                else:
                    lines.append("｜".join(cells))
                continue
            continue
        # 内联语法清理：粗体 / 行内代码 / 链接
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        lines.append(line.strip())
    return _squeeze_blank_lines("\n".join(lines))


class MarkdownRenderer:
    """Markdown 渲染器（native / plain / qq_active 三级）"""

    def __init__(self, config: dict, platform_types: dict[str, str] | None = None):
        self.config = config or {}
        self.enabled = bool(self.config.get("markdown_enabled", False))
        # 实例ID → 平台类型名 映射（如 {"bot": "qq_official"}），
        # 由调用方从 platform_manager 动态构建，杜绝硬编码平台实例ID。
        self.platform_types = dict(platform_types or {})
        native_extra = self.config.get("markdown_native_platforms", []) or []
        self.native_platforms = set(DEFAULT_NATIVE_PLATFORMS)
        for pid in native_extra:
            if pid and isinstance(pid, str):
                self.native_platforms.add(pid.strip())
        # qq_markdown_enabled：None 跟随全局；False 强制 QQ 不走原生 md
        self.qq_md_enabled = self.config.get("qq_markdown_enabled")

    # ---------- 平台判定 ----------

    def _resolve_platform_type(self, platform_id: str) -> str:
        """实例ID → 平台类型名（meta().name）；未注册/无法解析时原样返回"""
        if not platform_id:
            return ""
        return self.platform_types.get(platform_id, platform_id)

    def _is_native(self, platform_id: str) -> bool:
        # qq_official 已在 render() 中单独处理（默认原生 / 配置关闭时降级），
        # 此处只需按平台类型名判断即可。
        return self._resolve_platform_type(platform_id) in self.native_platforms

    # ---------- 对外主入口 ----------

    def render(self, text: str, platform_id: str) -> tuple[str, str]:
        """按平台与配置渲染，返回 (content, kind)

        kind ∈ {"native", "plain"}
        """
        if not self.enabled:
            return text, "plain"  # md 总开关关闭 → 原文直发（旧行为）

        if not _has_markdown_syntax(text):
            return text, "plain"  # 无 md 语法 → 直发，行为零变化

        # QQ 官方：默认原生 md 直发（QQ 已全面开放 markdown 渲染，表格原生显示）；
        # 仅当显式配置 qq_markdown_enabled=false 时，转 QQ 排版纯文本兜底。
        # 被动回复场景由 AstrBot 事件链路处理，不经过本 renderer，不受影响。
        platform_type = self._resolve_platform_type(platform_id)
        if platform_type == "qq_official":
            if self.qq_md_enabled is False:
                return render_qq_active(text), "plain"
            return text, "native"

        # 原生平台 → 保留 md 原文（discord / telegram 等）
        if self._is_native(platform_id):
            return text, "native"

        # 非原生平台 → strip 纯文本（表格结构降级为普通文本行）
        return strip_markdown(text), "plain"
