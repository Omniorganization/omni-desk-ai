# Contributing

## Development Rules

- Start from the root monorepo layout; do not add new root-level version-package directories as source.
- Keep release packages generated under `dist/` or workspace artifact folders, not as the canonical source tree.
- Preserve approval, audit, sandbox, security, and external evidence gates.
- Add or update tests for every production behavior change.
- Keep external Production GA claims blocked unless real evidence files satisfy `scripts/check_external_ga_evidence.py .`.

## Validation

Use the narrowest relevant checks while developing, then run release-governance checks before packaging:

```bash
python scripts/check_version_consistency.py .
python scripts/check_workflow_governance.py . --require-real-workflows
python scripts/check_ga_release_gate.py .
python -m pytest -q -p no:cacheprovider tests/test_distribution_manifest.py tests/test_workflow_governance.py tests/test_release_governance_assets.py tests/test_portable_sha256s.py
```

When `pytest` is run locally, avoid committing `.pytest_cache`, `.pyc`, `__pycache__`, frontend build folders, Flutter `.dart_tool`, Node `node_modules`, or generated release zips.
