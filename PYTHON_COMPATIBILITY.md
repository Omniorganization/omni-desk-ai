# Python Compatibility Fix

This package removes direct usage of `@dataclass(slots=True)` so the project can run on
Python 3.9 environments such as macOS CommandLineTools `/Library/Developer/CommandLineTools/usr/bin/python3`.

Recommended runtime remains Python 3.10+ or 3.11, but tests no longer fail during import
with:

```text
TypeError: dataclass() got an unexpected keyword argument 'slots'
```

If possible, use:

```bash
python3 --version
brew install python@3.11
python3.11 -m pip install -e ".[dev,test]"
python3.11 -m pytest -q
```
