"""LLM 翻译助手：把媒体评测节选 / 游戏简介翻译成简体中文。

设计要点：

- **批量翻译**：所有待翻译文本合并成一次 LLM 调用（JSON 数组进、JSON 数组出），
  相比逐条调用显著减少请求次数与 system prompt 的重复 token 开销；
  相同文本自动去重，只翻译一次。
- **Provider 可配置**：``resolve_provider_id`` 供游戏名识别与翻译共用——
  优先使用插件配置里指定的提供商（WebUI 下拉选择 AstrBot 已配置的聊天模型），
  留空时回退到当前会话正在使用的聊天模型。
- **安全降级**：任何失败（未配置模型 / 请求报错 / 输出不合约定）都返回
  ``None``，由调用方保留英文原文，不影响评分查询主流程。
"""

from __future__ import annotations

import json
import re
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # 本地测试 / 独立运行兜底
    import logging

    logger = logging.getLogger("metacritic.translator")

TRANSLATE_SYSTEM_PROMPT = """You are a professional video game media translator.
The user message is a JSON array of texts (game review excerpts and/or a game description).
Translate EVERY item into Simplified Chinese (简体中文).

Rules:
- Reply with ONLY a JSON array of translated strings, in the same order and with the same length as the input.
- Do not wrap the JSON in markdown code fences; do not add explanations or notes.
- Translate faithfully and naturally, keeping the tone of a game review.
- For game titles, character names and publication names, use their established Chinese names when they exist; otherwise keep the original English.
- If an item is already in Chinese, return it unchanged.
- Never merge, split, skip or reorder items.
"""


def parse_translations(text: str, expected: int) -> list[str] | None:
    """解析 LLM 返回的 JSON 数组；不合约定时返回 None（调用方保留原文）。

    容忍模型偶尔包上的 markdown 代码围栏；要求项数一致且每项都是非空字符串。
    """
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list) or len(data) != expected:
        return None
    if not all(isinstance(item, str) and item.strip() for item in data):
        return None
    return [item.strip() for item in data]


async def resolve_provider_id(
    context: Any,
    provider_id: str | None = None,
    umo: str | None = None,
) -> str | None:
    """解析实际使用的聊天模型 Provider ID。

    插件配置指定的提供商优先；留空时回退到当前会话正在使用的聊天模型
    （兼容旧版 ``get_current_chat_provider_id`` 不带 umo 参数的签名）。
    都拿不到时返回 None。
    """
    pid = (provider_id or "").strip()
    if pid:
        return pid
    try:
        return await context.get_current_chat_provider_id(umo=umo)
    except Exception:  # noqa: BLE001 - 旧版签名可能不带 umo
        try:
            return await context.get_current_chat_provider_id()
        except Exception:  # noqa: BLE001
            return None


async def llm_translate(
    context: Any,
    texts: list[str],
    provider_id: str | None = None,
    umo: str | None = None,
) -> dict[str, str] | None:
    """批量翻译文本，返回 ``{原文: 译文}`` 映射；失败返回 None。

    Args:
        context: AstrBot Context（需要 ``llm_generate``，v4.5.7+）。
        texts: 待翻译文本列表（可含重复项，内部去重）。
        provider_id: 插件配置指定的提供商 ID；为空则用会话当前聊天模型。
        umo: 会话来源 ID，用于解析会话当前聊天模型。
    """
    unique: list[str] = []
    seen: set[str] = set()
    for t in texts:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
    if not unique:
        return {}

    pid = await resolve_provider_id(context, provider_id, umo=umo)
    if not pid:
        logger.warning("[metacritic] 翻译不可用：未配置聊天模型提供商")
        return None

    try:
        resp = await context.llm_generate(
            chat_provider_id=pid,
            prompt=json.dumps(unique, ensure_ascii=False),
            system_prompt=TRANSLATE_SYSTEM_PROMPT,
        )
    except AttributeError:
        # 更老版本的 Context 没有 llm_generate
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[metacritic] LLM 翻译请求失败，保留原文: %s", e)
        return None

    translations = parse_translations(
        getattr(resp, "completion_text", "") or "", len(unique)
    )
    if translations is None:
        logger.warning("[metacritic] LLM 翻译输出无法解析，保留原文")
        return None
    return dict(zip(unique, translations))