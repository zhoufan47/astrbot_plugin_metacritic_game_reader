"""Metacritic 游戏评分数据抓取客户端（纯 aiohttp，无第三方硬依赖）。

数据来源为 Metacritic 官网 Nuxt 前端所使用的公开 JSON 后端
``https://backend.metacritic.com``。本模块只做静态读取，不执行目标页面脚本。

已验证的端点（2026-11）：
  - 搜索:   /finder/metacritic/search/{keyword}/web?...&mcoTypeId=13
  - 详情:   /games/metacritic/{slug}/web?...&componentName=product
  - 媒体分: /reviews/metacritic/critic/games/{slug}/[platform/{p}/]stats/web
  - 用户分: /reviews/metacritic/user/games/{slug}/[platform/{p}/]stats/web
  - 评测:   /reviews/metacritic/critic/games/{slug}/[platform/{p}/]web?...&limit=10

注意：查询串里的 ``?platform=`` 会被后端忽略，平台必须通过路径段
``/platform/{slug}/`` 指定，否则返回的是「主平台」的数据。
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

BACKEND = "https://backend.metacritic.com"
IMG_BASE = "https://www.metacritic.com/a/img/catalog"

# 只需一个浏览器 UA；apiKey 实测可省略（保留空值以贴合站点请求）
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.metacritic.com/",
    "Origin": "https://www.metacritic.com",
}

# 目标平台优先级（用户要求）：PC > PS5 > NS2 > XSX，其它平台忽略
PLATFORM_PRIORITY: list[tuple[str, str]] = [
    ("PC", "pc"),
    ("PlayStation 5", "playstation-5"),
    ("Nintendo Switch 2", "nintendo-switch-2"),
    ("Xbox Series X", "xbox-series-x"),
]
PRIORITY_SLUGS = {slug for _, slug in PLATFORM_PRIORITY}
PRIORITY_RANK = {slug: i for i, (_, slug) in enumerate(PLATFORM_PRIORITY)}


class MetacriticError(Exception):
    """Metacritic 数据获取失败。"""


class GameNotFoundError(MetacriticError):
    """没有找到匹配的游戏。"""


@dataclass
class Review:
    """一篇媒体评测（Metacritic critic review）。"""

    score: int | None
    publication: str
    quote: str  # 已截断到前 N 个字符
    date: str = ""

    @property
    def score_text(self) -> str:
        return str(self.score) if self.score is not None else "–"


@dataclass
class PlatformScore:
    """单个平台的评分汇总。"""

    name: str
    slug: str
    critic_score: int | None = None
    critic_review_count: int | None = None
    critic_sentiment: str = ""
    user_score: float | None = None
    user_review_count: int | None = None
    user_sentiment: str = ""
    reviews: list[Review] = field(default_factory=list)

    @property
    def has_score(self) -> bool:
        return self.critic_score is not None or self.user_score is not None


@dataclass
class GameInfo:
    """组装好的游戏评分数据，直接交给 HTML 模板渲染。"""

    title: str
    slug: str
    query: str
    release_date: str = ""
    premiere_year: str = ""
    genres: list[str] = field(default_factory=list)
    developer: str = ""
    publisher: str = ""
    cover_url: str = ""  # 封面图（cardImage，竖版）
    hero_url: str = ""  # 主视觉图（mainImage）
    description: str = ""
    platforms: list[PlatformScore] = field(default_factory=list)
    source_url: str = ""

    @property
    def platform_names(self) -> str:
        return " / ".join(p.name for p in self.platforms)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_text(text: str) -> str:
    """去掉 HTML 标签、压缩空白，便于放进模板。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int = 100) -> str:
    """按「前 100 字」截取，超出部分以省略号结尾。"""
    s = _clean_text(text)
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…"


def image_url(image: dict | None) -> str:
    """把 product.images[] 里的 bucketPath 还原成可访问的图片 URL。"""
    if not isinstance(image, dict):
        return ""
    if image.get("path"):
        return str(image["path"])
    bucket_path = image.get("bucketPath") or ""
    if not bucket_path:
        return ""
    # bucketPath 形如 /provider/6/3/xxx.jpg
    return IMG_BASE + bucket_path


