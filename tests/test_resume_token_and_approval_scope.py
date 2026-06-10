from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.security.approval_store import ApprovalStore


def test_resume_token_required(tmp_path):
    runs = RunStore(tmp_path / "runs.sqlite3")
    rid = runs.create({"channel": "local", "sender_id": "u", "text": "x"})
    token = runs.get(rid)["resume_token"]
    runs.require_resume_token(rid, token)
    try:
        runs.require_resume_token(rid, "bad")
    except PermissionError:
        pass
    else:
        raise AssertionError("bad token accepted")


def test_approval_scope_hash_must_match(tmp_path):
    store = ApprovalStore(tmp_path / "approvals.sqlite3", ttl_seconds=600)
    proposal = {"tool": "computer", "action": "click", "run_id": "r1", "plan_id": "p1", "step_index": 0, "scope_hash": "abc"}
    aid = store.create(proposal)
    store.decide(aid, "approved")
    store.require_approved(aid, proposal)
    bad = dict(proposal)
    bad["scope_hash"] = "different"
    try:
        store.require_approved(aid, bad)
    except PermissionError:
        pass
    else:
        raise AssertionError("mismatched scope accepted")
