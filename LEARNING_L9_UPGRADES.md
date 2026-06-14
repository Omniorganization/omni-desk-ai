# Learning L9 Pre-production Upgrades

This release adds a minimal but executable industrial learning layer beyond replay, drift detection, memory governance, and skill promotion.

## Added capabilities

- Learning A/B experiment framework with deterministic cohort assignment, SQLite persistence, metric aggregation, safety gates, and winner selection.
- Multi-agent memory review with reviewer, critic, and safety votes before candidate memory can be trusted.
- Causal learning with causal graph construction and root-cause analysis for failure chains.
- Long-horizon skill evolution with version lineage, benchmark history, version comparison, and retirement tracking.
- Learning ROI analysis to block low-value learning work when expected benefit does not justify compute/risk cost.
- World model primitives for environment entities, relations, state transitions, and next-state prediction.

## Validation

- `python -m compileall -q omnidesk_agent tests`
- `pytest --cov=omnidesk_agent --cov-report=json --cov-fail-under=75 -q`
- `python scripts/check_coverage_gates.py coverage.json`

Current validation result in this sandbox:

- 184 tests passed
- Total coverage: 76.52%
- `security/`: 92.13% >= 85%
- `core/`: >= 85% grouped gate
- `tools/`: >= 85% grouped gate

## Remaining note

Most SQLite context-manager leaks were fixed by making `connect_sqlite()` close connections on context exit. The suite still emits 6 ResourceWarning entries from a FastAPI webhook integration test path; they do not fail tests, but can be converted into a strict warning gate in the next cleanup pass.
