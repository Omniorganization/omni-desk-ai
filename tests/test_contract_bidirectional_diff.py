"""The contract diff fails on drift in either direction.

It used to be advisory: it exited 0, its client direction failed on its own
template-literal parsing (``${encodeURIComponent(id)}``), it ignored the
``/admin`` routes Web Admin calls, and it read only three gateway modules.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("contract_diff", ROOT / "scripts" / "check_contract_bidirectional_diff.py")
assert spec and spec.loader
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Building the gateway's routing table writes under ~/.omnidesk.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_the_repository_has_no_contract_drift() -> None:
    assert diff.main([str(ROOT)]) == 0


@pytest.mark.parametrize(
    ("client_path", "normalized"),
    [
        ("/app/approvals/${encodeURIComponent(id)}/decide", "/app/approvals/{}/decide"),
        ("/app/notifications${url.search}", "/app/notifications"),
        ("/app/devices/enrollment/${_pathSegment(enrollmentId)}/verify", "/app/devices/enrollment/{}/verify"),
        ("/app/projects/$projectId", "/app/projects/{}"),
        ("/app/sync?since_seq=${sinceSeq}", "/app/sync"),
        ("/admin/channels/ecosystem", "/admin/channels/ecosystem"),
        ("/app/devices/enrollment/${enrollmentId}/challenge", "/app/devices/enrollment/{}/challenge"),
        ("/health", None),
    ],
)
def test_client_paths_are_normalized_like_contract_templates(client_path: str, normalized: str | None) -> None:
    assert diff._normalize_path(client_path) == normalized


def _repo_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "apps" / "shared").mkdir(parents=True)
    shutil.copy(ROOT / "apps" / "shared" / "omni-app-api.contract.json", root / "apps" / "shared")
    for app in ("web-admin-next", "desktop-tauri/src", "mobile-flutter/lib"):
        shutil.copytree(ROOT / "apps" / app, root / "apps" / app, ignore=shutil.ignore_patterns("node_modules", ".next", ".dart_tool", "build"))
    return root


def test_a_client_call_missing_from_the_contract_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo_copy(tmp_path)
    # The extractor recognises the call forms the clients use (this.request,
    # _request, fetch(, omniProxy().
    (root / "apps" / "desktop-tauri" / "src" / "extra.ts").write_text(
        "export class Extra {\n  request(path: string) { return path; }\n  exportReport(id: string) { return this.request(`/app/reports/${encodeURIComponent(id)}/export`); }\n}\n",
        encoding="utf-8",
    )
    assert diff.main([str(root)]) == 1
    assert "/app/reports/{}/export" in capsys.readouterr().err


def test_a_contract_route_the_gateway_lacks_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo_copy(tmp_path)
    path = root / "apps" / "shared" / "omni-app-api.contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract["endpoints"].append({"method": "GET", "path": "/app/reports", "role": "viewer", "client_surfaces": []})
    path.write_text(json.dumps(contract), encoding="utf-8")
    assert diff.main([str(root)]) == 1
    assert "('GET', '/app/reports')" in capsys.readouterr().err


def test_a_gateway_route_missing_from_the_contract_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo_copy(tmp_path)
    path = root / "apps" / "shared" / "omni-app-api.contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract["endpoints"] = [item for item in contract["endpoints"] if item["path"] != "/app/sync/state"]
    path.write_text(json.dumps(contract), encoding="utf-8")
    assert diff.main([str(root)]) == 1
    assert "('GET', '/app/sync/state')" in capsys.readouterr().err


def test_a_path_appended_to_a_base_url_is_found() -> None:
    line = "const response = await fetchImpl(`${gatewayUrl.replace(/\\/$/, '')}/admin/session/identity`, {"
    assert diff.BASE_URL_PATH_RE.findall(line) == ["/admin/session/identity"]
