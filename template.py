"""HTML 模板加载器与 Jinja2 渲染数据组装。

所有模板都是 ``templates/`` 目录下的独立 ``.html`` 文件，使用 HTML + Jinja2
语法。本模块负责：

- 发现 / 列出可用模板（含插件内置模板与用户自行放入的模板）
- 按名称读取模板（带缓存，重载插件后自动生效）
- 校验名称合法性，防止路径穿越
- 把 ``GameInfo`` 转成模板可用的纯字典

模板文件不依赖任何外部字体 / JS / CDN，整页由 AstrBot t2i（Playwright）截图。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:
    from .metacritic_client import GameInfo
    from .platform_icons import (
        PLATFORM_COLORS,
        iconify_css,
        platform_icon,
        platform_icon_map,
        platform_iconify_class,
    )
except ImportError:  # 顶层导入兜底
    from metacritic_client import GameInfo  # type: ignore[no-redef]
    from platform_icons import (  # type: ignore[no-redef]
        PLATFORM_COLORS,
        iconify_css,
        platform_icon,
        platform_icon_map,
        platform_iconify_class,
    )

# 模板目录与本文件同目录
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# 内置模板的展示名（用于 /评分模板 列表）；用户自建模板显示为文件名
BUILTIN_LABELS: dict[str, str] = {
    "classic": "经典卡片 · 浅色分区，蓝色主视觉（默认）",
    "neon": "霓虹赛博 · 深色科技风，青紫配色",
    "compact": "报纸杂志 · 双栏紧凑排版，信息密度高",
    "glass": "通透玻璃 · 半透明悬浮面板，背景模糊与微亮边缘",
    "darkglass": "深色分层玻璃 · 渐变网格背景，冷白文字与紫色点缀",
}

DEFAULT_TEMPLATE = "classic"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")

# 兜底模板：当 templates/ 整个目录不可用时使用，保证插件永远能出图
_FALLBACK_TEMPLATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<style>
body{width:820px;background:#f4f6fa;font-family:"PingFang SC","Microsoft YaHei",sans-serif;color:#1b2430;margin:0}
.wrap{background:#fff;border-radius:16px;overflow:hidden;margin:20px}
.hero{padding:26px 30px;background:linear-gradient(120deg,#12203a,#3a5f96);color:#fff}
h1{font-size:34px;margin:0 0 8px}.sub{font-size:14px;color:#c9d8ef}
.body{padding:24px 30px}.p{border:1px solid #e3e9f2;border-radius:12px;padding:16px;margin-bottom:14px}
.pn{font-size:18px;font-weight:800;margin-bottom:10px}
.s{display:inline-block;background:#f4f6fa;border-radius:8px;padding:8px 14px;margin-right:10px;font-size:14px}
.rv{border-top:1px solid #eef1f6;padding:8px 0;font-size:13px;color:#5b687c}
.ft{padding:14px 30px;font-size:12px;color:#93a0b4;border-top:1px solid #e8edf4}
</style></head><body><div class="wrap">
<div class="hero"><h1>{{ game.title }}</h1>
<div class="sub">METACRITIC · {{ game.release_date }} · {{ game.developer }} · {{ template_name }}</div></div>
<div class="body">
<div style="margin-bottom:16px;font-size:13px;color:#5b687c">{{ game.description }}</div>
{% for p in game.platforms %}<div class="p"><div class="pn">{{ p.name }}</div>
<span class="s">媒体 {{ p.critic_score if p.critic_score is not none else '–' }}（{{ p.critic_review_count or 0 }} 篇）</span>
<span class="s">用户 {{ p.user_score if p.user_score is not none else '–' }}（{{ p.user_review_count or 0 }} 条）</span>
{% for r in p.reviews %}<div class="rv"><b>{{ r.score_text }}</b> {{ r.publication }} — {{ r.quote }}</div>{% endfor %}
</div>{% endfor %}</div>
<div class="ft">数据来源 Metacritic · {{ game.source_url }}</div></div></body></html>"""

