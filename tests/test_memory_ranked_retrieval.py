"""Ranked planner memory retrieval, and how well it finds the right memory.

Planner context used to keep a memory only when every whitespace-separated
word of the task was a substring of it, in recency order. A paraphrased task,
or any Chinese task (no spaces: the whole task had to appear verbatim), found
nothing. These tests measure recall@4 and MRR on a labeled corpus of
paraphrased English and Chinese tasks and hold the ranking to a floor.
"""
from __future__ import annotations

import json
import time

import pytest

from omnidesk_agent.config import MemoryPrivacyConfig, MemoryRetrievalConfig
from omnidesk_agent.memory.embeddings import SentenceTransformerEmbedder, cosine
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.memory.retrieval import bm25_scores, matches_memory_query, rank_memory_rows, tokenize

CHANNEL, ACTOR = "local-cli", "owner"

# (key, goal) of the memories the queries should find, plus distractors.
MEMORIES = [
    ("sales", "Summarize the weekly sales report in reports/sales.xlsx and email it to the finance team"),
    ("complaint", "Reply to the customer complaint about a late delivery on WhatsApp"),
    ("rename", "Rename all screenshots in the downloads folder by date"),
    ("meeting", "Book a meeting room for Friday's planning session in the calendar"),
    ("tests", "Run the unit tests and fix the failing login test"),
    ("translate", "Translate the product description into Japanese"),
    ("backup", "Back up the notes directory to the external drive"),
    ("invoice", "Check the invoice total in invoice_2041.pdf against the purchase order"),
    ("slack", "Post the release announcement to the team Slack channel"),
    ("logs", "Clean up old log files in the logs folder"),
    ("zh_sales", "把本周销售报表汇总后发给财务部"),
    ("zh_refund", "回复微信上关于退款的客户消息"),
    ("zh_rename", "整理下载文件夹里的截图并按日期命名"),
    ("zh_meeting", "在日历里预订周五的会议室"),
    ("zh_tests", "运行单元测试并修复登录失败的用例"),
    ("zh_translate", "把产品说明翻译成日语"),
    ("zh_backup", "备份笔记目录到移动硬盘"),
    ("zh_invoice", "检查发票金额是否和采购单一致"),
    ("zh_release", "在钉钉群里发布版本更新公告"),
    ("zh_logs", "清理日志文件夹里的旧日志"),
    ("d1", "Open the browser and check the weather forecast"),
    ("d2", "Take a screenshot of the dashboard"),
    ("d3", "Write a haiku about autumn leaves"),
    ("d4", "List the files in the workspace"),
    ("d5", "Draft a polite out-of-office reply"),
    ("d6", "查看今天的天气预报"),
    ("d7", "截一张仪表盘的图"),
    ("d8", "写一首关于秋天的诗"),
    ("d9", "列出工作区里的文件"),
    ("d10", "起草一封礼貌的休假自动回复"),
]

# Paraphrases: none repeats its memory verbatim.
QUERIES = [
    ("email the finance team a summary of this week's sales", "sales"),
    ("answer the WhatsApp customer who complained the parcel arrived late", "complaint"),
    ("rename my downloaded screenshots using their dates", "rename"),
    ("reserve a room for the Friday planning meeting", "meeting"),
    ("fix the broken login unit test", "tests"),
    ("translate the product text to Japanese", "translate"),
    ("verify invoice 2041's total matches the purchase order", "invoice"),
    ("announce the new release in Slack", "slack"),
    ("给财务发送销售汇总", "zh_sales"),
    ("微信上的退款消息怎么回复客户", "zh_refund"),
    ("预订周五会议室", "zh_meeting"),
    ("修复登录单元测试", "zh_tests"),
    ("核对发票和采购单的金额", "zh_invoice"),
    ("把笔记备份到硬盘", "zh_backup"),
    ("清理旧日志", "zh_logs"),
]


def _experience(goal: str, **extra) -> dict:
    return {"task_type": "general", "goal": goal, "success": True, "risk_level": "low", "memory_status": "validated", "tags": [CHANNEL], **extra}


@pytest.fixture
def store(tmp_path):
    with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
        yield memory


def _load(store: ExperienceStore, memories=MEMORIES) -> dict[int, str]:
    ids: dict[int, str] = {}
    for key, goal in memories:
        ids[store.add_experience(_experience(goal), channel=CHANNEL, actor=ACTOR)] = key
    return ids


