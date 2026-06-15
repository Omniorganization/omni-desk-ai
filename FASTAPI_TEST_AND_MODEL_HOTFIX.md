# FastAPI Test and Model Compatibility Hotfix

Fixes local test failures after the 17-item hardening package:

```text
ModuleNotFoundError: No module named 'fastapi'
ValueError: '<field>' in __slots__ conflicts with class variable
```

Changes:

- `tests/test_webhook_forced_signatures.py` now skips the FastAPI integration test when FastAPI is not installed, instead of failing at collection time.
- `omnidesk_agent/core/models.py` avoids `@dataclass(slots=True)` and explicit slot definitions that conflict with dataclass defaults.
- Model objects still hide `__dict__` to catch unsafe serialization patterns while preserving `dataclasses.asdict()` compatibility.

Recommended for full server integration tests:

```bash
python3 -m pip install --require-hashes -r requirements.dev.lock
python3 -m pip install -e . --no-deps --no-build-isolation
```
