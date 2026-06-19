# Python 3.9 Compatibility Fix

This patch fixes import-time failures on macOS CommandLineTools Python 3.9 such as:

```text
TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
TypeError: dataclass() got an unexpected keyword argument 'slots'
```

Changes:

- Adds `from __future__ import annotations` to Python modules so PEP 604-style annotations are not evaluated at import time.
- Removes remaining direct `@dataclass(slots=True)` usage.
- Keeps the governed self-improvement modules unchanged functionally.

GA12 raises the supported runtime floor to Python 3.10 so audited FastAPI/Starlette
security releases can be used. These compatibility fixes remain as historical
hardening, but Python 3.9 is no longer a release target.
