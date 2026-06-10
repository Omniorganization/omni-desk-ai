# Pydantic + Python 3.9 Compatibility Fix

This patch fixes import/test failures like:

```text
TypeError: Unable to evaluate type annotation 'str | None'
```

Reason:

- Python 3.9 does not support PEP 604 union syntax at runtime.
- Pydantic evaluates model annotations and can fail even when modules use
  `from __future__ import annotations`.

Changes:

- Replaced `T | None` with `Optional[T]`.
- Replaced a small set of simple `A | B` annotations with `Union[A, B]`.
- Ensured files using `Optional` / `Union` import them from `typing`.
- Kept `from __future__ import annotations` in place for forward-reference safety.
