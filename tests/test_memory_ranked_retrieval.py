"""Ranked planner memory retrieval, and how well it finds the right memory.

Planner context used to keep a memory only when every whitespace-separated
word of the task was a substring of it, in recency order. A paraphrased task,
or any Chinese task (no spaces: the whole task had to appear verbatim), found
nothing. These tests measure recall@4 and MRR on a labeled corpus of
paraphrased English and Chinese tasks and hold the ranking to a floor.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types

import pytest

from omnidesk_agent.config import AppConfig, MemoryPrivacyConfig, MemoryRetrievalConfig
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.learning.experience_extractor import ExperienceExtractor
from omnidesk_agent.memory import experience as experience_module
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


def _runtime_memory(goal: str, *, channel: str, ok: bool = True) -> dict:
    """A memory as the orchestrator writes it: ExperienceExtractor output tagged with the channel."""
    steps = [{"tool": "files", "action": "write_text", "risk": "medium"}, {"tool": "shell", "action": "run", "risk": "high"}]
    results = [{"ok": ok, "summary": "done" if ok else "", "error": None if ok else "ToolError: exit 1"}]
    run = {"status": "completed" if ok else "failed", "goal": goal, "steps": steps, "results": results}
    return ExperienceExtractor().extract(task=goal, plan={"steps": steps}, run_result=run, tags=[channel])


# Share no content word with any memory above; some are made of words every runtime memory carries.
UNRELATED = [
    "plan a trip to Kyoto",
    "bake a birthday cake for Anna",
    "订去成都的火车票",
    "给妈妈打个电话",
    "message them on telegram",  # the channel tag
    "reuse the successful plan",  # the advice on every success
    "use shell and write_text",  # tool names
    "code_or_upgrade general",  # task types
]


def test_runtime_memories_are_found_by_their_content_not_by_shared_boilerplate(store):
    # Every runtime memory carries the channel tag, the tools used and fixed advice. Matching
    # on those put unrelated memories into the planner prompt for most tasks.
    ids = {}
    for n, (key, goal) in enumerate(MEMORIES):
        ids[store.add_experience(_runtime_memory(goal, channel="telegram", ok=n % 3 != 0), channel="telegram", actor=ACTOR)] = key

    ranked = [[ids[row["id"]] for row in store.retrieve_for_task(query, limit=4, channel="telegram", actor=ACTOR)] for query, _ in QUERIES]
    recall, mrr = _metrics(ranked, [want for _query, want in QUERIES])
    assert recall >= 0.9 and mrr >= 0.8, (recall, mrr, ranked)
    for query in UNRELATED:
        assert store.retrieve_for_task(query, limit=4, channel="telegram", actor=ACTOR) == [], query


def test_fields_every_runtime_memory_shares_do_not_make_it_relevant():
    for ok in (True, False):
        row = {**_runtime_memory("Water the office plants", channel="telegram", ok=ok), "id": 1, "channel": "telegram", "updated_at": 1.0}
        boilerplate = [row["recommended_next_action"], " ".join(row["solution_attempted"]), row["task_type"], "telegram"]
        for query in boilerplate:
            assert rank_memory_rows([row], query, 4) == [], query
        assert rank_memory_rows([row], "water the plants", 4) == [row]


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


def test_a_memory_blocked_while_it_is_ranked_is_neither_returned_nor_touched(store, monkeypatch):
    # Ranking runs outside the write lock (with embeddings it can take seconds); each pick is
    # re-checked under it. A curator who blocks the memory meanwhile must win.
    blocked = store.add_experience(_experience("Rotate the API logs nightly"), channel=CHANNEL, actor=ACTOR)
    rank = experience_module.rank_memory_rows

    def rank_then_block(*args, **kwargs):
        ranked = rank(*args, **kwargs)
        store.update_memory_review(blocked, memory_status="blocked", confidence=0.0, reason="curator")
        return ranked

    monkeypatch.setattr(experience_module, "rank_memory_rows", rank_then_block)
    assert store.retrieve_for_task("rotate api logs", limit=4, channel=CHANNEL, actor=ACTOR) == []
    assert store.conn.execute("SELECT last_used_at FROM structured_experiences WHERE id=?", (blocked,)).fetchone()[0] is None


def test_a_returned_memory_carries_its_review_status_from_under_the_lock(store, monkeypatch):
    promoted = store.add_experience(_experience("Rotate the API logs nightly", memory_status="candidate"), channel=CHANNEL, actor=ACTOR)
    rank = experience_module.rank_memory_rows

    def rank_then_promote(*args, **kwargs):
        ranked = rank(*args, **kwargs)
        store.update_memory_review(promoted, memory_status="trusted", confidence=0.9, reason="curator")
        return ranked

    monkeypatch.setattr(experience_module, "rank_memory_rows", rank_then_promote)
    rows = store.retrieve_for_task("rotate api logs", limit=4, channel=CHANNEL, actor=ACTOR)
    assert [(row["id"], row["memory_status"]) for row in rows] == [(promoted, "trusted")]


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


def test_a_purged_memory_takes_its_cached_embedding_with_it(tmp_path):
    with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
        memory.configure_retrieval(MemoryRetrievalConfig(embeddings_enabled=True), embedder=FakeEmbedder())
        expiring = memory.add_experience(_experience("Get the car repaired", expires_at=time.time() + 3600), channel=CHANNEL, actor=ACTOR)
        kept = memory.add_experience(_experience("Wash the car"), channel=CHANNEL, actor=ACTOR)
        memory.retrieve_for_task("fix my car", limit=2, channel=CHANNEL, actor=ACTOR)
        cached = lambda: sorted(r[0] for r in memory.conn.execute("SELECT experience_id FROM structured_experience_embeddings"))  # noqa: E731
        assert cached() == sorted([expiring, kept])

        memory.purge_expired(dry_run=False, now=time.time() + 7200)
        assert cached() == [kept]  # derived from the memory text: it must not outlive the memory


def test_embeddings_need_an_embedder():
    with pytest.raises(ValueError):
        ExperienceStore.configure_retrieval(object.__new__(ExperienceStore), MemoryRetrievalConfig(embeddings_enabled=True))  # type: ignore[arg-type]


COMMIT = "0123456789abcdef0123456789abcdef01234567"


class RecordingSentenceTransformer:
    calls: list = []

    def __init__(self, *args, **kwargs):
        type(self).calls.append((args, kwargs, os.environ.get("HF_HUB_OFFLINE")))

    def encode(self, texts, normalize_embeddings=True):
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def sentence_transformers_stub(monkeypatch):
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = RecordingSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(RecordingSentenceTransformer, "calls", [])
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    return RecordingSentenceTransformer


def test_the_embedding_model_loads_from_local_files_only_at_a_pinned_commit(sentence_transformers_stub):
    # The daemon must not download a model, nor silently move to a new upstream revision.
    embedder = SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2", revision=COMMIT)
    assert sentence_transformers_stub.calls == [(("sentence-transformers/all-MiniLM-L6-v2",), {"revision": COMMIT, "local_files_only": True}, "1")]
    # Cached vectors are keyed by model and revision: another revision is another vector space.
    assert embedder.model_name == f"sentence-transformers/all-MiniLM-L6-v2@{COMMIT}"


@pytest.mark.parametrize("revision", ["", "main", "v1.0", COMMIT[:12]])
def test_a_hub_model_without_a_pinned_commit_is_refused(sentence_transformers_stub, revision):
    with pytest.raises(ValueError, match="embedding_revision"):
        SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2", revision=revision)
    assert sentence_transformers_stub.calls == []


def test_a_local_model_directory_needs_no_revision(sentence_transformers_stub, tmp_path):
    embedder = SentenceTransformerEmbedder(str(tmp_path))
    assert sentence_transformers_stub.calls == [((str(tmp_path),), {"revision": None, "local_files_only": True}, "1")]
    assert embedder.model_name == str(tmp_path)


def test_a_model_missing_from_the_local_cache_fails_with_a_fetch_hint(sentence_transformers_stub, monkeypatch):
    def missing(self, *args, **kwargs):
        raise OSError("not in the local cache")

    monkeypatch.setattr(sentence_transformers_stub, "__init__", missing)
    with pytest.raises(RuntimeError, match="not available locally"):
        SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2", revision=COMMIT)


def test_without_the_vector_extra_the_error_names_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # import fails, installed or not
    with pytest.raises(RuntimeError, match="vector"):
        SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2", revision=COMMIT)


def _runtime(cfg: AppConfig, memory) -> OmniDeskRuntime:
    runtime = object.__new__(OmniDeskRuntime)
    runtime.cfg = cfg
    runtime.memory = memory
    return runtime


def test_in_offline_mode_the_daemon_configures_embeddings_without_network(sentence_transformers_stub, tmp_path):
    cfg = AppConfig.model_validate({
        "runtime": {"offline_mode": True, "deny_external_services_in_offline_mode": True, "offline_cache_root": str(tmp_path / "offline-cache")},
        "memory_retrieval": {"embeddings_enabled": True, "embedding_revision": COMMIT, "candidate_window": 50},
    })
    cfg.workspace.root = tmp_path
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    cfg.ensure_dirs()
    runtime = OmniDeskRuntime(cfg)
    try:
        assert runtime.memory.retrieval.candidate_window == 50 and runtime.memory.embedder is not None
    finally:
        runtime.close()
    [(_args, kwargs, hub_offline)] = sentence_transformers_stub.calls
    assert kwargs["local_files_only"] is True and hub_offline == "1"


def test_embeddings_are_refused_on_a_store_that_cannot_rank_with_them():
    off, on = AppConfig(), AppConfig.model_validate({"memory_retrieval": {"embeddings_enabled": True, "embedding_revision": COMMIT}})
    _runtime(off, object())._configure_memory_retrieval()  # nothing to configure
    with pytest.raises(RuntimeError, match="SQLite memory store only"):
        _runtime(on, object())._configure_memory_retrieval()


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
