"""AstrBot 插件：Metacritic 游戏评分查询。

用法：
    /评分 艾尔登法环
    /mc Elden Ring
    /评分 黑神话悟空
    /mc 塞尔达 王国之泪

流程：
    1. 智能识别 XX 可能的游戏名称（优先用会话 LLM 把中文俗称/别名/错拼
       规范化为英文官方名；LLM 不可用时用内置别名表 + 启发式降级）。
    2. Metacritic 搜索 -> 取最匹配的 game-title。
    3. 按 PC > PS5 > NS2 > XSX 优先级抓取多平台：媒体评分、媒体评测数量、
       用户评分、前 5 篇媒体评测的分数与前 100 字，并取封面图 / 主视觉图。
    4. 从 templates/ 目录加载选定的独立 HTML 模板，组装数据后通过 AstrBot
       ``html_render``（t2i）渲染为图片发送。
    5. 模板可切换：WebUI 配置项 + ``/评分模板 <名称>`` 命令（支持会话级/全局），
       也可直接把自定义 .html 放进 templates/ 目录。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

# AstrBot v3.5.19+ 会自动识别继承自 Star 的类，官方已弃用 @register 装饰器。
# 但保留 import 以兼容旧版运行时（旧版需要该装饰器才能登记插件元数据）。
try:  # pragma: no cover - 新版仍导出该符号，仅标记为 deprecated
    from astrbot.api.star import register as _register_star
except ImportError:  # pragma: no cover
    _register_star = None

# AstrBot 以 ``data.plugins.<目录名>.main`` 包的形式导入插件；把插件目录加入
# sys.path，保证 ``import metacritic_client`` 这类同级模块导入可用，同时兼容
# 直接把插件目录当顶层目录运行（本地测试）的场景。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot 以包形式导入：data.plugins.<name>.main
    from .metacritic_client import (
        GameInfo,
        GameNotFoundError,
        MetacriticClient,
        MetacriticError,
    )
    from .template import (
        BUILTIN_LABELS,
        TEMPLATES_DIR,
        TemplateNotFoundError,
        available_template_ids,
        build_template_data,
        list_templates,
        load_template,
        resolve_template_name,
    )
except ImportError:  # 本地直接运行 / sys.path 兜底
    from metacritic_client import (  # type: ignore[no-redef]
        GameInfo,
        GameNotFoundError,
        MetacriticClient,
        MetacriticError,
    )
    from template import (  # type: ignore[no-redef]
        BUILTIN_LABELS,
        TEMPLATES_DIR,
        TemplateNotFoundError,
        available_template_ids,
        build_template_data,
        list_templates,
        load_template,
        resolve_template_name,
    )

# 中文 / 俗称 -> Metacritic 英文官方名（LLM 不可用时的降级词典）
ALIASES: dict[str, str] = {
    "艾尔登法环": "Elden Ring",
    "老头环": "Elden Ring",
    "法环": "Elden Ring",
    "黄金树幽影": "Elden Ring: Shadow of the Erdtree",
    "黑神话": "Black Myth: Wukong",
    "黑神话悟空": "Black Myth: Wukong",
    "黑猴": "Black Myth: Wukong",
    "赛博朋克": "Cyberpunk 2077",
    "赛博朋克2077": "Cyberpunk 2077",
    "巫师3": "The Witcher 3: Wild Hunt",
    "巫师三": "The Witcher 3: Wild Hunt",
    "荒野大镖客2": "Red Dead Redemption 2",
    "大镖客2": "Red Dead Redemption 2",
    "gta5": "Grand Theft Auto V",
    "给他爱5": "Grand Theft Auto V",
    "塞尔达 王国之泪": "The Legend of Zelda: Tears of the Kingdom",
    "王国之泪": "The Legend of Zelda: Tears of the Kingdom",
    "塞尔达 旷野之息": "The Legend of Zelda: Breath of the Wild",
    "旷野之息": "The Legend of Zelda: Breath of the Wild",
    "空洞骑士": "Hollow Knight",
    "丝之歌": "Hollow Knight: Silksong",
    "博德之门3": "Baldur's Gate 3",
    "原神": "Genshin Impact",
    "只狼": "Sekiro: Shadows Die Twice",
    "血源": "Bloodborne",
    "黑暗之魂3": "Dark Souls III",
    "对马岛之魂": "Ghost of Tsushima",
    "最后生还者2": "The Last of Us Part II",
    "美末2": "The Last of Us Part II",
    "战神 诸神黄昏": "God of War Ragnarök",
    "怪物猎人 荒野": "Monster Hunter Wilds",
    "怪猎荒野": "Monster Hunter Wilds",
    "明末 渊虚之羽": "Wuchang: Fallen Feathers",
    "明末": "Wuchang: Fallen Feathers",
    "影之刃零": "Phantom Blade Zero",
    "死亡搁浅": "Death Stranding",
    "霍格沃茨之遗": "Hogwarts Legacy",
    "霍格沃兹之遗": "Hogwarts Legacy",
    "生化危机4 重制版": "Resident Evil 4",
    "生化4重制": "Resident Evil 4",
    "最终幻想7 重生": "Final Fantasy VII Rebirth",
    "ff7重生": "Final Fantasy VII Rebirth",
    "宇宙机器人": "Astro Bot",
    "小丑牌": "Balatro",
    "潜行者2": "S.T.A.L.K.E.R. 2: Heart of Chornobyl",
    "天国拯救2": "Kingdom Come: Deliverance II",
    "双影奇境": "Split Fiction",
    "双人成行": "It Takes Two",
    "光与影 33号远征队": "Clair Obscur: Expedition 33",
    "33号远征队": "Clair Obscur: Expedition 33",
}

LLM_SYSTEM_PROMPT = """You are a video game title normalization assistant.
The user gives you a possible game name (may be Chinese, a nickname, a typo,
an abbreviation, or a partial title). Your job is to output the most likely
OFFICIAL ENGLISH title of the game as used on Metacritic.

