"""配置界面 i18n 回归测试：en-US / ja-JP 全 locale 覆盖 + 翻译纪律。

固化 SDK schema 生成口径（maibot_sdk.config.generate_plugin_config_schema）：
- 字段级：json_schema_extra 里的 label/hint/placeholder 必须在 i18n 全 locale 覆盖；
- 节级：__ui_label__/docstring 渲染出的 title/description 必须在 i18n 全 locale 覆盖；
- 纪律：数字集合一致、标识符 token 不丢失、ja 连续汉字需带假名、ja==base 仅限白名单。

SDK 未安装（无 maibot_sdk 可导入）时整文件跳过，不影响无 SDK 环境的测试运行。
可用环境变量 MAIBOT_SDK_PATH 指向 SDK 包所在目录（maibot_sdk 的父目录）。
"""

import os
import re
import sys

import pytest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def _import_maibot_sdk():
    """按 优先已安装 → MAIBOT_SDK_PATH → 相邻 scratch 解压目录 找 maibot_sdk。"""
    try:
        import maibot_sdk  # noqa: F401

        return True
    except ImportError:
        pass
    candidates = []
    env_path = os.environ.get("MAIBOT_SDK_PATH")
    if env_path:
        candidates.append(env_path)
    candidates.append(
        os.path.join(os.path.dirname(_PLUGIN_DIR), "_tmp_i18n_schedule", "sdk")
    )
    for cand in candidates:
        if cand and os.path.isdir(os.path.join(cand, "maibot_sdk")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            try:
                import maibot_sdk  # noqa: F401

                return True
            except ImportError:
                continue
    return False


if not _import_maibot_sdk():
    pytest.skip("maibot_sdk not available", allow_module_level=True)

from maibot_sdk.config import (  # noqa: E402
    generate_plugin_config_schema,
    is_plugin_config_class,
)

from schedule_assistant.plugin import ScheduleAssistantConfig  # noqa: E402

LOCALES = ("en-US", "ja-JP")

# 豁免：纯技术串（时间格式/格式示例）与纯产品名，ja==base 检查中直接豁免
PURE_TECH = {
    "HH:MM",
    "09:00",
    "qq:123456",
    "Apple ID",
    "{username} {date} {weekday} {weather_current} {agenda}…",
    "{item_title} {time_label} {ahead_label}…",
}
# 豁免：中日同形词且日语本身合法（最小=さいしょう）
WHITELIST_EQ = {"最小 2"}

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def _is_identifier_token(tok: str) -> bool:
    if "_" in tok:
        return True  # snake_case
    if tok.isupper() and len(tok) >= 2:
        return True  # UPPER
    return bool(re.search(r"[a-z][A-Z]", tok))  # camelCase


def _walk(config_class, schema_node, prefix=""):
    """遍历配置模型：yield (path, extra, desc, fschema, is_section)。"""
    for fname, finfo in config_class.model_fields.items():
        extra = (
            finfo.json_schema_extra if isinstance(finfo.json_schema_extra, dict) else {}
        )
        desc = finfo.description or ""
        path = f"{prefix}{fname}"
        if is_plugin_config_class(finfo.annotation):
            yield (
                f"{path}.__section__",
                None,
                (finfo.annotation.__doc__ or "").strip(),
                schema_node["sections"][fname],
                True,
            )
            yield from _walk(
                finfo.annotation, schema_node["sections"][fname], prefix=f"{path}."
            )
        else:
            yield (path, extra, desc, schema_node["fields"][fname], False)


def _iter_schema():
    schema = generate_plugin_config_schema(ScheduleAssistantConfig)
    yield from _walk(ScheduleAssistantConfig, schema)


def _iter_translations():
    """yield (path, key, base_text, locale, translated)。"""
    for path, extra, desc, fschema, is_section in _iter_schema():
        i18n = fschema.get("i18n") or {}
        if is_section:
            needs = []
            if fschema.get("title"):
                needs.append(("title", fschema["title"]))
            if fschema.get("description"):
                needs.append(("description", fschema["description"]))
        else:
            needs = []
            if "label" in extra:
                needs.append(("label", extra.get("label", fschema.get("label"))))
            if extra.get("hint") or desc:
                needs.append(("hint", extra.get("hint") or desc))
            if "placeholder" in extra:
                needs.append(("placeholder", extra.get("placeholder")))
        for loc in LOCALES:
            loc_entries = i18n.get(loc) or {}
            for key, base in needs:
                yield (path, key, base, loc, loc_entries.get(key))


class TestLocaleCoverage:
    """全字段 / 全节的 en-US 与 ja-JP 覆盖：缺失即回归（WebUI 会回退显示中文）。"""

    def test_all_fields_and_sections_fully_covered(self):
        missing = []
        n_fields = n_sections = 0
        for path, extra, desc, fschema, is_section in _iter_schema():
            i18n = fschema.get("i18n") or {}
            if is_section:
                n_sections += 1
                if fschema.get("title"):
                    required = ["title"]
                else:
                    required = []
                if fschema.get("description"):
                    required.append("description")
            else:
                n_fields += 1
                required = []
                if "label" in extra:
                    required.append("label")
                if extra.get("hint") or desc:
                    required.append("hint")
                if "placeholder" in extra:
                    required.append("placeholder")
            for loc in LOCALES:
                loc_entries = i18n.get(loc) or {}
                for key in required:
                    if not loc_entries.get(key):
                        missing.append(f"{path}: i18n.{loc}.{key}")
        assert n_fields > 0 and n_sections > 0
        assert missing == [], f"i18n 覆盖缺口: {missing}"

    def test_translation_values_are_nonempty_strings(self):
        for path, key, _base, loc, text in _iter_translations():
            assert isinstance(text, str) and text.strip(), (path, key, loc)


class TestTranslationDiscipline:
    """纪律检测：数字一致 / 标识符保留 / ja 假名 / ja==base 白名单。"""

    def test_digit_multiset_matches_base(self):
        issues = []
        for path, key, base, loc, text in _iter_translations():
            if re.search(r"\d", base) and sorted(re.findall(r"\d", base)) != sorted(
                re.findall(r"\d", text)
            ):
                issues.append(f"{path}.{key} [{loc}]: {base!r} -> {text!r}")
        assert issues == [], f"数字集合与 base 不一致: {issues}"

    def test_identifier_tokens_preserved(self):
        issues = []
        for path, key, base, loc, text in _iter_translations():
            for tok in {t for t in _TOKEN_RE.findall(base) if _is_identifier_token(t)}:
                if tok not in text:
                    issues.append(
                        f"{path}.{key} [{loc}]: 丢失 token {tok!r} in {text!r}"
                    )
        assert issues == [], f"标识符 token 丢失: {issues}"

    def test_ja_long_kanji_runs_have_kana(self):
        issues = []
        for path, key, _base, loc, text in _iter_translations():
            if loc != "ja-JP":
                continue
            runs = re.findall(r"[\u4e00-\u9fff]{5,}", text)
            has_kana = re.search(r"[\u3040-\u30ff]", text)
            if runs and not has_kana:
                issues.append(f"{path}.{key}: 连续汉字 {runs} 且无假名: {text!r}")
        assert issues == [], f"疑似非日语译文（需人工确认）: {issues}"

    def test_ja_equal_to_base_only_for_whitelisted(self):
        issues = []
        for path, key, base, loc, text in _iter_translations():
            if (
                loc == "ja-JP"
                and text == base
                and base not in PURE_TECH
                and base not in WHITELIST_EQ
            ):
                issues.append(f"{path}.{key}: ja 与 base 相等: {text!r}")
        assert issues == [], f"ja 未翻译（需人工确认）: {issues}"