def _metrics(ranked_keys: list[list[str]], relevant: list[str], k: int = 4) -> tuple[float, float]:
    hits = sum(1 for keys, want in zip(ranked_keys, relevant) if want in keys[:k])
    reciprocal = sum(1.0 / (keys.index(want) + 1) for keys, want in zip(ranked_keys, relevant) if want in keys)
    return hits / len(relevant), reciprocal / len(relevant)


def _old_retrieval(store: ExperienceStore, query: str, limit: int = 4) -> list[dict]:
    # The previous planner retrieval: newest first, every query word must be a substring.
    rows = store.list_structured(days=1, limit=1000)
    return [row for row in rows if matches_memory_query(row, query)][:limit]


def test_paraphrased_english_and_chinese_tasks_find_their_memory(store):
    ids = _load(store)
    relevant = [want for _query, want in QUERIES]

    ranked = [[ids[row["id"]] for row in store.retrieve_for_task(query, limit=4, channel=CHANNEL, actor=ACTOR)] for query, _ in QUERIES]
    recall, mrr = _metrics(ranked, relevant)
    baseline = [[ids[row["id"]] for row in _old_retrieval(store, query)] for query, _ in QUERIES]
    old_recall, old_mrr = _metrics(baseline, relevant)

    # Floors for this corpus; the measured values are in the PR evidence.
    assert recall >= 0.9 and mrr >= 0.8, (recall, mrr, ranked)
    assert old_recall <= 0.2 and recall - old_recall >= 0.7, (old_recall, old_mrr)


def test_ranking_puts_the_closest_memory_first_not_the_newest(store):
    store.add_experience(_experience("Email the finance team the quarterly sales summary"), channel=CHANNEL, actor=ACTOR)
    time.sleep(0.01)
    for goal in ("Email the design team the logo files", "Email my landlord about the heating"):
        store.add_experience(_experience(goal), channel=CHANNEL, actor=ACTOR)

    rows = store.retrieve_for_task("send the finance team a sales summary by email", limit=3, channel=CHANNEL, actor=ACTOR)
    assert rows[0]["goal"] == "Email the finance team the quarterly sales summary"


def test_an_old_relevant_memory_outside_the_recency_window_is_found_through_full_text(store):
    store.configure_retrieval(MemoryRetrievalConfig(candidate_window=20, fts_candidates=50))
    old = store.add_experience(_experience("Reconcile the vendor invoice against the purchase order"), channel=CHANNEL, actor=ACTOR)
    for i in range(60):
        store.add_experience(_experience(f"Water the office plants, round {i}"), channel=CHANNEL, actor=ACTOR)

    rows = store.retrieve_for_task("reconcile this invoice with its purchase order", limit=4, channel=CHANNEL, actor=ACTOR)
    assert [row["id"] for row in rows][:1] == [old]


def test_ranking_never_crosses_the_actor_or_channel_boundary(store):
    store.add_experience(_experience("Summarize the weekly sales report for finance"), channel=CHANNEL, actor="bob")
    store.add_experience(_experience("Summarize the weekly sales report for finance"), channel="telegram", actor=ACTOR)
    mine = store.add_experience(_experience("Water the plants"), channel=CHANNEL, actor=ACTOR)

    assert store.retrieve_for_task("summarize the weekly sales report", limit=4, channel=CHANNEL, actor=ACTOR) == []
    assert [r["id"] for r in store.retrieve_for_task("water plants", limit=4, channel=CHANNEL, actor=ACTOR)] == [mine]


def test_retired_and_expired_memories_are_not_ranked(store):
    active = store.add_experience(_experience("Rotate the API logs nightly"), channel=CHANNEL, actor=ACTOR)
    retired = store.add_experience(_experience("Rotate the API logs nightly"), channel=CHANNEL, actor=ACTOR)
    store.update_memory_review(retired, memory_status="deprecated", confidence=0.1, reason="wrong")
    store.add_experience(_experience("Rotate the API logs nightly", expires_at=time.time() - 1), channel=CHANNEL, actor=ACTOR)

    assert [r["id"] for r in store.retrieve_for_task("rotate api logs", limit=4, channel=CHANNEL, actor=ACTOR)] == [active]


def test_retrieval_marks_only_returned_memories_as_used(store):
    hit = store.add_experience(_experience("Archive last month's invoices"), channel=CHANNEL, actor=ACTOR)
    miss = store.add_experience(_experience("Water the plants"), channel=CHANNEL, actor=ACTOR)
    store.retrieve_for_task("archive the invoices", limit=4, channel=CHANNEL, actor=ACTOR)
    used = {row["id"]: row["last_used_at"] for row in store.list_structured(days=1)}
    assert used[hit] is not None and used[miss] is None


