"""LLM 翻译功能测试（离线运行，不依赖真实 AstrBot / 网络）。

运行方式（在装有 pytest + aiohttp 的环境里）：
    python -m pytest tests/test_translation.py -v

main.py 依赖 astrbot.api，这里用最小桩模块替代，只覆盖翻译相关逻辑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _install_astrbot_stubs() -> None:
    """注入最小 astrbot 桩，使 main.py 可在无 AstrBot 环境下导入。"""
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot.test")

    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

        async def get_kv_data(self, key, default=None):
            return default

        async def put_kv_data(self, key, value):
            pass

        async def delete_kv_data(self, key):
            pass

    class Context:
        def register_web_api(self, *args, **kwargs):
            pass

    star.Star = Star
    star.Context = Context
    # 不提供 register：main.py 会回退到自带的 identity 装饰器

    event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class _Filter:
        def command(self, *args, **kwargs):
            def deco(fn):
                return fn

            return deco

    event.AstrMessageEvent = AstrMessageEvent
    event.filter = _Filter()

    api.star = star
    api.event = event
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.event"] = event


_install_astrbot_stubs()

from main import MetacriticGameReader  # noqa: E402
from metacritic_client import (  # noqa: E402
    GameInfo,
    PlatformScore,
    Review,
    first_paragraph,
)
from translator import (  # noqa: E402
    llm_translate,
    parse_translations,
    resolve_provider_id,
)


def run(coro):
    return asyncio.run(coro)


class FakeContext:
    """模拟 AstrBot Context 的 LLM 相关接口。"""

    def __init__(self, reply: str = "", provider: str | None = "session-provider"):
        self.reply = reply
        self.provider = provider
        self.calls: list[dict] = []

    async def get_current_chat_provider_id(self, umo=None):
        if self.provider is None:
            raise RuntimeError("no provider")
        return self.provider

    async def llm_generate(self, *, chat_provider_id, prompt, system_prompt=None, **kw):
        self.calls.append(
            {"provider": chat_provider_id, "prompt": prompt, "system": system_prompt}
        )
        return types.SimpleNamespace(completion_text=self.reply)


def make_game() -> GameInfo:
    r1 = Review(
        score=95,
        publication="IGN",
        quote="A masterpiece of open-world design…",
        quote_first_para="A masterpiece of open-world design that redefines the genre.",
    )
    r2 = Review(
        score=90,
        publication="GameSpot",
        quote="Stunning visuals and tight combat…",
        quote_first_para="Stunning visuals and tight combat make every fight a joy.",
    )
    # 与 r1 第一段完全相同，用于验证去重
    r3 = Review(
        score=92,
        publication="PC Gamer",
        quote="A masterpiece of open-world design…",
        quote_first_para="A masterpiece of open-world design that redefines the genre.",
    )
    return GameInfo(
        title="Elden Ring",
        slug="elden-ring",
        query="艾尔登法环",
        description="An epic action RPG set in a vast dark fantasy world.",
        description_full=(
            "An epic action RPG set in a vast dark fantasy world. "
            "Explore the Lands Between and become the Elden Lord."
        ),
        platforms=[
            PlatformScore(name="PC", slug="pc", critic_score=94, reviews=[r1, r2]),
            PlatformScore(
                name="PlayStation 5", slug="playstation-5", critic_score=96, reviews=[r3]
            ),
        ],
    )


# ------------------------------------------------------------------ #
# first_paragraph
# ------------------------------------------------------------------ #
def test_first_paragraph_strips_html_and_takes_first_block():
    text = "<p>First paragraph <b>here</b>.</p>\n\nSecond paragraph should be ignored."
    # 标签按 _clean_text 的既有约定替换为空格
    assert first_paragraph(text) == "First paragraph here ."


def test_first_paragraph_soft_wrap_is_not_a_boundary():
    text = "Line one continues\non line two.\n\nSecond paragraph."
    assert first_paragraph(text) == "Line one continues on line two."


def test_first_paragraph_empty():
    assert first_paragraph("") == ""
    assert first_paragraph("<br>\n\n  ") == ""


def test_first_paragraph_unescapes_entities():
    assert first_paragraph("Tom &amp; Jerry&#39;s adventure") == "Tom & Jerry's adventure"


# ------------------------------------------------------------------ #
# parse_translations
# ------------------------------------------------------------------ #
def test_parse_valid_json_array():
    assert parse_translations('["你好", "世界"]', 2) == ["你好", "世界"]


def test_parse_tolerates_code_fence():
    assert parse_translations('```json\n["你好"]\n```', 1) == ["你好"]


def test_parse_rejects_wrong_length():
    assert parse_translations('["只有一条"]', 2) is None


def test_parse_rejects_non_array_or_empty_items():
    assert parse_translations('{"a": 1}', 1) is None
    assert parse_translations('["", "ok"]', 2) is None
    assert parse_translations("not json", 1) is None
    assert parse_translations("", 1) is None


# ------------------------------------------------------------------ #
# llm_translate
# ------------------------------------------------------------------ #
def test_translate_dedupes_and_maps_back():
    ctx = FakeContext(reply='["译文一", "译文二"]')
    mapping = run(llm_translate(ctx, ["text one", "text two", "text one"]))
    assert mapping == {"text one": "译文一", "text two": "译文二"}
    # 去重后只发送两条，且只调用一次 LLM
    assert len(ctx.calls) == 1
    assert json.loads(ctx.calls[0]["prompt"]) == ["text one", "text two"]


def test_translate_uses_configured_provider():
    ctx = FakeContext(reply='["译文"]')
    run(llm_translate(ctx, ["hello"], provider_id="my-provider"))
    assert ctx.calls[0]["provider"] == "my-provider"


def test_translate_falls_back_to_session_provider():
    ctx = FakeContext(reply='["译文"]')
    run(llm_translate(ctx, ["hello"], provider_id="", umo="test-umo"))
    assert ctx.calls[0]["provider"] == "session-provider"


def test_translate_no_provider_returns_none():
    ctx = FakeContext(provider=None)
    assert run(llm_translate(ctx, ["hello"])) is None
    assert ctx.calls == []


def test_translate_llm_error_returns_none():
    class BrokenContext(FakeContext):
        async def llm_generate(self, **kw):
            raise RuntimeError("boom")

    assert run(llm_translate(BrokenContext(), ["hello"])) is None


def test_translate_bad_output_returns_none():
    ctx = FakeContext(reply="抱歉，我不会翻译")
    assert run(llm_translate(ctx, ["hello"])) is None


def test_translate_empty_texts_returns_empty_mapping():
    ctx = FakeContext()
    assert run(llm_translate(ctx, ["", "  "])) == {}
    assert ctx.calls == []


# ------------------------------------------------------------------ #
# _translate_game（main.py 集成逻辑）
# ------------------------------------------------------------------ #
def make_plugin(cfg: dict, ctx: FakeContext) -> MetacriticGameReader:
    return MetacriticGameReader(context=ctx, config=cfg)


def test_translate_game_reviews_and_description():
    game = make_game()
    # 去重后的顺序：r1 第一段、r2 第一段、简介全文
    reply = json.dumps(
        ["开放世界设计的杰作，重新定义了这一类型。", "画面惊艳，战斗手感出色。", "史诗级动作角色扮演游戏。"],
        ensure_ascii=False,
    )
    ctx = FakeContext(reply=reply)
    plugin = make_plugin(
        {"translate_reviews": True, "translate_description": True}, ctx
    )
    run(plugin._translate_game(game, umo="u"))

    assert len(ctx.calls) == 1  # 3 篇评测 + 简介，合并成一次调用
    assert game.platforms[0].reviews[0].quote == "开放世界设计的杰作，重新定义了这一类型。"
    assert game.platforms[0].reviews[1].quote == "画面惊艳，战斗手感出色。"
    # 相同第一段复用同一译文
    assert game.platforms[1].reviews[0].quote == "开放世界设计的杰作，重新定义了这一类型。"
    assert game.description == "史诗级动作角色扮演游戏。"
    # 原始第一段保留不变（供后续复用/调试）
    assert game.platforms[0].reviews[0].quote_first_para.startswith("A masterpiece")


def test_translate_game_truncates_long_translation():
    game = make_game()
    long_zh = "很长的译文" * 60  # 300 字，超过默认 100 字
    reply = json.dumps([long_zh, "短译文", "简介译文"], ensure_ascii=False)
    ctx = FakeContext(reply=reply)
    plugin = make_plugin(
        {
            "translate_reviews": True,
            "translate_description": True,
            "review_quote_chars": 100,
        },
        ctx,
    )
    run(plugin._translate_game(game, umo="u"))
    quote = game.platforms[0].reviews[0].quote
    assert len(quote) == 101 and quote.endswith("…")


def test_translate_game_disabled_makes_no_llm_call():
    game = make_game()
    ctx = FakeContext(reply="[]")
    plugin = make_plugin({}, ctx)
    run(plugin._translate_game(game, umo="u"))
    assert ctx.calls == []
    assert game.platforms[0].reviews[0].quote.startswith("A masterpiece")


def test_translate_game_reviews_only_keeps_english_description():
    game = make_game()
    ctx = FakeContext(reply='["译文一", "译文二"]')
    plugin = make_plugin({"translate_reviews": True}, ctx)
    run(plugin._translate_game(game, umo="u"))
    sent = json.loads(ctx.calls[0]["prompt"])
    assert game.description_full not in sent  # 简介未开启翻译，不发送给 LLM
    assert game.description.startswith("An epic action RPG")


def test_translate_game_failure_keeps_original():
    game = make_game()
    ctx = FakeContext(reply="模型罢工了")
    plugin = make_plugin(
        {"translate_reviews": True, "translate_description": True}, ctx
    )
    run(plugin._translate_game(game, umo="u"))
    assert game.platforms[0].reviews[0].quote.startswith("A masterpiece")
    assert game.description.startswith("An epic action RPG")


def test_translate_game_uses_configured_provider():
    game = make_game()
    ctx = FakeContext(reply='["译文一", "译文二", "简介译文"]')
    plugin = make_plugin(
        {
            "translate_reviews": True,
            "translate_description": True,
            "translate_provider": "chosen-provider",
        },
        ctx,
    )
    run(plugin._translate_game(game, umo="u"))
    assert ctx.calls[0]["provider"] == "chosen-provider"


# ------------------------------------------------------------------ #
# resolve_provider_id（识别 / 翻译共用）
# ------------------------------------------------------------------ #
def test_resolve_provider_explicit_wins():
    ctx = FakeContext()
    assert run(resolve_provider_id(ctx, "chosen", umo="u")) == "chosen"


def test_resolve_provider_falls_back_to_session():
    ctx = FakeContext()
    assert run(resolve_provider_id(ctx, "", umo="u")) == "session-provider"
    assert run(resolve_provider_id(ctx, None)) == "session-provider"


def test_resolve_provider_none_when_unconfigured():
    ctx = FakeContext(provider=None)
    assert run(resolve_provider_id(ctx, "")) is None


# ------------------------------------------------------------------ #
# _llm_normalize（游戏名识别的 Provider 选择）
# ------------------------------------------------------------------ #
def test_normalize_uses_configured_provider():
    ctx = FakeContext(reply="Elden Ring")
    plugin = make_plugin({"recognize_provider": "rec-provider"}, ctx)
    assert run(plugin._llm_normalize("老头环", umo="u")) == "Elden Ring"
    assert ctx.calls[0]["provider"] == "rec-provider"


def test_normalize_falls_back_to_session_provider():
    ctx = FakeContext(reply="Elden Ring")
    plugin = make_plugin({}, ctx)  # recognize_provider 留空
    assert run(plugin._llm_normalize("老头环", umo="u")) == "Elden Ring"
    assert ctx.calls[0]["provider"] == "session-provider"


def test_normalize_disabled_makes_no_llm_call():
    ctx = FakeContext(reply="Elden Ring")
    plugin = make_plugin({"use_llm": False, "recognize_provider": "rec"}, ctx)
    assert run(plugin._llm_normalize("老头环")) is None
    assert ctx.calls == []


def test_normalize_no_provider_degrades_silently():
    ctx = FakeContext(provider=None)
    plugin = make_plugin({}, ctx)
    assert run(plugin._llm_normalize("老头环")) is None
    assert ctx.calls == []


# ------------------------------------------------------------------ #
# 配置 Schema
# ------------------------------------------------------------------ #
def test_conf_schema_has_translation_options():
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["translate_reviews"]["type"] == "bool"
    assert schema["translate_reviews"]["default"] is False
    assert schema["translate_description"]["type"] == "bool"
    assert schema["translate_description"]["default"] is False
    provider = schema["translate_provider"]
    assert provider["type"] == "string"
    assert provider["default"] == ""
    # WebUI 依据该标记渲染「选择 AstrBot 已配置提供商」下拉框
    assert provider["_special"] == "select_provider"


def test_conf_schema_has_recognize_provider():
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    provider = schema["recognize_provider"]
    assert provider["type"] == "string"
    assert provider["default"] == ""
    assert provider["_special"] == "select_provider"