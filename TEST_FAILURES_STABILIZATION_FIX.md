# Test Failure Stabilization Fix

This patch fixes the failures seen after the production-boundary update:

- Restores no-`__dict__` model dataclasses using manual `__slots__`.
- Makes `PlanValidator` work with registries that only expose `describe()`.
- Makes `RunStore.save_waiting()` backward-compatible while still rotating waiting resume tokens.
- Rejects non-null resume tokens when a run is not waiting for approval.
- Removes the need for `pytest-asyncio` in existing async tests by using `asyncio.run()`.
- Fixes generated typing artifacts such as `Union[str, list][str]`.
- GA12 later raises `requires-python` to `>=3.10` to keep the web stack on audited security releases.
