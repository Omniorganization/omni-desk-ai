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


def _pins(filename: str) -> dict[str, str]:
    return dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", Path(filename).read_text(encoding="utf-8"), re.MULTILINE))


def test_gmail_deployment_lock_is_the_explicit_urllib3_allowance() -> None:
    # Explicit exception to the ban above: requirements.gmail.lock is opt-in
    # for deployments that enable Gmail, whose Google client stack needs
    # requests/urllib3. It must carry the patched urllib3, no audit tooling,
    # and exactly the image's (enterprise) pins for everything it shares, so
    # installing it after the default locks only adds the Gmail packages.
    gmail, enterprise = _pins("requirements.gmail.lock"), _pins("requirements.enterprise.lock")
    assert tuple(map(int, gmail["urllib3"].split("."))) >= (2, 8, 0)
    assert "filelock" not in gmail and "pip-audit" not in gmail and "bandit" not in gmail
    assert {"google-auth-oauthlib", "google-api-python-client"} <= gmail.keys()
    assert {name: (gmail[name], enterprise[name]) for name in gmail.keys() & enterprise.keys() if gmail[name] != enterprise[name]} == {}


def test_gmail_lock_license_gate_is_required_by_the_security_policy(tmp_path: Path) -> None:
    # The Gmail lock is resolved apart from the dev lock, so the dev-lock
    # license gate does not cover its 19 extra packages. Deleting its own gate
    # from security.yml must fail the policy, not pass silently.
    from scripts.check_security_workflow_policy import check

    gate = "scripts/check_license_policy.py --lockfile requirements.gmail.lock --policy release/license-policy.json"
    workflow = Path(".github/workflows/security.yml").read_text(encoding="utf-8")
    assert gate in workflow
    assert not [issue for issue in check(Path(".")) if gate in issue]
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "security.yml").write_text(
        "\n".join(line for line in workflow.splitlines() if gate not in line), encoding="utf-8"
    )
    assert [issue for issue in check(tmp_path) if gate in issue]


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