Rules:
- Reply with ONLY the official English game title, nothing else.
- Do not add quotes, explanations, punctuation, or a trailing period.
- Use the English release title; keep common subtitle punctuation (e.g. ": ").
- If the input is already an English title, fix casing/spelling and return it.
- If you truly cannot identify a game, reply with the single word: UNKNOWN
- Examples:
    艾尔登法环 -> Elden Ring
    老头环 -> Elden Ring
    黑神话悟空 -> Black Myth: Wukong
    塞尔达王国之泪 -> The Legend of Zelda: Tears of the Kingdom
    elden ring -> Elden Ring
    gta5 -> Grand Theft Auto V
    zelda botw -> The Legend of Zelda: Breath of the Wild
"""


def _identity_decorator(*_args, **_kwargs):
    """新版 AstrBot 无需 @register；旧版则使用真实的注册装饰器。"""
    def deco(cls):
        return cls
    return deco


# 旧版 AstrBot（< v3.5.19）依赖 @register 登记元数据；新版会自动发现 Star 子类，
# 但保留装饰器调用也不会有副作用（只是多一次 metadata 赋值）。
register = _register_star or _identity_decorator


@register(
    "astrbot_plugin_metacritic_game_reader",
    "zhoufan",
    "Metacritic 游戏评分查询：智能识别游戏名，抓取 PC/PS5/NS2/XSX 多平台评分，用官方 iconify CSS mask 渲染平台 Logo，支持多套可切换 HTML 模板渲染成图片",
    "1.2.1",
)
class MetacriticGameReader(Star):
    """Metacritic 游戏评分查询插件。

    指令：
        /评分 <游戏名> 或 /mc <游戏名>

    智能识别中文俗称 / 英文别名 / 错拼，抓取 PC、PS5、NS2、XSX 多平台的
    媒体评分、媒体评测数、用户评分，以及前 5 篇媒体评测分数与节选，
    连同封面图 / 主视觉图一起渲染成图片发送。
    """

    # 模板选择持久化在插件 KV 里，键名前缀
    _TPL_KV_KEY = "selected_template"

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg: dict = config or {}
        self.client: MetacriticClient | None = None
        # 会话级模板覆盖（umo -> 模板 ID）；持久化选择单独放在 KV
        self._session_templates: dict[str, str] = {}
        # 简单并发限流，避免同一会话刷屏打爆后端
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self):
        timeout = int(self.cfg.get("request_timeout", 20) or 20)
        max_reviews = int(self.cfg.get("max_reviews", 5) or 5)
        quote_chars = int(self.cfg.get("review_quote_chars", 100) or 100)
        self.client = MetacriticClient(
            timeout=timeout,
            max_reviews=max_reviews,
            quote_chars=quote_chars,
        )
        available = available_template_ids()
        active = self._config_template()
        logger.info(
            "[metacritic] 插件已初始化 (timeout=%s, max_reviews=%s, quote_chars=%s, "
            "template=%s, available=%s)",
            timeout,
            max_reviews,
            quote_chars,
            active,
            available,
        )

    # ------------------------------------------------------------------ #
    # 模板选择
    # ------------------------------------------------------------------ #
    def _config_template(self) -> str:
        """WebUI 配置里选择的模板（非法值自动回退）。"""
        return resolve_template_name(self.cfg.get("template"))

    async def _get_template(self, umo: str | None) -> str:
        """解析本次会话实际使用的模板：会话覆盖 > KV 持久化 > WebUI 配置 > 默认。

        会话覆盖让「/评分模板 neon」可以只影响当前聊天；持久化选择让管理员
        切换后对所有会话生效。
        """
        key = umo or "default"
        if key in self._session_templates:
            return self._session_templates[key]
        try:
            saved = await self.get_kv_data(self._TPL_KV_KEY, "")
        except Exception:  # noqa: BLE001 - KV 不可用时忽略
            saved = ""
        if saved and str(saved) in available_template_ids():
            return str(saved)
        return self._config_template()

    async def _set_template(self, name: str, umo: str | None, persist: bool) -> str:
        """切换模板，返回实际生效的模板 ID。"""
        template_id = (name or "").strip()
        # 显式校验：非法名 / 不存在的模板给出明确提示，而不是静默回退
        if template_id not in available_template_ids():
            raise TemplateNotFoundError(f"模板名「{template_id}」不合法或不存在")
        # 严格校验文件可读且确实是 Jinja2 模板；损坏的模板拒绝切换
        path = TEMPLATES_DIR / f"{template_id}.html"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            raise TemplateNotFoundError(f"模板「{template_id}」无法读取：{e}") from e
        if "{{" not in text and "{%" not in text:
            raise TemplateNotFoundError(
                f"模板「{template_id}」不含 Jinja2 变量，无法用于渲染"
            )
        if persist:
            try:
                await self.put_kv_data(self._TPL_KV_KEY, template_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("[metacritic] 持久化模板选择失败（仅当前会话生效）: %s", e)
            self._session_templates.clear()
        else:
            self._session_templates[umo or "default"] = template_id
        return template_id

    async def terminate(self):
        if self.client:
            await self.client.close()
            self.client = None

    # ------------------------------------------------------------------ #
    # 智能游戏名识别
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_alias(raw: str) -> str | None:
        """内置别名表匹配：先精确，再按「输入包含别名」优先长别名。"""
        key = re.sub(r"\s+", "", raw).lower()
        if not key:
            return None
        # 精确匹配
        for alias, official in ALIASES.items():
            if key == re.sub(r"\s+", "", alias).lower():
                return official
        # 长别名优先（避免 "明末" 抢在 "明末 渊虚之羽" 之前）
        for alias in sorted(ALIASES, key=len, reverse=True):
            akey = re.sub(r"\s+", "", alias).lower()
            if len(akey) >= 2 and akey in key:
                return ALIASES[alias]
        return None

    async def _llm_normalize(self, raw: str, umo: str | None = None) -> str | None:
        """用 AstrBot 当前会话的聊天模型把 XX 规范化成英文官方名。

        v4.5.7+ 使用 ``context.llm_generate``；任何异常都安全降级，
        不会因为没配模型 / 模型报错而让指令失败。
        """
        if not self.cfg.get("use_llm", True):
            return None
        try:
            provider_id = None
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception:  # noqa: BLE001
                try:
                    provider_id = await self.context.get_current_chat_provider_id()
                except Exception:  # noqa: BLE001
                    pass
            if not provider_id:
                return None

            prompt = f"Game name to normalize: {raw}"
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=LLM_SYSTEM_PROMPT,
                )
                text = (getattr(resp, "completion_text", None) or "").strip()
            except AttributeError:
                # 更老版本的 Context 没有 llm_generate
                return None
            return self._sanitize_llm_title(text)
        except Exception as e:  # noqa: BLE001
            logger.warning("[metacritic] LLM 规范化失败，使用降级逻辑: %s", e)
            return None

    @staticmethod
    def _sanitize_llm_title(text: str) -> str | None:
        if not text:
            return None
        text = text.strip().strip("`").strip()
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        text = text.strip('"\'“”‘’').strip()
        text = re.split(r"[\r\n]", text)[0].strip()
        # 模型有时会带 "Title: " 之类前缀
        text = re.sub(
            r"^(official\s+)?(english\s+)?(game\s+)?(title|name)\s*[:：]\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        if not text or text.upper() == "UNKNOWN" or len(text) > 120:
            return None
        # 纯中文结果说明模型没翻译，视为失败
        if re.fullmatch(r"[\u4e00-\u9fff\s·：:，,、\-—0-9]+", text):
            return None
        return text

    async def detect_game_name(
        self, event: AstrMessageEvent, raw: str
    ) -> tuple[str, str]:
        """返回 (用于搜索的英文游戏名, 识别方式说明)。"""
        raw = (raw or "").strip().strip("/")
        if not raw:
            return "", "空输入"

        umo = getattr(event, "unified_msg_origin", None)

        # 1) 内置别名表（最快、最稳，避免 LLM 幻觉把中文名改坏）
        alias = self._normalize_alias(raw)
        if alias:
            logger.info("[metacritic] 别名表命中: %r -> %r", raw, alias)
            return alias, "别名表"

        # 2) LLM 智能识别（中文俗称 / 错拼 / 缩写 / 模糊描述）
        llm = await self._llm_normalize(raw, umo=umo)
        if llm:
            logger.info("[metacritic] LLM 识别: %r -> %r", raw, llm)
            return llm, "LLM"

        # 3) 已经是英文/罗马字，直接用（清洗首尾标点）
        cleaned = re.sub(r"^[^\w]+|[^\w]+$", "", raw, flags=re.UNICODE)
        if cleaned and not re.search(r"[\u4e00-\u9fff]", cleaned):
            return cleaned, "原文"

        # 4) 中文且 LLM 不可用：仍尝试原样搜索
        logger.info("[metacritic] 未归一化，原样搜索: %r", raw)
        return raw, "原文"

    # ------------------------------------------------------------------ #
    # 命令处理
    # ------------------------------------------------------------------ #
    async def _handle(self, event: AstrMessageEvent, raw: str):
        yield event.plain_result(f"🔎 正在 Metacritic 查询「{raw}」…")

        keyword, method = await self.detect_game_name(event, raw)
        if not keyword:
            yield event.plain_result(
                "请提供游戏名称，例如：/评分 艾尔登法环  或  /mc Elden Ring"
            )
            return

        umo = getattr(event, "unified_msg_origin", None) or "default"
        lock = self._locks.setdefault(umo, asyncio.Lock())
        if lock.locked():
            yield event.plain_result("上一个查询还在进行中，请稍等一下～")
            return

        async with lock:
            client = self.client
            if client is None:
                client = MetacriticClient()
                self.client = client

            try:
                game: GameInfo = await client.get_game_info(keyword)
            except GameNotFoundError as e:
                yield event.plain_result(
                    f"😕 {e}\n\n💡 可尝试输入英文官方名，如 /mc Elden Ring"
                )
                return
            except MetacriticError as e:
                logger.error("[metacritic] 数据获取失败: %s", e)
                yield event.plain_result(f"获取 Metacritic 数据失败：{e}")
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("[metacritic] 未预期的错误: %s", e)
                yield event.plain_result(
                    f"查询出错啦（{type(e).__name__}），请稍后重试。",
                )
                return

            if not game.platforms:
                yield event.plain_result(
                    f"找到《{game.title}》，但没有 PC / PS5 / NS2 / XSX 平台的评分数据。\n"
                    f"详情：{game.source_url}"
                )
                return

            # 解析本次使用的模板 -> 组装 HTML -> 交给 AstrBot t2i 渲染
            # load_template 内部对损坏/缺失模板会自动回退，保证永远有模板可用
            template_name = await self._get_template(umo)
            tmpl = load_template(template_name)
            quote_chars = int(self.cfg.get("review_quote_chars", 100) or 100)
            data = build_template_data(game, template_name, quote_chars)
            options = {
                "full_page": True,
                "type": "jpeg",
                "quality": 85,
                "timeout": 45000,
                "scale": "css",
            }
            try:
                url = await self.html_render(
                    tmpl, data, options=options, umo=umo
                )
            except Exception as e:  # noqa: BLE001 - 渲染失败给文本兜底
                logger.exception("[metacritic] t2i 渲染失败: %s", e)
                yield event.plain_result(self._text_fallback(game, method))
                return

            # summary = (
            #     f"《{game.title}》 · 识别方式：{method} · "
            #     f"平台：{game.platform_names} · 模板：{template_name}"
            # )
            yield event.image_result(url)
            ## 仅调试用
            # yield event.plain_result(summary + f"\n{game.source_url}")

    @staticmethod
    def _text_fallback(game: GameInfo, method: str) -> str:
        lines = [
            f"🎮 {game.title}（识别方式：{method}）",
            f"🔗 {game.source_url}",
        ]
        for p in game.platforms:
            lines.append(
                f"\n【{p.name}】媒体 {p.critic_score or '—'} "
                f"({p.critic_review_count or 0} 篇) | "
                f"用户 {p.user_score or '—'} ({p.user_review_count or 0} 条)"
            )
            for r in p.reviews[:5]:
                lines.append(f"  · {r.score_text} {r.publication}：{r.quote}")
        lines.append("\n（图片渲染服务暂不可用，已以文本形式返回）")
        return "\n".join(lines)

    @filter.command("评分", alias={"mc"})
    async def cmd_score(self, event: AstrMessageEvent, game_name: str = ""):
        """查询 Metacritic 游戏评分，如 /评分 艾尔登法环 或 /mc Elden Ring

        Args:
            game_name(string): 游戏名称（支持中文俗称、英文全名、缩写、带空格的标题）
        """
        # 注意：框架会把带默认值的参数只按「第一个词」传入（GreedyStr 注解会被
        # 默认值覆盖），因此始终优先从原始消息里截取指令之后的完整文本，以支持
        # "Elden Ring" / "塞尔达 王国之泪" 这类含空格的游戏名。
        raw = self._extract_raw(event, "评分") or (game_name or "").strip()
        async for result in self._handle(event, raw):
            yield result

    # ------------------------------------------------------------------ #
    # 模板切换命令
    # ------------------------------------------------------------------ #
    @filter.command("评分模板", alias={"mc模板"})
    async def cmd_template(self, event: AstrMessageEvent, arg: str = ""):
        """查看或切换评分图片模板，如 /评分模板 neon、/评分模板 列表

        Args:
            arg(string): 模板名称 / 列表 / 全局 / 重置 / 清空
        """
        args = self._extract_raw(event, "评分模板")
        parts = args.split() if args else []
        umo = getattr(event, "unified_msg_origin", None)
        current = await self._get_template(umo)

        # 无参数 / 列表：展示所有模板
        if not parts or parts[0] in ("列表", "list", "ls", "?"):
            lines = [f"🎨 可用评分模板（当前：{current}）", ""]
            for t in list_templates():
                mark = "✅" if t["id"] == current else "　"
                lines.append(f" {mark} {t['id']:<10} {t['label']}")
            lines += [
                "",
                "切换：/评分模板 neon（仅当前会话生效）",
                "全局：/评分模板 neon 全局（所有会话生效，写入持久化）",
                "重置：/评分模板 重置（清除会话与全局覆盖，回到 WebUI 配置）",
                "自定义：把 .html 放进插件 templates/ 目录并重载插件即可选择。",
            ]
            yield event.plain_result("\n".join(lines))
            return

        first = parts[0]
        # 重置
        if first in ("重置", "reset", "清空", "clear"):
            self._session_templates.pop(umo or "default", None)
            try:
                await self.delete_kv_data(self._TPL_KV_KEY)
            except Exception:  # noqa: BLE001
                pass
            yield event.plain_result(
                f"♻️ 已重置模板覆盖，当前使用 WebUI 配置：{self._config_template()}"
            )
            return

        # 是否全局持久化
        persist = any(p in ("全局", "global", "all") for p in parts[1:])
        try:
            chosen = await self._set_template(first, umo, persist)
        except TemplateNotFoundError as e:
            ids = "、".join(t["id"] for t in list_templates())
            yield event.plain_result(f"❌ {e}\n可用模板：{ids}")
            return

        scope = "全局（已持久化）" if persist else "当前会话"
        label = BUILTIN_LABELS.get(chosen, "自定义模板")
        yield event.plain_result(
            f"✅ 已切换模板 → {chosen}（{label}）\n"
            f"生效范围：{scope}\n发送 /评分 <游戏名> 即可查看效果。"
        )

    @staticmethod
    def _extract_raw(event: AstrMessageEvent, command: str = "评分") -> str:
        """取指令之后的完整文本（兼容 /评分、评分、/mc、mc 及唤醒前缀）。

        指令词支持中英文别名；取「最后一个匹配到的指令词」之后的全部文本，
        因此带空格的游戏名 / 模板参数不会被截断。
        """
        text = (event.get_message_str() or "").strip()
        aliases = {
            "评分模板": ["评分模板", "mc模板"],
            "评分": ["评分", "mc"],
        }.get(command, [command])
        # 用 alternation 匹配任一别名；长别名优先避免 "评分模板" 被 "评分" 抢先
        pat = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
        m = re.search(
            rf"[/／]?\s*(?:{pat})\s*(.*)$",
            text,
            flags=re.IGNORECASE,
        )
        return m.group(1).strip() if m else ""
