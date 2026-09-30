import json
import re
from pathlib import Path

import pytest


@pytest.mark.parametrize("filename", ["requirements.lock", "requirements.dev.lock", "requirements.security.lock"])
def test_security_tooling_locks_exclude_pre_patch_versions(filename: str) -> None:
    lock = Path(filename).read_text(encoding="utf-8")
    for package, minimum in (("urllib3", (2, 8, 0)), ("filelock", (4, 0, 0))):
        match = re.search(rf"^{package}==(\d+)\.(\d+)\.(\d+)(?:\s|$)", lock, re.MULTILINE)
        assert match is not None, f"missing {package} in {filename}"
        assert tuple(map(int, match.groups())) >= minimum


@pytest.mark.parametrize("filename", ["requirements.runtime.lock", "requirements.bootstrap.lock", "requirements.enterprise.lock"])
def test_optional_audit_dependencies_do_not_enter_production_locks(filename: str) -> None:
    lock = Path(filename).read_text(encoding="utf-8")
    assert not re.search(r"^(?:filelock|urllib3)==", lock, re.MULTILINE)


@pytest.mark.parametrize("filename", ["requirements.lock", "requirements.dev.lock", "requirements.security.lock", "requirements.runtime.lock", "requirements.enterprise.lock"])
def test_anyio_constraint_is_realized_in_all_affected_locks(filename: str) -> None:
    constraints = Path("requirements.security-constraints.in").read_text(encoding="utf-8")
    for package in ("anyio", "starlette"):
        floor = re.search(rf"^{package}>=(\d+)\.(\d+)\.(\d+)$", constraints, re.MULTILINE)
        resolved = re.search(rf"^{package}==(\d+)\.(\d+)\.(\d+)\s", Path(filename).read_text(encoding="utf-8"), re.MULTILINE)
        assert floor is not None and resolved is not None
        assert tuple(map(int, resolved.groups())) >= tuple(map(int, floor.groups()))


def test_native_manifest_and_central_rust_toolchain_match() -> None:
    contract = json.loads(Path(".github/toolchains.json").read_text(encoding="utf-8"))
    manifest = Path("apps/desktop-tauri/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    minimum = re.search(r'^rust-version = "(\d+\.\d+)"$', manifest, re.MULTILINE)
    assert minimum is not None
    assert contract["rust"].rsplit(".", 1)[0] == minimum.group(1)
