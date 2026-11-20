"""Versioned registry of learned artefacts and the planner policy they form.

Lifecycle of one artefact (skill, knowledge entry, prompt guidance, workflow
rule)::

    register_candidate -> record_evaluation -> request_approval
        -> decide (human, stored) -> apply -> [rollback]

``apply`` creates a new immutable policy version (the previous active version
plus the artefact) and moves the ``active`` pointer to it. ``rollback`` moves
the pointer back. The planner reads ``active_policy()``; experiments and
replay read ``candidate_policy(artifact_id)``. Nothing here changes tools,
permissions or approval thresholds.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from omnidesk_agent.self_learning.approval import HumanApprovalGate
from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.observability.schema import LearningEvent
from omnidesk_agent.self_learning.policy import STAGE_KNOWLEDGE_PROMPT, SelfLearningBoundaryPolicy
from omnidesk_agent.self_learning.policy_hook import (
    MAX_PROMPT_ADDENDUM_CHARS,
    MAX_RULE_CHARS,
    PlannerPolicy,
    policy_version,
)
from omnidesk_agent.self_learning.schemas import ApprovalRecord, LearningProposal
from omnidesk_agent.storage.sqlite import connect_sqlite

ARTIFACT_KINDS = ("skill", "knowledge", "prompt", "workflow")
MAX_BODY_CHARS = {
    "skill": 4000,
    "knowledge": MAX_RULE_CHARS - len("Knowledge: "),
    "prompt": 1200,
    "workflow": MAX_RULE_CHARS,
}
APPROVAL_TYPES = {
    "skill": "skill_update",
    "knowledge": "knowledge_update",
    "prompt": "prompt_template",
    "workflow": "workflow_rule",
}
PASSING_VERDICTS = frozenset({"improved", "non_inferior"})
EVALUATION_VERDICTS = PASSING_VERDICTS | {"regressed", "inconclusive"}
ACTIVE = "active"
EVALUABLE_STATUSES = {"candidate", "evaluated", "pending_approval", "approved", "rolled_back"}

_SLUG = re.compile(r"[^a-z0-9_\-一-鿿]+")


def content_sha256(kind: str, name: str, body: str) -> str:
    canonical = json.dumps({"kind": kind, "name": name, "body": body}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", str(name or "").lower()).strip("-")
    if len(slug) <= 80:
        return slug
    # Distinct long names must not collapse into one slot (same kind + name supersedes).
    return slug[:69].rstrip("-") + "-" + hashlib.sha256(slug.encode("utf-8")).hexdigest()[:10]


@dataclass
class LearnedArtifact:
    artifact_id: str
    kind: str
    name: str
    body: str
    content_sha256: str
    status: str = "candidate"
    source: dict[str, Any] = field(default_factory=dict)
    evaluation: Optional[dict[str, Any]] = None
    approval_id: Optional[str] = None
    blockers: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PolicyRegistry:
    def __init__(
        self,
        db_path: Path,
        *,
        approvals: HumanApprovalGate,
        audit: Optional[LearningAuditLog] = None,
        boundary: Optional[SelfLearningBoundaryPolicy] = None,
    ):
        if approvals.store is None:
            raise ValueError("PolicyRegistry requires an approval gate with a store")
        self.db_path = Path(db_path).expanduser()
        self.approvals = approvals
        self.audit = audit
        self.boundary = boundary or SelfLearningBoundaryPolicy()
        self._init()

    # ------------------------------------------------------------------ setup
    def _connect(self) -> sqlite3.Connection:
        con = connect_sqlite(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS learning_artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  name TEXT NOT NULL,
                  status TEXT NOT NULL,
                  approval_id TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  payload_json TEXT NOT NULL
                )""")
            con.execute("CREATE INDEX IF NOT EXISTS learning_artifacts_status ON learning_artifacts(status)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS learning_policy_versions (
                  version_id TEXT PRIMARY KEY,
                  parent_version TEXT,
                  artifact_ids_json TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  created_by TEXT NOT NULL
                )""")
            con.execute("""
                CREATE TABLE IF NOT EXISTS learning_policy_pointer (
                  name TEXT PRIMARY KEY,
                  version_id TEXT NOT NULL,
                  updated_at REAL NOT NULL
                )""")
            con.execute("""
                CREATE TABLE IF NOT EXISTS learning_policy_stack (
                  position INTEGER PRIMARY KEY AUTOINCREMENT,
                  version_id TEXT NOT NULL
                )""")
            con.execute("""
                CREATE TABLE IF NOT EXISTS learning_policy_history (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  at REAL NOT NULL,
                  action TEXT NOT NULL,
                  from_version TEXT NOT NULL,
                  to_version TEXT NOT NULL,
                  artifact_id TEXT,
                  actor TEXT NOT NULL,
                  reason TEXT NOT NULL
                )""")
            empty = policy_version([])
            con.execute(
                "INSERT OR IGNORE INTO learning_policy_versions(version_id, parent_version, artifact_ids_json, created_at, created_by) VALUES(?,?,?,?,?)",
                (empty, None, "[]", time.time(), "init"),
            )
            con.execute(
                "INSERT OR IGNORE INTO learning_policy_pointer(name, version_id, updated_at) VALUES(?,?,?)",
                (ACTIVE, empty, time.time()),
            )

    # ------------------------------------------------------------- candidates
    def register_candidate(self, kind: str, name: str, body: str, *, source: Optional[dict[str, Any]] = None) -> LearnedArtifact:
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"unknown artefact kind: {kind}")
        slug = slugify(name)
        text = str(body or "").strip()
        if not slug:
            raise ValueError("artefact name is empty")
        if not text:
            raise ValueError("artefact body is empty")
        if len(text) > MAX_BODY_CHARS[kind]:
            raise ValueError(f"{kind} artefact body exceeds {MAX_BODY_CHARS[kind]} characters")
        digest = content_sha256(kind, slug, text)
        artifact_id = f"la_{hashlib.sha256(f'{kind}:{slug}:{digest}'.encode()).hexdigest()[:16]}"
        existing = self.get(artifact_id)
        if existing is not None:
            return existing
        proposal = self._proposal(kind, slug, text, artifact_id)
        blockers = list(self.boundary.evaluate(proposal).blockers)
        artifact = LearnedArtifact(
            artifact_id=artifact_id,
            kind=kind,
            name=slug,
            body=text,
            content_sha256=digest,
            status="blocked" if blockers else "candidate",
            source=dict(source or {}),
            blockers=blockers,
        )
        with self._connect() as con:
            self._write(con, artifact)
        return artifact

    def record_evaluation(self, artifact_id: str, evaluation: dict[str, Any]) -> LearnedArtifact:
        artifact = self._require(artifact_id)
        if artifact.status not in EVALUABLE_STATUSES:
            raise PermissionError(f"artefact {artifact_id} cannot be evaluated in status {artifact.status}")
        verdict = str(evaluation.get("verdict") or "")
        if verdict not in EVALUATION_VERDICTS:
            raise ValueError(f"evaluation verdict must be one of {sorted(EVALUATION_VERDICTS)}")
        if not str(evaluation.get("method") or "").strip():
            raise ValueError("evaluation must name its method")
        trace_count = int(evaluation.get("trace_count") or 0)
        if verdict in PASSING_VERDICTS and trace_count <= 0:
            raise ValueError("a passing evaluation needs at least one evaluated trace")
        artifact.evaluation = {
            **evaluation,
            "trace_count": trace_count,
            "content_sha256": artifact.content_sha256,
            "base_version": self.active_version(),
            "recorded_at": time.time(),
        }
        if artifact.status in {"candidate", "rolled_back"}:
            artifact.status = "evaluated"
        artifact.updated_at = time.time()
        with self._connect() as con:
            self._write(con, artifact)
        return artifact

    def request_approval(self, artifact_id: str) -> ApprovalRecord:
        artifact = self._require(artifact_id)
        if artifact.status != "evaluated":
            raise PermissionError(f"artefact {artifact_id} needs an evaluation before approval (status {artifact.status})")
        verdict = (artifact.evaluation or {}).get("verdict")
        if verdict not in PASSING_VERDICTS:
            raise PermissionError(f"artefact {artifact_id} did not pass evaluation (verdict {verdict})")
        approval = self.approvals.submit(
            self._proposal(artifact.kind, artifact.name, artifact.body, artifact.artifact_id),
            approval_type=APPROVAL_TYPES[artifact.kind],
            reason=f"apply learned {artifact.kind} '{artifact.name}' to the planner policy",
            content_sha256=artifact.content_sha256,
        )
        artifact.approval_id = approval.approval_id
        artifact.status = "pending_approval"
        artifact.updated_at = time.time()
        with self._connect() as con:
            self._write(con, artifact)
        return approval

    def decide(self, approval_id: str, decision: str, *, reviewer: str, reason: str = "") -> LearnedArtifact:
        artifact = self._by_approval(approval_id)
        if artifact.status != "pending_approval":
            raise PermissionError(f"artefact {artifact.artifact_id} is not waiting for approval (status {artifact.status})")
        record = self.approvals.decide(approval_id, decision, reviewer=reviewer, reason=reason)
        artifact.status = "approved" if record.status == "approved" else "rejected"
        artifact.updated_at = time.time()
        with self._connect() as con:
            self._write(con, artifact)
        return artifact

    # ------------------------------------------------------------ apply/rollback
    def apply(self, artifact_id: str, *, actor: str, reason: str = "") -> PlannerPolicy:
        actor = str(actor or "").strip()
        if not actor:
            raise PermissionError("apply requires an actor identity")
        artifact = self._require(artifact_id)
        if artifact.status != "approved" or not artifact.approval_id:
            raise PermissionError(f"artefact {artifact_id} is not approved (status {artifact.status})")
        if content_sha256(artifact.kind, artifact.name, artifact.body) != artifact.content_sha256:
            raise PermissionError(f"artefact {artifact_id} content changed after registration")
        # The stored decision authorizes, not the artefact row.
        self.approvals.require_approved(
            ApprovalRecord(proposal_id=artifact.artifact_id, approval_type=APPROVAL_TYPES[artifact.kind], approval_id=artifact.approval_id),
            content_sha256=artifact.content_sha256,
        )
        evaluation = artifact.evaluation or {}
        if evaluation.get("verdict") not in PASSING_VERDICTS:
            raise PermissionError(f"artefact {artifact_id} has no passing evaluation")
        if evaluation.get("content_sha256") != artifact.content_sha256:
            raise PermissionError("evaluation does not match the artefact content")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = self._pointer(con)
            if evaluation.get("base_version") != current:
                raise PermissionError(
                    f"artefact was evaluated against policy {evaluation.get('base_version')}; "
                    f"active policy is {current}; re-run the evaluation"
                )
            current_ids = self._version_ids(con, current)
            members = [self._load(con, aid) for aid in current_ids]
            replaced = [m for m in members if m.kind == artifact.kind and m.name == artifact.name]
            kept = [m for m in members if m not in replaced]
            new_members = kept + [artifact]
            policy = self._compose(new_members, policy_id="planner_current")
            self._insert_version(con, policy, parent=current, actor=actor)
            # Version ids are content hashes, so the same set reached in another
            # order reuses a row; rollback follows the activation stack instead.
            con.execute("INSERT INTO learning_policy_stack(version_id) VALUES(?)", (current,))
            self._move_pointer(con, current, policy.version, action="apply", artifact_id=artifact_id, actor=actor, reason=reason)
            for old in replaced:
                old.status = "superseded"
                old.updated_at = time.time()
                self._write(con, old)
            artifact.status = "applied"
            artifact.updated_at = time.time()
            self._write(con, artifact)
        return policy

    def rollback(self, *, actor: str, reason: str, to_version: Optional[str] = None) -> PlannerPolicy:
        actor = str(actor or "").strip()
        if not actor or not str(reason or "").strip():
            raise PermissionError("rollback requires an actor and a reason")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = self._pointer(con)
            if to_version is None:
                row = con.execute("SELECT position, version_id FROM learning_policy_stack ORDER BY position DESC LIMIT 1").fetchone()
            else:
                row = con.execute(
                    "SELECT position, version_id FROM learning_policy_stack WHERE version_id=? ORDER BY position DESC LIMIT 1",
                    (to_version,),
                ).fetchone()
            if row is None:
                raise PermissionError("no earlier activation to roll back to" if to_version is None else f"{to_version} is not an earlier activation")
            target = str(row["version_id"])
            if target == current:
                raise PermissionError("target version is already active")
            con.execute("DELETE FROM learning_policy_stack WHERE position >= ?", (int(row["position"]),))
            target_ids = self._version_ids(con, target)
            current_ids = self._version_ids(con, current)
            for aid in current_ids:
                if aid not in target_ids:
                    member = self._load(con, aid)
                    member.status = "rolled_back"
                    member.updated_at = time.time()
                    self._write(con, member)
            for aid in target_ids:
                if aid not in current_ids:
                    member = self._load(con, aid)
                    member.status = "applied"
                    member.updated_at = time.time()
                    self._write(con, member)
            self._move_pointer(con, current, target, action="rollback", artifact_id=None, actor=actor, reason=reason)
            policy = self._compose([self._load(con, aid) for aid in target_ids], policy_id="planner_current")
        if self.audit is not None:
            self.audit.append(LearningEvent(
                event_type="rollback_event",
                rollback_success=True,
                metadata={"from_version": current, "to_version": target, "actor": actor, "reason": reason},
            ))
        return policy

    # ----------------------------------------------------------------- reads
    def active_version(self) -> str:
        with self._connect() as con:
            return self._pointer(con)

    def active_policy(self) -> PlannerPolicy:
        with self._connect() as con:
            members = [self._load(con, aid) for aid in self._version_ids(con, self._pointer(con))]
        return self._compose(members, policy_id="planner_current")

    def candidate_policy(self, artifact_id: str) -> PlannerPolicy:
        """Active policy plus one candidate, for sandboxed replay or a governed experiment."""
        candidate = self._require(artifact_id)
        if candidate.status in {"blocked", "rejected", "superseded"}:
            raise PermissionError(f"artefact {artifact_id} cannot be evaluated (status {candidate.status})")
        with self._connect() as con:
            members = [self._load(con, aid) for aid in self._version_ids(con, self._pointer(con))]
        members = [m for m in members if not (m.kind == candidate.kind and m.name == candidate.name)] + [candidate]
        return self._compose(members, policy_id="planner_candidate")

    def get(self, artifact_id: str) -> Optional[LearnedArtifact]:
        with self._connect() as con:
            row = con.execute("SELECT payload_json FROM learning_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return None if row is None else LearnedArtifact(**json.loads(row["payload_json"]))

    def list_artifacts(self, *, statuses: Optional[Iterable[str]] = None, limit: int = 100) -> list[LearnedArtifact]:
        with self._connect() as con:
            if statuses:
                wanted = sorted(set(statuses))
                marks = ",".join("?" for _ in wanted)
                rows = con.execute(
                    f"SELECT payload_json FROM learning_artifacts WHERE status IN ({marks}) ORDER BY updated_at DESC LIMIT ?",  # nosec B608
                    (*wanted, max(0, int(limit))),
                ).fetchall()
            else:
                rows = con.execute("SELECT payload_json FROM learning_artifacts ORDER BY updated_at DESC LIMIT ?", (max(0, int(limit)),)).fetchall()
        return [LearnedArtifact(**json.loads(row["payload_json"])) for row in rows]

    def history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM learning_policy_history ORDER BY id DESC LIMIT ?", (max(0, int(limit)),)).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- internals
    def _compose(self, members: list[LearnedArtifact], *, policy_id: str) -> PlannerPolicy:
        for member in members:
            # A row edited outside the lifecycle must never reach a planner.
            if content_sha256(member.kind, member.name, member.body) != member.content_sha256:
                raise PermissionError(f"artefact {member.artifact_id} content does not match its registered hash")
        ordered = sorted(members, key=lambda m: (ARTIFACT_KINDS.index(m.kind), m.name))
        prompt = "\n\n".join(m.body for m in ordered if m.kind == "prompt")
        if len(prompt) > MAX_PROMPT_ADDENDUM_CHARS:
            raise PermissionError("applied prompt guidance would exceed the planner prompt budget; retire an entry first")
        skills = "\n\n".join(f"## {m.name}\n{m.body}" for m in ordered if m.kind == "skill")
        rules = tuple(
            [m.body for m in ordered if m.kind == "workflow"]
            + [f"Knowledge: {m.body}" for m in ordered if m.kind == "knowledge"]
        )
        try:
            return PlannerPolicy(
                policy_id=policy_id,
                version=policy_version([(m.kind, m.name, m.content_sha256) for m in ordered]),
                prompt_addendum=prompt,
                skills_block=skills,
                rules=rules,
                artifact_ids=tuple(m.artifact_id for m in ordered),
            )
        except ValueError as exc:  # over budget: refuse instead of truncating
            raise PermissionError(f"policy exceeds planner budget: {exc}") from exc

    @staticmethod
    def _proposal(kind: str, name: str, body: str, artifact_id: str) -> LearningProposal:
        return LearningProposal(
            stage=STAGE_KNOWLEDGE_PROMPT,
            proposal_type=APPROVAL_TYPES[kind],
            title=f"Learned {kind}: {name}",
            problem=name,
            proposed_change=body,
            expected_benefit="planner guidance learned from recorded runs",
            rollback_plan="Roll the planner policy pointer back to the previous version.",
            proposal_id=artifact_id,
        )

    def _require(self, artifact_id: str) -> LearnedArtifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        return artifact

    def _by_approval(self, approval_id: str) -> LearnedArtifact:
        with self._connect() as con:
            row = con.execute("SELECT payload_json FROM learning_artifacts WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return LearnedArtifact(**json.loads(row["payload_json"]))

    @staticmethod
    def _write(con: sqlite3.Connection, artifact: LearnedArtifact) -> None:
        con.execute(
            """
            INSERT INTO learning_artifacts(artifact_id, kind, name, status, approval_id, created_at, updated_at, payload_json)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(artifact_id) DO UPDATE SET
              status=excluded.status, approval_id=excluded.approval_id,
              updated_at=excluded.updated_at, payload_json=excluded.payload_json
            """,
            (
                artifact.artifact_id, artifact.kind, artifact.name, artifact.status, artifact.approval_id,
                artifact.created_at, artifact.updated_at, json.dumps(artifact.to_dict(), ensure_ascii=False),
            ),
        )

    @staticmethod
    def _load(con: sqlite3.Connection, artifact_id: str) -> LearnedArtifact:
        row = con.execute("SELECT payload_json FROM learning_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return LearnedArtifact(**json.loads(row["payload_json"]))

    @staticmethod
    def _pointer(con: sqlite3.Connection) -> str:
        row = con.execute("SELECT version_id FROM learning_policy_pointer WHERE name=?", (ACTIVE,)).fetchone()
        return str(row["version_id"])

    @staticmethod
    def _version_ids(con: sqlite3.Connection, version_id: str) -> list[str]:
        row = con.execute("SELECT artifact_ids_json FROM learning_policy_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown policy version {version_id}")
        return list(json.loads(row["artifact_ids_json"]))

    @staticmethod
    def _insert_version(con: sqlite3.Connection, policy: PlannerPolicy, *, parent: str, actor: str) -> None:
        con.execute(
            "INSERT OR IGNORE INTO learning_policy_versions(version_id, parent_version, artifact_ids_json, created_at, created_by) VALUES(?,?,?,?,?)",
            (policy.version, parent, json.dumps(list(policy.artifact_ids)), time.time(), actor),
        )

    @staticmethod
    def _move_pointer(con: sqlite3.Connection, current: str, target: str, *, action: str, artifact_id: Optional[str], actor: str, reason: str) -> None:
        now = time.time()
        con.execute("UPDATE learning_policy_pointer SET version_id=?, updated_at=? WHERE name=?", (target, now, ACTIVE))
        con.execute(
            "INSERT INTO learning_policy_history(at, action, from_version, to_version, artifact_id, actor, reason) VALUES(?,?,?,?,?,?,?)",
            (now, action, current, target, artifact_id, actor, reason or ""),
        )
