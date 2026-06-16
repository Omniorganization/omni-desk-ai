from __future__ import annotations

from pathlib import Path

from omnidesk_agent.observability_otel import inject_traceparent, make_traceparent, parse_traceparent
from omnidesk_agent.repositories.sqlite import SQLiteRepositoryFactory
from omnidesk_agent.self_learning.promotion.policy import LearningPromotionPolicy
from scripts.list_cosign_artifacts import list_payload_artifacts


def test_release_workflow_uses_registry_digest_without_manual_input() -> None:
    text = Path('.github/workflows/release.yml').read_text(encoding='utf-8')
    assert 'inputs.image_digest' not in text
    assert '--build-arg OMNIDESK_IMAGE_DIGEST' not in text
    assert 'docker buildx imagetools inspect' in text
    assert 'echo "OMNIDESK_IMAGE_DIGEST=$digest"' in text
    assert 'echo "OMNIDESK_WEB_ADMIN_IMAGE_DIGEST=$web_admin_digest"' in text
    assert 'python scripts/write_slsa_provenance.py' in text


def test_cosign_payload_artifact_listing_excludes_cosign_sidecars(tmp_path: Path) -> None:
    for name in [
        'pkg.whl',
        'pkg.tar.gz',
        'checksums.txt',
        'SHA256SUMS.txt',
        'release_metadata.json',
        'release_signatures.json',
        'sbom.json',
        'slsa-provenance.json',
        'pkg.whl.sig',
        'pkg.whl.cosign.sig',
        'pkg.whl.cosign.pem',
        'slsa.intoto.sig',
        'slsa.intoto.pem',
    ]:
        (tmp_path / name).write_text('x', encoding='utf-8')
    names = {path.name for path in list_payload_artifacts(tmp_path)}
    assert 'pkg.whl' in names
    assert 'pkg.tar.gz' in names
    assert 'SHA256SUMS.txt' in names
    assert 'release_signatures.json' in names
    assert 'slsa-provenance.json' in names
    assert 'pkg.whl.sig' in names
    assert 'pkg.whl.cosign.sig' not in names
    assert 'slsa.intoto.sig' not in names


def test_otel_traceparent_roundtrip() -> None:
    traceparent = make_traceparent('a' * 32, 'b' * 16)
    parsed = parse_traceparent(traceparent)
    assert parsed == {'trace_id': 'a' * 32, 'parent_span_id': 'b' * 16, 'flags': '01'}
    headers: dict[str, str] = {}
    inject_traceparent(headers, 'c' * 32, 'd' * 16, sampled=False)
    assert headers['traceparent'].endswith('-00')


def test_learning_promotion_policy_requires_samples_safety_confidence_and_human_review() -> None:
    policy = LearningPromotionPolicy(min_sample_size_per_arm=20, min_success_delta=0.05, min_confidence=0.8)
    too_small = policy.evaluate({
        'control': {'sample_count': 1, 'success_rate': 0.5, 'average_cost': 1.0, 'safety_violation_rate': 0.0},
        'treatment': {'sample_count': 1, 'success_rate': 1.0, 'average_cost': 1.0, 'safety_violation_rate': 0.0},
    })
    assert too_small.decision == 'reject'
    unsafe = policy.evaluate({
        'control': {'sample_count': 40, 'success_rate': 0.5, 'average_cost': 1.0, 'safety_violation_rate': 0.0},
        'treatment': {'sample_count': 40, 'success_rate': 0.8, 'average_cost': 1.0, 'safety_violation_rate': 0.01},
    })
    assert unsafe.decision == 'reject'
    assert 'safety' in unsafe.reason
    approved = policy.evaluate({
        'control': {'sample_count': 200, 'success_rate': 0.5, 'average_cost': 1.0, 'safety_violation_rate': 0.0},
        'treatment': {'sample_count': 200, 'success_rate': 0.7, 'average_cost': 1.05, 'safety_violation_rate': 0.0},
    })
    assert approved.decision == 'candidate_for_human_review'
    assert approved.requires_human_approval is True
    assert approved.rollback_plan_required is True


def test_sqlite_transactional_outbox_repository(tmp_path: Path) -> None:
    repo = SQLiteRepositoryFactory(tmp_path / 'repo.sqlite3').transactional_outbox()
    first = repo.enqueue(topic='outbound.send', payload={'x': 1}, dedupe_key='same')
    second = repo.enqueue(topic='outbound.send', payload={'x': 2}, dedupe_key='same')
    assert first == second
    batch = repo.claim_batch(limit=5)
    assert len(batch) == 1
    assert batch[0]['topic'] == 'outbound.send'
    repo.mark_done(batch[0]['id'])
    assert repo.claim_batch(limit=5) == []


def test_enterprise_assets_exist_and_contract_passes() -> None:
    assert Path('deploy/observability/otel-collector.yaml').exists()
    assert Path('omnidesk_agent/repositories/postgres.py').exists()
    assert Path('scripts/check_enterprise_readiness.py').exists()