# 模板内容缓存：{模板 ID: 模板文本}。插件重载后模块重新导入，缓存自动清空。
_cache: dict[str, str] = {}

# 向后兼容：老代码可能仍引用 HTML_TEMPLATE（默认模板内容）
def _default_template_text() -> str:
    try:
        return load_template(DEFAULT_TEMPLATE)
    except Exception:
        return ""


class TemplateNotFoundError(Exception):
    """请求的模板不存在或非法。"""


def is_valid_name(name: str) -> bool:
    """模板 ID 只能是字母数字下划线连字符，杜绝路径穿越。"""
    return bool(name) and bool(_SAFE_NAME.match(name))


def list_templates() -> list[dict[str, str]]:
    """列出所有可用模板，返回 [{id, label, builtin}]，按 ID 排序。

    内置模板始终排在前面（按 BUILTIN_LABELS 定义顺序），其余按文件名排序。
    """
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    if TEMPLATES_DIR.is_dir():
        files = sorted(TEMPLATES_DIR.glob("*.html"))
    else:
        files = []

    def add(template_id: str) -> None:
        if template_id in seen:
            return
        seen.add(template_id)
        builtin = template_id in BUILTIN_LABELS
        result.append(
            {
                "id": template_id,
                "label": BUILTIN_LABELS.get(template_id, f"自定义模板 · {template_id}"),
                "builtin": "true" if builtin else "false",
            }
        )

    for template_id in BUILTIN_LABELS:
        if any(f.stem == template_id for f in files):
            add(template_id)
    for f in files:
        if is_valid_name(f.stem):
            add(f.stem)
    return result


def available_template_ids() -> list[str]:
    return [t["id"] for t in list_templates()]


def resolve_template_name(name: str | None) -> str:
    """把配置值规范化成合法的模板 ID；非法 / 缺失时回退到默认模板。"""
    if not name or not is_valid_name(str(name)):
        return DEFAULT_TEMPLATE
    candidate = str(name)
    if candidate in available_template_ids():
        return candidate
    return DEFAULT_TEMPLATE


def _inject_iconify_css(text: str) -> str:
    """把官方同款 iconify mask CSS 注入模板。

    默认自动注入，因此模板里只要写
    ``<span class="iconify i-mc-platform:pclogo-bold"></span>`` 即可，
    无需关心 CSS 从哪来。若模板已显式使用 ``{{ iconify_css }}`` 变量
    （自定义模板想自己控制位置），则不重复注入。
    """
    if "iconify_css" in text or ".i-mc-platform\\:" in text:
        return text
    css = "<style>\n" + iconify_css() + "\n</style>\n"
    # 放到 </head> 之前；没有 head 时直接前置
    idx = text.lower().find("</head>")
    if idx != -1:
        return text[:idx] + css + text[idx:]
    return css + text


def load_template(name: str | None = None) -> str:
    """读取并返回模板文本。非法名称 / 文件缺失时回退到默认模板。

    会自动注入官方同款 ``iconify`` CSS mask 规则，使模板可以直接用
    ``<span class="iconify i-mc-platform:xxx">`` 渲染平台 logo。
    结果会被缓存；修改模板文件后重载插件即可生效。
    """
    template_id = resolve_template_name(name)
    if template_id in _cache:
        return _cache[template_id]

    path = TEMPLATES_DIR / f"{template_id}.html"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return _fallback(template_id, "读取失败")

    # 基本校验：必须含有 Jinja2 变量引用，避免把纯静态文件当成模板
    if "{{" not in text and "{%" not in text:
        return _fallback(template_id, "不含任何 Jinja2 变量")
    text = _inject_iconify_css(text)
    _cache[template_id] = text
    return text


def _fallback(template_id: str, reason: str) -> str:
    """模板不可用时：先尝试默认模板，仍不行则用内置极简模板，保证永远能出图。"""
    try:
        from astrbot.api import logger
        logger.warning(
            "[metacritic] 模板 %s.html %s，回退到 %s",
            template_id,
            reason,
            DEFAULT_TEMPLATE,
        )
    except Exception:
        pass
    if template_id != DEFAULT_TEMPLATE:
        try:
            return load_template(DEFAULT_TEMPLATE)
        except Exception:
            pass
    return _FALLBACK_TEMPLATE


