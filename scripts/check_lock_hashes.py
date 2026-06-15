from __future__ import annotations

from pathlib import Path
import re
import sys

REQ_RE = re.compile(r"^[A-Za-z0-9_.-]+==[^\s;]+")
HASH_RE = re.compile(r"--hash=sha256:[a-f0-9]{64}")

path = Path(sys.argv[1] if len(sys.argv) > 1 else "requirements.lock")
if not path.exists():
    raise SystemExit(f"missing lockfile: {path}")
missing: list[str] = []
current: str | None = None
has_hash = False
for raw in path.read_text(encoding="utf-8").splitlines() + [""]:
    line = raw.strip()
    if current and HASH_RE.search(line):
        has_hash = True
        continue
    if REQ_RE.match(line):
        if current and not has_hash:
            missing.append(current)
        current = line.split()[0].rstrip("\\")
        has_hash = bool(HASH_RE.search(line))
        continue
    if not line:
        if current and not has_hash:
            missing.append(current)
        current = None
        has_hash = False
        continue
    # comments, index options, and continuation comments do not terminate a requirement block.
    if line.startswith("#") or line.startswith("--"):
        continue
if missing:
    print("requirements.lock must be a transitive hash lock. Missing --hash entries:")
    for dep in missing[:50]:
        print(f"  - {dep}")
    raise SystemExit(2)
print(f"{path} contains hash-locked requirements")