class MetacriticClient:
    """异步 Metacritic 客户端。用完调用 close()，或使用 async with。"""

    def __init__(self, timeout: int = 20, max_reviews: int = 5, quote_chars: int = 100):
        self.timeout = timeout
        self.max_reviews = max_reviews
        self.quote_chars = quote_chars
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=DEFAULT_HEADERS,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "MetacriticClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _json(self, url: str) -> dict | None:
        """GET 一个 JSON 端点；非 200 或解析失败返回 None（便于降级）。"""
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    @staticmethod
    def _component_params(name: str, display: str, ctype: str) -> str:
        return (
            f"componentName={name}"
            f"&componentDisplayName={quote(display)}"
            f"&componentType={ctype}"
        )

    # ------------------------------------------------------------------ #
    # 1. 搜索：找出最可能的游戏
    # ------------------------------------------------------------------ #
    async def search(self, keyword: str, limit: int = 15) -> list[dict]:
        """按关键词搜索游戏，返回候选列表（按后端相关性排序）。

        搜索索引是英文/罗马化的，中文别名需要先经 LLM 转换成英文官方名。
        """
        kw = quote(keyword.strip())
        if not kw:
            return []
        params = (
            "offset=0"
            f"&limit={limit}"
            "&mcoTypeId=13"  # 13 = games
            "&sortBy=&sortDirection=DESC"
            + "&" + self._component_params("search", "Search", "SearchResults")
        )
        url = f"{BACKEND}/finder/metacritic/search/{kw}/web?{params}"
        data = await self._json(url)
        if not data:
            return []
        items = (data.get("data") or {}).get("items") or []
        return [
            it
            for it in items
            if isinstance(it, dict)
            and it.get("type") == "game-title"
            and it.get("slug")
            and it.get("title")
        ]

    @staticmethod
    def _score_candidate(candidate: dict, keyword: str) -> float:
        """给候选项打分，用于从搜索结果里挑出最可能的游戏。"""
        title = str(candidate.get("title") or "")
        slug = str(candidate.get("slug") or "")
        kw = keyword.strip().lower()
        t = title.lower()
        s = slug.lower().replace("-", " ")
        score = 0.0
        if t == kw:
            score += 100
        elif s == kw.replace(" ", "-"):
            score += 90
        elif t.startswith(kw):
            score += 60
        elif kw in t:
            score += 40
        elif kw in s:
            score += 25
        # 有媒体评分的通常是正式作品而非占位/DLC 条目
        cs = candidate.get("criticScoreSummary") or {}
        if _as_int(cs.get("score")) is not None:
            score += 15
        # 标题越短越接近本体（避免 "XXX: Some DLC" 排在前面）
        score -= min(len(title), 80) * 0.1
        return score

    async def find_game(self, keyword: str) -> dict:
        """返回最匹配的候选；找不到抛 GameNotFoundError。"""
        candidates = await self.search(keyword)
        if not candidates:
            raise GameNotFoundError(f"未在 Metacritic 找到与「{keyword}」相关的游戏")
        best = max(candidates, key=lambda c: self._score_candidate(c, keyword))
        return best

    # ------------------------------------------------------------------ #
    # 2. 详情（标题、封面、主视觉、平台列表）
    # ------------------------------------------------------------------ #
    async def get_product(self, slug: str) -> dict | None:
        params = self._component_params("product", "Product", "Product")
        return await self._json(f"{BACKEND}/games/metacritic/{slug}/web?{params}")

    @staticmethod
    def _extract_images(item: dict) -> tuple[str, str]:
        cover, hero = "", ""
        for im in item.get("images") or []:
            if not isinstance(im, dict):
                continue
            url = image_url(im)
            kind = (im.get("typeName") or "").lower()
            if kind == "cardimage" and not cover:
                cover = url
            elif kind == "mainimage" and not hero:
                hero = url
            elif not cover:
                cover = url
        # 兜底：两张图互相补齐
        return cover or hero, hero or cover

    @staticmethod
    def _extract_platforms(item: dict) -> list[dict]:
        """只保留 4 个目标平台，并按 PC > PS5 > NS2 > XSX 排序。"""
        picked: list[dict] = []
        for p in item.get("platforms") or []:
            if not isinstance(p, dict):
                continue
            slug = str(p.get("slug") or "")
            if slug in PRIORITY_SLUGS:
                picked.append(p)
        picked.sort(key=lambda p: PRIORITY_RANK.get(str(p.get("slug")), 99))
        return picked

    # ------------------------------------------------------------------ #
    # 3. 每个平台的评分 / 评测
    # ------------------------------------------------------------------ #
    async def _platform_stats(self, slug: str, kind: str, platform_slug: str | None) -> dict:
        seg = f"/platform/{platform_slug}" if platform_slug else ""
        p = self._component_params(
            f"{kind}-score-summary",
            f"{kind.title()} Score Summary",
            "MetaScoreSummary",
        )
        url = (
            f"{BACKEND}/reviews/metacritic/{kind}/games/{slug}{seg}/stats/web?{p}"
        )
        data = await self._json(url)
        if not data:
            return {}
        return (data.get("data") or {}).get("item") or {}

    async def _platform_reviews(
        self, slug: str, platform_slug: str | None, limit: int = 10
    ) -> list[dict]:
        seg = f"/platform/{platform_slug}" if platform_slug else ""
        p = self._component_params("critic-reviews", "Critic Reviews", "ReviewList")
        url = (
            f"{BACKEND}/reviews/metacritic/critic/games/{slug}{seg}/web?"
            f"offset=0&limit={limit}&filterBySentiment=all&sort=score&{p}"
        )
        data = await self._json(url)
        if not data:
            return []
        return (data.get("data") or {}).get("items") or []

    async def _fetch_platform(
        self, slug: str, platform: dict
    ) -> PlatformScore:
        name = str(platform.get("name") or "")
        pslug = str(platform.get("slug") or "")
        ps = PlatformScore(name=name, slug=pslug)

        critic, user, raw_reviews = await asyncio.gather(
            self._platform_stats(slug, "critic", pslug),
            self._platform_stats(slug, "user", pslug),
            self._platform_reviews(slug, pslug, limit=max(self.max_reviews, 5)),
        )

        # product.platforms[].criticScoreSummary 是站点「平台卡片」上展示的
        # 权威数值（例如 PC = 94 / 63 篇）；stats 端点的 reviewCount 有时只统计
        # 参与加权计算的评分（60），因此优先使用前者，stats 作为兜底。
        fallback = platform.get("criticScoreSummary") or {}
        ps.critic_score = _as_int(fallback.get("score"))
        if ps.critic_score is None:
            ps.critic_score = _as_int(critic.get("score"))
        ps.critic_review_count = _as_int(fallback.get("reviewCount"))
        if ps.critic_review_count is None:
            ps.critic_review_count = _as_int(critic.get("reviewCount"))
        ps.critic_sentiment = str(
            fallback.get("sentiment") or critic.get("sentiment") or ""
        )
        ps.user_score = _as_float(user.get("score"))
        ps.user_review_count = _as_int(user.get("reviewCount"))
        ps.user_sentiment = str(user.get("sentiment") or "")

        for r in raw_reviews[: self.max_reviews]:
            if not isinstance(r, dict):
                continue
            ps.reviews.append(
                Review(
                    score=_as_int(r.get("score")),
                    publication=str(r.get("publicationName") or r.get("author") or "Unknown"),
                    quote=truncate(str(r.get("quote") or ""), self.quote_chars),
                    date=str(r.get("date") or ""),
                )
            )
        return ps

    # ------------------------------------------------------------------ #
    # 4. 组装
    # ------------------------------------------------------------------ #
    async def get_game_info(self, keyword: str) -> GameInfo:
        """智能识别游戏名 -> 抓取全平台评分 -> 返回渲染数据。"""
        candidate = await self.find_game(keyword)
        slug = str(candidate["slug"])
        product = await self.get_product(slug)
        item: dict = {}
        if product:
            item = (product.get("data") or {}).get("item") or {}
        if not item:
            item = dict(candidate)  # 降级使用搜索结果里的字段

        cover, hero = self._extract_images(item)
        production = item.get("production") or {}
        companies = production.get("companies") or [] if isinstance(production, dict) else []

        def company_of(typename: str) -> str:
            for c in companies:
                if isinstance(c, dict) and (c.get("typeName") or "").lower() == typename:
                    return str(c.get("name") or "")
            return ""

        target_platforms = self._extract_platforms(item)
        # 若 product 没给出平台列表（罕见），至少用搜索结果里的平台补一个主平台
        if not target_platforms and candidate.get("platforms"):
            for p in candidate["platforms"]:
                if isinstance(p, dict) and p.get("slug") in PRIORITY_SLUGS:
                    target_platforms.append(p)
        # 仍为空时，抓取不带平台段的默认（主平台）数据
        fetch_list: list[dict] = target_platforms or [
            {"name": str(item.get("platform") or "Metacritic"), "slug": ""}
        ]

        scores = await asyncio.gather(
            *(self._fetch_platform(slug, p) for p in fetch_list)
        )
        # 丢弃完全没有评分的平台（未发售/无评测）
        platforms = [s for s in scores if s.has_score]

        return GameInfo(
            title=str(item.get("title") or candidate.get("title") or keyword),
            slug=slug,
            query=keyword,
            release_date=str(item.get("releaseDate") or candidate.get("releaseDate") or ""),
            premiere_year=str(item.get("premiereYear") or candidate.get("premiereYear") or ""),
            genres=[str(g.get("name")) for g in (item.get("genres") or []) if isinstance(g, dict) and g.get("name")],
            developer=company_of("developer"),
            publisher=company_of("publisher"),
            cover_url=cover,
            hero_url=hero,
            description=truncate(str(item.get("description") or ""), 160),
            platforms=platforms,
            source_url=f"https://www.metacritic.com/game/{slug}/",
        )