def clear_cache() -> None:
    """清空模板缓存（切换模板 / 热重载时可调用）。"""
    _cache.clear()


# 各模板的 logo 配色策略：
#   "brand"  -> 各平台品牌色（浅色模板，默认）
#   "white"  -> 全白（深色模板）
#   "ink"    -> 深墨色（报纸模板）
TEMPLATE_ICON_THEME: dict[str, str] = {
    "classic": "brand",
    "neon": "white",
    "compact": "ink",
    "glass": "brand",
    "darkglass": "white",
}
_THEME_COLORS = {"white": "#ffffff", "ink": "#20201e"}


def _icon_color_for(template_id: str, slug: str) -> str | None:
    """返回该模板下指定平台应使用的 logo 颜色；None 表示用品牌色。"""
    theme = TEMPLATE_ICON_THEME.get(template_id, "brand")
    return _THEME_COLORS.get(theme)


def _brand_color(slug: str) -> str:
    return PLATFORM_COLORS.get(slug, "#334155")


def build_template_data(
    game: GameInfo,
    template_name: str | None = None,
    quote_chars: int = 100,
) -> dict[str, Any]:
    """把 GameInfo 转成 Jinja2 可渲染的纯字典。

    除了 ``game`` 之外，还注入 ``template_name``（当前模板名）与
    ``quote_chars``（评测节选字数）。每个平台额外注入 ``icon``
    （来自 Metacritic 官方 ``mc-platform`` 图标集的内联 SVG data URI，
    已按模板主题配色），模板可直接 ``<img src="{{ p.icon }}">`` 使用；
    顶层 ``platform_icons`` 是 ``{slug: data_uri}`` 全量映射。
    """
    template_id = resolve_template_name(template_name)

    def icon_for(slug: str) -> str:
        return platform_icon(slug, _icon_color_for(template_id, slug))

    # 官方 iconify class 方式的颜色：通过父元素 color 控制
    theme_color = _icon_color_for(template_id, "")

    return {
        "template_name": template_id,
        "quote_chars": int(quote_chars),
        # 官方同款 CSS（一般已由 load_template 自动注入；显式暴露供自定义模板用）
        "iconify_css": iconify_css(),
        # logo 颜色（None 表示各平台用品牌色），供 span 的 style="color:..." 使用
        "icon_color": theme_color,
        # 全量映射：{slug: data_uri}，供仍想用 <img> 的模板取用
        "platform_icons": {s: icon_for(s) for s in platform_icon_map()},
        "game": {
            "title": game.title,
            "slug": game.slug,
            "query": game.query,
            "release_date": game.release_date,
            "premiere_year": game.premiere_year,
            "genres": game.genres,
            "developer": game.developer,
            "publisher": game.publisher,
            "cover_url": game.cover_url,
            "hero_url": game.hero_url,
            "description": game.description,
            "source_url": game.source_url,
            "platforms": [
                {
                    "name": p.name,
                    "slug": p.slug,
                    "icon": icon_for(p.slug),  # 兼容：<img> 用的 data URI
                    "icon_class": platform_iconify_class(p.slug),  # 官方 iconify class
                    "icon_color": theme_color or _brand_color(p.slug),
                    "critic_score": p.critic_score,
                    "critic_review_count": p.critic_review_count,
                    "critic_sentiment": p.critic_sentiment,
                    "user_score": p.user_score,
                    "user_review_count": p.user_review_count,
                    "user_sentiment": p.user_sentiment,
                    "reviews": [
                        {
                            "score": r.score,
                            "score_text": r.score_text,
                            "publication": r.publication,
                            "quote": r.quote,
                            "date": r.date,
                        }
                        for r in p.reviews
                    ],
                }
                for p in game.platforms
            ],
        },
    }


# 兼容旧接口：保持 HTML_TEMPLATE 可用（值默认模板文本）
HTML_TEMPLATE = _default_template_text()
