# Learned artefact lifecycle

Learned artefacts are planner guidance that the agent derives from recorded
runs: skills (tool procedures), knowledge entries, prompt guidance and
workflow rules. Code: `omnidesk_agent/self_learning/policy_registry.py`.

## What an artefact can change

Applied artefacts form one versioned **planner policy**
(`omnidesk_agent/self_learning/policy_hook.py:PlannerPolicy`):

| artefact kind | becomes | limit |
|---|---|---|
| `skill` | a section of `skills_block` | 4000 chars each, 6000 total |
| `prompt` | part of `prompt_addendum` | 1200 chars each, 2000 total |
| `workflow` | one entry of `rules` | 300 chars |
| `knowledge` | one `Knowledge: ...` entry of `rules` | 289 chars |

A policy never changes tool availability, plan validation, permissions or
approval thresholds; those stay in code. A policy over budget is refused, not
truncated. Status today: the planner does not read the policy yet. That needs
the planner hook (`planner.plan(msg, policy=...)`), which is being added with
the agent-core re-planning loop; until then `active_policy()` is computed and
stored but has no runtime effect.

## Lifecycle

```text
register_candidate -> record_evaluation -> request_approval
    -> decide (human, stored) -> apply -> rollback
```

1. **Candidate.** `DailySelfLearningJob` mines skills from successful runs
   (`skill_learning/structural_miner.py`). A skill is shared with every
   requester, so it holds only the task type and the `tool.action` sequence,
   never message text, and it needs at least 3 successful runs from at least
   2 different requesters. Text that asks for a forbidden change (bypass
   approval, delete audit logs, push to main, weaken the sandbox, in English
   or Chinese, including rephrasings) is stored as `blocked`.
2. **Evaluation.** `record_evaluation` stores a verdict (`improved`,
   `non_inferior`, `regressed`, `inconclusive`), the method and the number of
   evaluated traces. A passing verdict needs at least one trace. The
   evaluation remembers the policy version it was run against.
3. **Approval.** `request_approval` needs a passing evaluation. The approval
   is bound to the artefact's content hash. `decide` stores the decision with
   the reviewer identity; the pipeline's own identities (`system`, `agent`,
   `omnidesk-self-learning`, ...) cannot approve, and a decided approval is
   final.
4. **Apply.** `apply` re-reads the stored approval (an approval object a
   caller passes in is not trusted), checks the content hash, and refuses if
   the active policy changed since the evaluation (re-run it). It creates a
   new immutable policy version and moves the `active` pointer. An artefact
   with the same kind and name is superseded.
5. **Rollback.** `rollback` moves the pointer back to the policy that was
   active before the latest apply (an activation stack, so the order artefacts
   were applied in is respected), or to an earlier activation named with
   `--to-version`; it marks the removed artefacts `rolled_back`, restores
   superseded ones, and writes a `rollback_event` to the learning audit log.
   A rolled-back artefact needs a new evaluation and approval.

Every pointer move is in `learning_policy_history` with actor and reason.
Rows edited outside this API (for example a forged `approved` status or a
changed body) are refused at apply, and a policy whose stored content does
not match its hashes is never composed.

## Operator CLI

```bash
python -m omnidesk_agent.self_learning.cli list --status candidate
python -m omnidesk_agent.self_learning.cli request-approval <artifact_id>
python -m omnidesk_agent.self_learning.cli decide <approval_id> approved --reviewer you@example.com --reason "replay improved"
python -m omnidesk_agent.self_learning.cli apply <artifact_id> --actor you@example.com
python -m omnidesk_agent.self_learning.cli rollback --actor you@example.com --reason "canary regression"
python -m omnidesk_agent.self_learning.cli active
python -m omnidesk_agent.self_learning.cli history
```

`--reviewer`/`--actor` is recorded together with the OS account that ran the
command. Files: `<workspace>/learning_policy.sqlite3` (artefacts, versions,
pointer, history) and `<workspace>/self_learning.sqlite3` (approvals and
approval history).

## Not done yet

- The planner does not consume the policy (see above).
- Evaluation: the paired replay and the online A/B/canary gate that produce
  evaluations are described in [ONLINE_LEARNING_EXPERIMENTS.md](ONLINE_LEARNING_EXPERIMENTS.md).
- No HTTP route; the admin route needs the server wiring change.
- Knowledge, prompt and workflow drafts from `KnowledgeBuilder` are still
  human-facing review notes under `self_learning/pending_updates/`; they are
  not artefacts and are never applied.
