"""Authorization and review eligibility for task-planner memory context."""
from __future__ import annotations

import math
import re
from typing import Any, Optional


def known_memory_identity(channel: str | None, actor: str | None) -> bool:
    return all(isinstance(value, str) and value.strip() and value != "unknown" for value in (channel, actor))


def planner_memory_visible(row: dict[str, Any], *, channel: str, actor: str, now: float) -> bool:
    # Never infer ownership from tags, namespace strings or the requesting actor.
    if not known_memory_identity(channel, actor) or row.get("channel") != channel or row.get("actor") != actor:
        return False
    if row.get("memory_status") not in {"candidate", "validated", "trusted"}:
        return False
    expiry = row.get("expires_at")
    if expiry is None:
        return True
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return False
    return math.isfinite(expiry) and expiry > now


def matches_memory_query(row: dict[str, Any], query: str) -> bool:
    # Match literal tokens, including punctuation, without executing FTS syntax.
    terms = query.casefold().split()
    text = " ".join(str(row.get(field) or "") for field in (
        "goal", "failure_reason", "recommended_next_action", "tags",
    )).casefold()
    return bool(terms) and all(term in text for term in terms)


# --------------------------------------------------------------------------- ranked retrieval
#
# Planner memory is ranked with BM25 over the memory's text instead of keeping
# only rows that contain every task word. Chinese, Japanese and Korean text has
# no spaces, so runs of those characters are indexed as character bigrams;
# other scripts as lightly stemmed words. Optional embedding scores (cosine
# similarity, computed by the store) are blended in when configured.

_CJK_RUN_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]+")
_WORD_RE = re.compile(r"\w+")
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are", "be", "it", "this",
    "that", "my", "me", "please", "from", "at", "by", "as", "into", "then", "can", "you", "i", "we",
})
_STATUS_RANK = {"trusted": 2, "validated": 1, "candidate": 0}


def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Search terms of `text`: CJK character bigrams plus stemmed words of other scripts."""
    folded = str(text or "").casefold()
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(folded):
        tokens.extend([run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)])
    for word in _WORD_RE.findall(_CJK_RUN_RE.sub(" ", folded)):
        if word in _STOPWORDS or word == "_" * len(word):
            continue
        if len(word) == 1 and not word.isdigit():
            continue
        tokens.append(_stem(word))
    return tokens


def memory_document(row: dict[str, Any]) -> str:
    """The text a memory is matched on. The goal counts twice: it says what the task was."""
    def text(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            return " ".join(str(item) for item in value)
        return str(value or "")

    goal = text(row.get("goal"))
    return " ".join([goal, goal, text(row.get("task_type")), text(row.get("solution_attempted")), text(row.get("recommended_next_action")), text(row.get("failure_reason")), text(row.get("tags"))])


def bm25_scores(documents: list[list[str]], query: list[str], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    if not documents:
        return []
    terms = set(query)
    count = len(documents)
    average = (sum(len(doc) for doc in documents) / count) or 1.0
    frequency: dict[str, int] = {}
    for doc in documents:
        for term in set(doc) & terms:
            frequency[term] = frequency.get(term, 0) + 1
    idf = {term: math.log(1.0 + (count - df + 0.5) / (df + 0.5)) for term, df in frequency.items()}
    scores = []
    for doc in documents:
        counts: dict[str, int] = {}
        for term in doc:
            if term in idf:
                counts[term] = counts.get(term, 0) + 1
        norm = k1 * (1.0 - b + b * len(doc) / average)
        scores.append(sum(idf[t] * c * (k1 + 1.0) / (c + norm) for t, c in counts.items()))
    return scores


def rank_memory_rows(
    rows: list[dict[str, Any]],
    query: str,
    limit: int,
    *,
    semantic_scores: Optional[list[float]] = None,
    semantic_weight: float = 0.0,
    min_semantic: float = 0.35,
) -> list[dict[str, Any]]:
    """Best `limit` rows for `query`, most relevant first.

    A row needs at least one lexical match, or, with semantic scores, a cosine
    similarity of at least `min_semantic`. Ties go to reviewed memories, then
    to the most recently updated.
    """
    if not rows or limit <= 0:
        return []
    query_terms = tokenize(query)
    lexical = bm25_scores([tokenize(memory_document(row)) for row in rows], query_terms)
    top = max(lexical) if lexical else 0.0
    weight = min(1.0, max(0.0, float(semantic_weight))) if semantic_scores is not None else 0.0
    scored: list[tuple[float, int, float, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        semantic = float(semantic_scores[index]) if semantic_scores is not None else 0.0
        if lexical[index] <= 0 and not (semantic_scores is not None and semantic >= min_semantic):
            continue
        normalized = lexical[index] / top if top > 0 else 0.0
        score = (1.0 - weight) * normalized + weight * max(0.0, semantic)
        scored.append((score, _STATUS_RANK.get(str(row.get("memory_status")), 0), float(row.get("updated_at") or 0.0), row))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [row for *_rest, row in scored[:limit]]