def test_encrypted_memory_is_still_ranked(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("OMNIDESK_MEMORY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with ExperienceStore(tmp_path / "enc.sqlite3", MemoryPrivacyConfig(encrypt_at_rest=True)) as memory:
        ids = _load(memory)
        rows = memory.retrieve_for_task("给财务发送销售汇总", limit=4, channel=CHANNEL, actor=ACTOR)
    assert rows and ids[rows[0]["id"]] == "zh_sales"


class FakeEmbedder:
    """Maps words to concept axes, so 'automobile' and 'car' are close without sharing a word."""

    model_name = "fake-concepts"
    AXES = {"car": 0, "automobile": 0, "vehicle": 0, "repair": 1, "fix": 1, "mechanic": 1, "bread": 2, "bake": 2}

    def __init__(self):
        self.calls: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        vectors = []
        for text in texts:
            vector = [0.0, 0.0, 0.0, 0.01]
            for word in text.lower().replace(".", " ").split():
                if word in self.AXES:
                    vector[self.AXES[word]] += 1.0
            vectors.append(vector)
        return vectors


def test_optional_embeddings_find_a_memory_without_a_shared_word_and_are_cached(tmp_path):
    embedder = FakeEmbedder()
    with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
        memory.configure_retrieval(MemoryRetrievalConfig(embeddings_enabled=True, embedding_weight=0.6), embedder=embedder)
        target = memory.add_experience(_experience("Get the automobile repaired by a mechanic"), channel=CHANNEL, actor=ACTOR)
        memory.add_experience(_experience("Bake bread for the party"), channel=CHANNEL, actor=ACTOR)

        first = memory.retrieve_for_task("fix my car", limit=2, channel=CHANNEL, actor=ACTOR)
        second = memory.retrieve_for_task("fix my car", limit=2, channel=CHANNEL, actor=ACTOR)

        stored = memory.conn.execute("SELECT payload FROM structured_experience_embeddings").fetchall()

    assert [r["id"] for r in first] == [target] == [r["id"] for r in second]
    # Two memory vectors embedded once and reused; one query vector per retrieval.
    assert embedder.calls == [2, 1, 1]
    assert len(stored) == 2 and json.loads(stored[0][0])["sha256"]


def test_embedding_cache_is_encrypted_with_the_memory(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("OMNIDESK_MEMORY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with ExperienceStore(tmp_path / "enc.sqlite3", MemoryPrivacyConfig(encrypt_at_rest=True)) as memory:
        memory.configure_retrieval(MemoryRetrievalConfig(embeddings_enabled=True), embedder=FakeEmbedder())
        memory.add_experience(_experience("Get the car repaired"), channel=CHANNEL, actor=ACTOR)
        memory.retrieve_for_task("fix my car", limit=1, channel=CHANNEL, actor=ACTOR)
        payload = memory.conn.execute("SELECT payload FROM structured_experience_embeddings").fetchone()[0]
    assert "vector" not in payload and "sha256" not in payload


def test_embeddings_need_an_embedder_and_the_extra():
    with pytest.raises(ValueError):
        ExperienceStore.configure_retrieval(object.__new__(ExperienceStore), MemoryRetrievalConfig(embeddings_enabled=True))  # type: ignore[arg-type]
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="vector"):
            SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2")


def test_tokenizer_handles_cjk_runs_stems_and_stopwords():
    assert tokenize("回复微信") == ["回复", "复微", "微信"]
    assert tokenize("Reports and files, testing") == ["report", "file", "test"]
    assert tokenize("the a of") == []
    assert tokenize("invoice_2041.pdf 2041") == ["invoice_2041", "pdf", "2041"]


def test_bm25_prefers_rarer_terms_and_shorter_documents():
    docs = [tokenize("invoice report report report"), tokenize("invoice"), tokenize("weather report")]
    scores = bm25_scores(docs, tokenize("invoice"))
    assert scores[1] > scores[0] > 0 and scores[2] == 0


def test_rank_requires_a_match_unless_semantics_say_otherwise():
    rows = [{"id": 1, "goal": "bake bread", "updated_at": 1}, {"id": 2, "goal": "car repair", "updated_at": 2}]
    assert rank_memory_rows(rows, "fix the vehicle", 2) == []
    ranked = rank_memory_rows(rows, "fix the vehicle", 2, semantic_scores=[0.0, 0.9], semantic_weight=0.5)
    assert [r["id"] for r in ranked] == [2]
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0) and cosine([], [1.0]) == 0.0
