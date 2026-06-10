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

Recommended runtime is still Python 3.10+ or Python 3.11, but this package is safer for Python 3.9 environments.
