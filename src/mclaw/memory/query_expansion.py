"""查询扩展 —— FTS5 下的同义词/缩略语代偿

未安装 ``vector-memory`` 可选模块（约 2.5GB）时，知识库只有 BM25 关键词检索：
用户问"等保"，文档里写"等级保护测评"，就一条都搜不到。这在垂直领域尤其明显
——术语高度稳定，同义写法却很多（等保/等级保护、密评/商用密码应用安全性评估、
HVV/护网、供方/供应商…）。

本模块用零依赖的方式补上这块：命中同义词组里的任一词，就把同组其他词一起
加进 FTS5 的 OR 查询。

词表来源（后者追加到前者）：
1. 内置 ``kb_synonyms.json``（通用网络安全/招投标术语）
2. 用户 ``<data_dir>/knowledge/synonyms.json``（客户自己的项目名、产品别名）

注意：同义词是双刃剑——组越大召回越宽、精度越低。内置表刻意保守，只收真正
等价的写法；扩表时也别把"相关词"（如 预算 / 最高限价）当同义词塞进来。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_BUILTIN_PATH = Path(__file__).with_name("kb_synonyms.json")

# 扩展词上限：防止一个大组把 FTS5 查询撑成几十个 OR 项，反而淹没原查询
DEFAULT_MAX_EXTRA = 12

# 至少含一个汉字/字母才算有效检索词（与 knowledge_manager 的分块过滤同源）
_MEANINGFUL_RE = re.compile(r"[0-9A-Za-z一-鿿]")


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"[QueryExpansion] 同义词表解析失败，已忽略 {path}: {e}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"[QueryExpansion] 同义词表格式应为对象，已忽略 {path}")
        return {}

    out: dict[str, list[str]] = {}
    for key, val in data.items():
        if key.startswith("_"):  # _comment 之类的说明字段
            continue
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list):
            continue
        terms = [str(t).strip() for t in val if str(t).strip()]
        if key.strip() and terms:
            out[key.strip()] = terms
    return out


def _merge_groups(mapping: dict[str, list[str]]) -> list[list[str]]:
    """把 词→同义词 的映射合并成等价组（有交集就并起来）。

    这样 "等保": ["等级保护"] 和 "等级保护": ["等保2.0"] 会并成同一组，
    避免同一个等价类散成多条、扩展不全。
    """
    groups: list[set[str]] = []
    for key, values in mapping.items():
        members = {key, *values}
        merged: set[str] = set()
        remaining: list[set[str]] = []
        for g in groups:
            if g & members:
                merged |= g
            else:
                remaining.append(g)
        remaining.append(merged | members)
        groups = remaining
    return [sorted(g) for g in groups]


def load_synonym_groups(user_path: Path | None = None) -> list[list[str]]:
    """加载等价词组；内置表 + 用户表。

    用户表是**追加**语义（同名 key 的词会被合并，而不是覆盖内置词）：
    用户加自己项目名/产品别名时，不该静默丢掉内置的通用同义词——那等于
    悄悄降低了召回，而且没有任何提示。
    """
    mapping = _load_json(_BUILTIN_PATH)
    if user_path is not None:
        user_map = _load_json(Path(user_path))
        if user_map:
            logger.info(f"[QueryExpansion] 加载用户同义词表: {user_path} ({len(user_map)} 组)")
        for key, values in user_map.items():
            existing = mapping.get(key, [])
            mapping[key] = existing + [v for v in values if v not in existing]
    return _merge_groups(mapping)


def expand_query(query: str, groups: list[list[str]], max_extra: int = DEFAULT_MAX_EXTRA) -> list[str]:
    """返回应追加到检索查询里的等价词（不含查询本身已有的词）。

    直接在**原查询字符串**里找等价词，而不是先分词再找：像"等级保护测评"
    这类词会被 jieba 切成多段，用分词结果匹配会漏。
    """
    if not query or not groups:
        return []

    extra: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if not any(term in query for term in group):
            continue
        for term in group:
            if term in query or term in seen or not _MEANINGFUL_RE.search(term):
                continue
            seen.add(term)
            extra.append(term)
            if len(extra) >= max_extra:
                return extra
    return extra
