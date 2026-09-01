"""
Markdown 渲染管线测试

测试 markdown.py 的语法检测、strip 降级、QQ 排版、native/plain 分支。
markdown.py 不依赖 AstrBot 环境，可独立测试。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from markdown import (
    MarkdownRenderer,
    _has_markdown_syntax,
    _strip_md_regex,
    render_qq_active,
    strip_markdown,
)

SAMPLE_MD = """早安喵~

#### 📅 早安播报
2026-08-16 周日 雨天喔

**🌤️ 天气** 小雨 22°C，降水概率0.77%

#### 🫕 温馨提示
下雨天记得带伞~"""


class TestHasMarkdownSyntax:
    """md 语法启发式检测"""

    def test_bold(self):
        assert _has_markdown_syntax("**加粗** 文本")

    def test_heading(self):
        assert _has_markdown_syntax("#### 标题")

    def test_list(self):
        assert _has_markdown_syntax("- 列表项")

    def test_link(self):
        assert _has_markdown_syntax("[链接](https://x.com)")

    def test_plain_text_false(self):
        assert not _has_markdown_syntax("普通文本没有语法")

    def test_empty_false(self):
        assert not _has_markdown_syntax("")
        assert not _has_markdown_syntax(None)


class TestStripMarkdown:
    """md → 纯文本 strip"""

    def test_strip_bold_and_heading(self):
        text = "**加粗** 文本\n\n#### 标题"
        result = strip_markdown(text)
        assert "**" not in result
        assert "####" not in result
        assert "加粗" in result
        assert "标题" in result

    def test_strip_sample(self):
        result = strip_markdown(SAMPLE_MD)
        assert "**" not in result
        assert "####" not in result
        assert "早安喵" in result
        assert "温馨提示" in result

    def test_regex_fallback(self):
        """库不可用时正则粗剥兜底（直接测正则函数）"""
        result = _strip_md_regex("**粗** [链接](https://x.com) `代码`")
        assert "**" not in result
        assert "链接" in result
        assert "代码" in result


class TestRenderQqActive:
    """QQ 官方平台降级排版"""

    def test_heading_to_brackets(self):
        assert render_qq_active("#### 今日日程") == "【今日日程】"

    def test_table_two_columns(self):
        text = "| 时间 | 事项 |\n|------|------|\n| 09:00 | 开会 |"
        result = render_qq_active(text)
        assert "时间：事项" in result
        assert "09:00：开会" in result

    def test_bold_stripped(self):
        assert render_qq_active("**天气** 晴") == "天气 晴"


class TestMarkdownRenderer:
    """MarkdownRenderer.render 平台分支"""

    def _renderer(self, enabled=True, native_platforms=None, qq_md=None):
        return MarkdownRenderer(
            {
                "markdown_enabled": enabled,
                "markdown_native_platforms": native_platforms or [],
                "qq_markdown_enabled": qq_md,
            },
            platform_types={"webchat": "webchat", "bot": "qq_official"},
        )

    def test_disabled_returns_raw(self):
        """markdown_enabled=False → 原文直发（旧行为）"""
        r = self._renderer(enabled=False)
        content, kind = r.render(SAMPLE_MD, "webchat")
        assert kind == "plain"
        assert "####" in content  # 原文直发，未处理

    def test_plain_text_no_change(self):
        """无 md 语法 → 原文直发，行为零变化"""
        r = self._renderer()
        content, kind = r.render("普通文本", "webchat")
        assert content == "普通文本"
        assert kind == "plain"

    def test_webchat_strips_md(self):
        """webchat 非原生平台 → strip 纯文本"""
        r = self._renderer()
        content, kind = r.render(SAMPLE_MD, "webchat")
        assert kind == "plain"
        assert "####" not in content
        assert "**" not in content

    def test_qq_official_native(self):
        """QQ 官方默认 native（直发原生 md）"""
        r = self._renderer()
        content, kind = r.render(SAMPLE_MD, "bot")
        assert kind == "native"
        assert "####" in content

    def test_qq_official_disabled_fallback(self):
        """qq_markdown_enabled=False → QQ 排版纯文本"""
        r = self._renderer(qq_md=False)
        content, kind = r.render(SAMPLE_MD, "bot")
        assert kind == "plain"
        assert "【" in content  # 标题转【】

    def test_extra_native_platform(self):
        """markdown_native_platforms 追加原生平台"""
        r = self._renderer(native_platforms=["webchat"])
        _, kind = r.render(SAMPLE_MD, "webchat")
        assert kind == "native"
