"""游戏别名持久化存储。

别名来源有三层，查询优先级从高到低：

1. **用户别名库**（本模块）：用户通过 ``/评分别名`` 命令或 WebUI 管理页面
   添加/删除的别名，以及 LLM 识别成功后自动写入的别名。持久化到 JSON 文件，
   位于 ``data/plugin_data/<plugin_name>/aliases.json``，不会随插件更新丢失。
2. **内置别名表**：写在 ``main.py`` 的 ``ALIASES`` 常量里，作为开箱即用的词典。
3. **LLM 识别**：前两者都未命中时调用大模型，识别成功后可自动回写库中。

JSON 结构::

    {
      "version": 1,
      "entries": {
        "<规范化别名 key>": {
          "alias": "老头环",          // 原始别名（展示用）
          "title": "Elden Ring",      // Metacritic 英文官方名
          "source": "llm",            // builtin | user | llm
          "created_at": 1730000000.0, // 首次写入时间
          "updated_at": 1730000000.0, // 最后更新时间
          "hits": 3                   // 命中次数
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# 别名来源
SOURCE_BUILTIN = "builtin"
SOURCE_USER = "user"
SOURCE_LLM = "llm"

_MAX_ALIAS_LEN = 120
_MAX_TITLE_LEN = 160
_MAX_ENTRIES = 5000


def normalize_key(text: str) -> str:
    """把输入规范化为匹配用的 key：去空白、转小写。

    例如 ``"  Elden  Ring "`` 与 ``"elden ring"`` 得到同一个 key，
    ``"老头 环"`` 与 ``"老头环"`` 也相同。
    """
    if not text:
        return ""
    return re.sub(r"\s+", "", str(text)).strip().lower()


def clean_alias(alias: str) -> str:
    """清洗别名：去首尾空白与包裹引号，限制长度。"""
    a = (alias or "").strip().strip("\"'“”‘’")
    a = re.sub(r"\s+", " ", a).strip()
    return a[:_MAX_ALIAS_LEN]


def clean_title(title: str) -> str:
    """清洗官方名：去首尾空白与引号，保留内部标点（如 ``: ``），限制长度。"""
    t = (title or "").strip().strip("\"'“”‘’")
    t = t.strip().strip(".")
    return t[:_MAX_TITLE_LEN]


class AliasStore:
    """别名词典的持久化容器（线程安全，异步写入由调用方保证顺序）。"""

    def __init__(self, data_dir: str | Path, filename: str = "aliases.json"):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / filename
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------------ #
    # 读写磁盘
    # ------------------------------------------------------------------ #
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._entries = self._read_file()
            self._loaded = True

    def _read_file(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        # 逐条校验，丢弃损坏项而不是整个文件失败
        valid: dict[str, dict[str, Any]] = {}
        for key, item in entries.items():
            if not isinstance(item, dict):
                continue
            alias = clean_alias(str(item.get("alias") or key))
            title = clean_title(str(item.get("title") or ""))
            if not alias or not title:
                continue
            source = str(item.get("source") or SOURCE_USER)
            if source not in (SOURCE_BUILTIN, SOURCE_USER, SOURCE_LLM):
                source = SOURCE_USER
            valid[normalize_key(key) or normalize_key(alias)] = {
                "alias": alias,
                "title": title,
                "source": source,
                "created_at": _as_float(item.get("created_at")) or 0.0,
                "updated_at": _as_float(item.get("updated_at")) or 0.0,
                "hits": int(_as_float(item.get("hits")) or 0),
            }
        return valid

    def save(self) -> None:
        """原子写入磁盘（先写临时文件再替换，避免写一半损坏）。"""
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": time.time(),
                "entries": self._entries,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.data_dir), prefix=".aliases-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def lookup(self, text: str) -> dict[str, Any] | None:
        """精确匹配（忽略空白大小写）。返回条目副本含 ``title``。"""
        self._ensure_loaded()
        key = normalize_key(text)
        if not key:
            return None
        with self._lock:
            item = self._entries.get(key)
            return dict(item) if item else None

    def lookup_contains(self, text: str) -> dict[str, Any] | None:
        """包含匹配：输入里含有某个别名（长别名优先，最多少于 2 字的别名不参与）。

        用于 ``/评分 我想看老头环的评分`` 这类带多余文字的输入。
        """
        self._ensure_loaded()
        key = normalize_key(text)
        if not key:
            return None
        with self._lock:
            candidates = [
                (len(k), k, v)
                for k, v in self._entries.items()
                if len(k) >= 2 and k in key
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda x: x[0], reverse=True)
            return dict(candidates[0][2])

    def search(self, text: str, limit: int = 50) -> list[dict[str, Any]]:
        """按关键字模糊搜索（别名或官方名包含即可），按更新时间倒序。"""
        self._ensure_loaded()
        needle = normalize_key(text)
        with self._lock:
            items = []
            for key, item in self._entries.items():
                if needle and needle not in key and needle not in normalize_key(
                    item["title"]
                ):
                    continue
                items.append({"key": key, **item})
        items.sort(key=lambda x: (x.get("updated_at") or 0, x.get("hits") or 0), reverse=True)
        return items[: max(1, min(limit, _MAX_ENTRIES))]

    def all_entries(self) -> list[dict[str, Any]]:
        return self.search("", limit=_MAX_ENTRIES)

    def count(self) -> int:
        self._ensure_loaded()
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict[str, int]:
        """按来源统计数量，供 WebUI 顶部展示。"""
        self._ensure_loaded()
        with self._lock:
            out = {SOURCE_BUILTIN: 0, SOURCE_USER: 0, SOURCE_LLM: 0}
            for item in self._entries.values():
                src = item.get("source") or SOURCE_USER
                out[src] = out.get(src, 0) + 1
            out["total"] = len(self._entries)
            return out

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def upsert(
        self,
        alias: str,
        title: str,
        source: str = SOURCE_USER,
        overwrite: bool = True,
    ) -> tuple[bool, dict[str, Any] | None]:
        """新增或更新别名。

        Returns:
            ``(changed, entry)``。``changed`` 为 False 表示已存在相同内容且未变更；
            参数非法时返回 ``(False, None)``。
        """
        a = clean_alias(alias)
        t = clean_title(title)
        if not a or not t:
            return False, None
        if source not in (SOURCE_BUILTIN, SOURCE_USER, SOURCE_LLM):
            source = SOURCE_USER
        key = normalize_key(a)
        if not key:
            return False, None

        self._ensure_loaded()
        now = time.time()
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                if not overwrite and existing["title"] != t:
                    return False, dict(existing)
                if existing["title"] == t and existing["source"] == source:
                    # 内容没变，只更新时间，避免无意义写盘
                    existing["updated_at"] = now
                    return False, dict(existing)
                existing["title"] = t
                existing["source"] = source
                existing["alias"] = a
                existing["updated_at"] = now
                entry = dict(existing)
            else:
                if len(self._entries) >= _MAX_ENTRIES:
                    return False, None
                entry = {
                    "alias": a,
                    "title": t,
                    "source": source,
                    "created_at": now,
                    "updated_at": now,
                    "hits": 0,
                }
                self._entries[key] = entry
            self.save()
            return True, dict(entry)

    def delete(self, alias: str) -> bool:
        """按别名删除一条记录，返回是否真的删除。"""
        key = normalize_key(alias)
        if not key:
            return False
        self._ensure_loaded()
        with self._lock:
            if key not in self._entries:
                return False
            del self._entries[key]
            self.save()
            return True

    def delete_many(self, aliases: list[str]) -> int:
        """批量删除，返回删除条数。"""
        keys = [normalize_key(a) for a in aliases]
        keys = [k for k in keys if k]
        if not keys:
            return 0
        self._ensure_loaded()
        with self._lock:
            removed = 0
            for k in keys:
                if k in self._entries:
                    del self._entries[k]
                    removed += 1
            if removed:
                self.save()
            return removed

    def record_hit(self, alias: str) -> None:
        """记录一次命中（用于「最常用」排序），每 10 次才落盘以降低 IO。"""
        key = normalize_key(alias)
        if not key:
            return
        self._ensure_loaded()
        with self._lock:
            item = self._entries.get(key)
            if not item:
                return
            item["hits"] = int(item.get("hits") or 0) + 1
            if item["hits"] % 10 == 0:
                self.save()

    def clear(self, source: str | None = None) -> int:
        """清空别名。``source`` 为 None 时全部清空，否则只清指定来源。"""
        self._ensure_loaded()
        with self._lock:
            if source is None:
                removed = len(self._entries)
                self._entries.clear()
            else:
                keys = [k for k, v in self._entries.items() if v.get("source") == source]
                removed = len(keys)
                for k in keys:
                    del self._entries[k]
            if removed:
                self.save()
            return removed

    def import_builtin(self, mapping: dict[str, str], overwrite: bool = False) -> int:
        """把内置别名表导入库中（``/评分别名 导入内置``），返回新增条数。"""
        added = 0
        for alias, title in mapping.items():
            a = clean_alias(alias)
            t = clean_title(title)
            if not a or not t:
                continue
            key = normalize_key(a)
            if not key:
                continue
            if not overwrite and self.lookup(a) is not None:
                continue
            changed, _ = self.upsert(a, t, SOURCE_BUILTIN, overwrite=True)
            if changed:
                added += 1
        return added


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def default_store_dir(plugin_name: str = "astrbot_plugin_metacritic_game_reader") -> Path:
    """返回推荐的别名库目录：``data/plugin_data/<plugin_name>/``。

    AstrBot 环境不可用（例如本地测试）时回退到插件目录下的 ``.data/``。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path()) / plugin_name
    except Exception:  # noqa: BLE001 - 非 AstrBot 环境
        return Path(__file__).resolve().parent / ".data"
