
# PyYAML Compatibility Fix

Fixes:

```text
ModuleNotFoundError: No module named 'yaml'
```

`omnidesk_agent.config` can now be imported without PyYAML, so unit tests for non-YAML paths can run before full installation. Loading YAML config files still requires PyYAML.

Install options:

```bash
python3 -m pip install PyYAML
python3 -m pip install --require-hashes -r requirements.dev.lock
python3 -m pip install -e . --no-deps --no-build-isolation
```
