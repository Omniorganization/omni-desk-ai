
# httpx Optional Import Fix

Fixes:

```text
ModuleNotFoundError: No module named 'httpx'
```

Channel adapters can now be imported for envelope parsing and validation even when
`httpx` is not installed. Outbound channel HTTP calls still require `httpx`.

Install option:

```bash
python3 -m pip install httpx
python3 -m pip install --require-hashes -r requirements.dev.lock
python3 -m pip install -e . --no-deps --no-build-isolation
```
