# Python Compatibility Fix

This package removes direct usage of `@dataclass(slots=True)`.

GA12 raises the supported runtime floor to Python 3.10 so the FastAPI/Starlette
stack can remain on audited security releases. Historical Python 3.9 import
fixes are kept in the codebase, but Python 3.9 is no longer a release target.

Older 3.9 environments failed during import with:

```text
TypeError: dataclass() got an unexpected keyword argument 'slots'
```

If possible, use:

```bash
python3 --version
brew install python@3.11
python3.11 -m pip install --require-hashes -r requirements.dev.lock
python3.11 -m pip install -e . --no-deps --no-build-isolation
python3.11 -m pytest -q
```
