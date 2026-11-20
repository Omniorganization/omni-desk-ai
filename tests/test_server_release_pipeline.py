"""The server release can reach staging, and can never reach customers.

Why: release.yml's only signing job waited for the iOS/Android signing jobs,
so without Apple/Google accounts no signed server release could ever exist
and staging had nothing to deploy. The server-release job removes that wait.
These tests pin what must stay true while it does:

* it signs under the same main-only, release-environment rules as build, with
  no permission build does not already have;
* it builds the wheel once and the image FROM that wheel, with the wheel's
  real sha256 (no placeholder identity);
* its metadata says server-staging-only, it never runs the Real GA gate, and
  promote-production refuses it, while the build job keeps the native binding
  and the Real GA / Customer GA checks unchanged;
* deploy-staging verifies both images against the signed metadata before
  deploying.
"""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
MAIN_ONLY_IF = "github.ref == 'refs/heads/main'"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run") or "") + str(step.get("uses") or "") + json.dumps(step.get("with") or {}) for step in job["steps"])


def test_server_release_signs_under_the_build_jobs_rules_and_permissions() -> None:
    jobs = _workflow("release.yml")["jobs"]
    server, build = jobs["server-release"], jobs["build"]
    assert server["if"] == MAIN_ONLY_IF
    assert server["environment"] == "release"
    assert server["permissions"] == build["permissions"]
    # It must not wait for the native signing jobs, or it is blocked like build.
    assert "needs" not in server
    assert set(build["needs"]) == {"web-admin-release", "desktop-release", "mobile-android-release", "mobile-ios-release"}


def test_server_release_builds_the_wheel_once_and_the_image_from_it() -> None:
    server = _workflow("release.yml")["jobs"]["server-release"]
    text = _run_text(server)
    assert text.count("bash scripts/build_release.sh") == 1
    build_step = next(step for step in server["steps"] if step.get("run") == "bash scripts/build_release.sh")
    assert build_step["env"]["OMNIDESK_RELEASE_DEFER_METADATA"] == "1"
    assert '--build-context wheel="$context"' in text
    assert '--build-arg OMNIDESK_ARTIFACT_SHA256="$artifact_sha"' in text
    assert "set-by-release-metadata" not in text
    assert "-f deploy/sandbox-runner/Dockerfile" in text
    assert "--release-scope server-staging-only" in text
    assert "--sandbox-runner-image-digest" in text


def test_server_release_never_runs_or_satisfies_the_ga_gate() -> None:
    jobs = _workflow("release.yml")["jobs"]
    server_text = _run_text(jobs["server-release"])
    for gate in ("check_real_ga_complete.py", "check_customer_distribution_ga.py", "import_external_ga_evidence.py", "tri-app-native-artifacts"):
        assert gate not in server_text
    # The full release keeps the native binding and both GA gates.
    build_text = _run_text(jobs["build"])
    for gate in ("check_real_ga_complete.py . --write-report", "check_customer_distribution_ga.py", "--output dist/tri-app-native-artifacts.json"):
        assert gate in build_text
    uploads = [step["with"]["name"] for step in jobs["server-release"]["steps"] if "upload-artifact" in str(step.get("uses"))]
    assert uploads == ["server-release-dist"]


def test_production_refuses_server_staging_only_releases_before_deploying() -> None:
    steps = _workflow("promote-production.yml")["jobs"]
    steps = next(iter(steps.values()))["steps"]
    names = [step.get("name", "") for step in steps]
    refuse = names.index("Refuse server/staging-only releases")
    deploy = names.index("Deploy signed artifact to production")
    assert refuse < deploy
    assert '[[ "$scope" != "full" ]]' in steps[refuse]["run"]


def test_staging_verifies_both_images_against_signed_metadata_before_deploying() -> None:
    steps = next(iter(_workflow("deploy-staging.yml")["jobs"].values()))["steps"]
    names = [step.get("name", "") for step in steps]
    verify = names.index("Verify the release images (signatures, attestations, signed digests)")
    assert verify < names.index("Deploy signed artifact to staging")
    run = steps[verify]["run"]
    assert 'cosign verify "$name@$digest"' in run
    assert 'gh attestation verify "oci://$name@$digest"' in run
    assert "sandbox_runner_image.digest" in run
    assert 'test "$(python scripts/read_release_metadata.py dist image.digest)" = "$OMNIDESK_IMAGE_DIGEST"' in run


def test_dockerfile_installs_the_provided_wheel_and_checks_its_sha256() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM scratch AS wheel" in dockerfile
    assert "COPY --from=wheel /*.whl /tmp/" in dockerfile
    assert 'echo "$OMNIDESK_ARTIFACT_SHA256  $wheels" | sha256sum -c -' in dockerfile


def _fake_wheel(dist: Path) -> None:
    dist.mkdir()
    with zipfile.ZipFile(dist / "omnidesk_agent-1.0-py3-none-any.whl", "w") as archive:
        archive.writestr("omnidesk_agent-1.0.dist-info/METADATA", "Name: omnidesk-agent\nVersion: 1.0\n")


def test_metadata_records_scope_and_refuses_a_server_release_without_its_runner(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    _fake_wheel(dist)
    script = ROOT / "scripts" / "write_release_metadata.py"
    digest = "sha256:" + "1" * 64
    missing_runner = subprocess.run([sys.executable, str(script), str(dist), "--image-digest", digest, "--release-scope", "server-staging-only"],
                                    capture_output=True, text=True)
    assert missing_runner.returncode == 2
    ok = subprocess.run([sys.executable, str(script), str(dist), "--image-digest", digest, "--release-scope", "server-staging-only",
                         "--sandbox-runner-image-ref", "ghcr.io/o/r:t", "--sandbox-runner-image-digest", "sha256:" + "2" * 64],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    metadata = json.loads((dist / "release_metadata.json").read_text(encoding="utf-8"))
    assert metadata["release_scope"] == "server-staging-only"
    assert metadata["sandbox_runner_image"] == {"ref": "ghcr.io/o/r:t", "digest": "sha256:" + "2" * 64}
    default = subprocess.run([sys.executable, str(script), str(dist), "--image-digest", digest], capture_output=True, text=True)
    assert default.returncode == 0
    assert json.loads((dist / "release_metadata.json").read_text(encoding="utf-8"))["release_scope"] == "full"
