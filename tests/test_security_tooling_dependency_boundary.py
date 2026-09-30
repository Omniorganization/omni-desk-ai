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
