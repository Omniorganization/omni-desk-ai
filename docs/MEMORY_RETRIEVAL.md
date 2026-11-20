# Planner memory retrieval

`ExperienceStore.retrieve_for_task(task, limit, channel=, actor=)` (SQLite) gives the planner the memories of the
same channel and actor that are most relevant to the task. Visibility rules are unchanged: only `candidate`,
`validated` or `trusted` memories that have not expired, never another channel's or actor's.

Ranking (`memory/retrieval.py`):

- Candidates: the actor's `memory_retrieval.candidate_window` (500) most recently updated memories, plus up to
  `fts_candidates` (200) older ones the FTS5 index matches (prefix match on ASCII words; skipped when memory is
  encrypted at rest, because the index then holds ciphertext).
- Text: the goal (counted twice), task type, tools used, recommended next action, failure reason and tags.
- Terms: runs of Chinese, Japanese or Korean characters become character bigrams; other text becomes lightly
  stemmed words, minus a short English stopword list.
- Score: BM25 (k1 1.5, b 0.75). A memory needs at least one matching term. Ties go to reviewed memories, then to the
  most recently updated.
- Optional embeddings (`embeddings_enabled`, off by default; `pip install 'omnidesk-agent[vector]'`): a local
  sentence-transformers model; the score becomes `(1 - embedding_weight) * bm25/max + embedding_weight * cosine`, and
  a memory with no shared term qualifies at cosine >= `min_embedding_similarity`. One vector per memory and model is
  cached in `structured_experience_embeddings`, encrypted like the memory text, and recomputed when the text changes.

Each returned memory gets `last_used_at` set, after a re-check under the write lock that it is still visible.

Quality is measured in `tests/test_memory_ranked_retrieval.py` on 15 paraphrased English and Chinese tasks over 30
memories: recall@4 and MRR are 1.0 with this ranking and 0.0 with the previous all-words substring match.

The PostgreSQL store (`repositories/postgres_state.py`) still uses the previous all-words match until it calls
`rank_memory_rows`; embeddings are SQLite-only and the runtime refuses `embeddings_enabled` on other stores.
