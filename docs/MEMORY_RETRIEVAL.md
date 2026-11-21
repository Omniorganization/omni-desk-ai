# Planner memory retrieval

`ExperienceStore.retrieve_for_task(task, limit, channel=, actor=)` (SQLite) gives the planner the memories of the
same channel and actor that are most relevant to the task. Visibility rules are unchanged: only `candidate`,
`validated` or `trusted` memories that have not expired, never another channel's or actor's.

Ranking (`memory/retrieval.py`):

- Candidates: the actor's `memory_retrieval.candidate_window` (500) most recently updated memories, plus up to
  `fts_candidates` (200) older ones the FTS5 index matches (prefix match on ASCII words; skipped when memory is
  encrypted at rest, because the index then holds ciphertext).
- Text: the goal (counted twice), the failure reason and the memory's own tags. Fields that every runtime memory
  shares are left out: the channel tag, the task type, the tool names and the fixed next-action advice ("reuse
  successful plan"). Matched on, they made unrelated memories relevant to most tasks.
- Terms: runs of Chinese, Japanese or Korean characters become character bigrams; other text becomes lightly
  stemmed words, minus a short English stopword list.
- Score: BM25 (k1 1.5, b 0.75). A memory needs at least one matching term. Ties go to reviewed memories, then to the
  most recently updated.
- Optional embeddings (`embeddings_enabled`, off by default; `pip install 'omnidesk-agent[vector]'`): a
  sentence-transformers model loaded from local files (see below); the score becomes
  `(1 - embedding_weight) * bm25/max + embedding_weight * cosine`, and a memory with no shared term qualifies at
  cosine >= `min_embedding_similarity`. One vector per memory and model version is cached in
  `structured_experience_embeddings`, encrypted like the memory text, recomputed when the text changes, and deleted
  with the memory on retention purge.

Ranking runs outside the write lock. Each pick is then re-checked under it (`BEGIN IMMEDIATE`): a memory a curator
blocked or deprecated meanwhile is dropped, and the row returned is the current one. Each returned memory gets
`last_used_at` set.

## Embedding model: local files only, no network

The daemon never downloads a model. It loads `embedding_model` with `local_files_only=True` and sets
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` (unless already set) before importing sentence-transformers, so it
makes no network request for embeddings, with or without `runtime.offline_mode`. Nothing changes for the reference
Kubernetes NetworkPolicy, which allows egress only to the sandbox runner.

Fetching the model is a separate operator step, and the only network access the feature needs. Run it where that
access is allowed, then make the files available to the daemon:

- A Hugging Face id (the default `sentence-transformers/all-MiniLM-L6-v2`) needs `embedding_revision` set to the
  commit you fetched, 40 hex characters, for example after
  `huggingface-cli download sentence-transformers/all-MiniLM-L6-v2 --revision <commit>` (the commit is the name of
  the snapshot directory in the Hugging Face cache). Without it, startup is refused: a branch or tag can move
  upstream and would silently mix two vector spaces in the cache, which is keyed by model and revision.
- Or set `embedding_model` to a local model directory. No revision is needed; cached vectors are keyed by the path,
  so give a new model a new path.

A model missing from the local files fails at startup with a hint to fetch it. The `vector` extra
(`sentence-transformers>=3,<7`) is not in the hash-pinned locks and the container image does not include it.

With embeddings on, the first retrieval after enabling them, or after many new memories, encodes up to
`candidate_window + fts_candidates` memories on the planner's event loop before the cache is warm.

Quality is measured in `tests/test_memory_ranked_retrieval.py` on 15 paraphrased English and Chinese tasks over 30
memories: recall@4 and MRR are 1.0 with this ranking and 0.0 with the previous all-words substring match. Stored the
way the orchestrator writes them (ExperienceExtractor output with the channel tag, tool names and fixed advice), the
same memories still give recall@4 and MRR 1.0, and 8 unrelated tasks, 4 of them made only of those shared words, get
no memory.

The PostgreSQL store (`repositories/postgres_state.py`) still uses the previous all-words match until it calls
`rank_memory_rows`; embeddings are SQLite-only and the runtime refuses `embeddings_enabled` on other stores.
