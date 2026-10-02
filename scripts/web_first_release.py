"""Package and verify a scoped Web candidate without changing aggregate Customer GA."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

SCHEMA = "omnidesk-web-first-candidate/v1"
PAYLOADS = {"web-image.tar.gz", "runtime-source.tar.gz", "acceptance.json", "browser.json"}
REQUIRED_WEB_CHECKS = {
    "owner authenticates and browser P-256 enrollment succeeds",
    "all session cookies are HttpOnly Secure SameSite Strict",
    "owner signed browser approval succeeds",
    "browser replay rejected by durable backend nonce",
    "browser calls real free model and persists audited answer",
    "authenticated stream workspace receives actual SSE",
    "stream workspace labels audited delivery truthfully",
    "HTTPS browser trusts test CA without TLS bypass",
    "strict CSP and Trusted Types remain enabled",
    "CSP nonce differs between responses",
    "no secrets or private keys stored in browser storage",
    "reload restores verified session and PostgreSQL project",
    "viewer write rejected by backend RBAC",
    "missing CSRF rejected before backend mutation",
    "logout blocks subsequent backend access",
    "expired session cannot restore authenticated state",
    "no browser runtime errors",
}
REQUIRED_RUNTIME_CHECKS = {
    "production guards pass without disabling policy",
    "device nonce replay rejected after restart",
    "device nonce replay rejected after database restore",
    "actual resource limits enforced",
    "actual strict sandbox execution",
    "production web image browser acceptance",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(directory: Path, expected_sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("Expected immutable checkout SHA is required")
    manifest = json.loads((directory / "web-candidate.json").read_text())
    if manifest.get("schema") != SCHEMA or manifest.get("scope") != "web-only":
        raise ValueError("Unsupported release scope")
    if manifest.get("checkout_sha") != expected_sha:
        raise ValueError("Candidate checkout SHA mismatch")
    if manifest.get("customer_ga") is not False or manifest.get("persistent_deployment") is not False:
        raise ValueError("Candidate must not claim production or Customer GA")
    if manifest.get("native_distribution") != []:
        raise ValueError("Native artifacts must not be distributed in this scope")
    items = manifest.get("artifacts")
    if not isinstance(items, list) or len(items) != len(PAYLOADS) or {i.get("name") for i in items} != PAYLOADS:
        raise ValueError("Missing, duplicate or unexpected Web candidate artifacts")
    for item in items:
        path = directory / item["name"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("Candidate payload must be a nonempty regular file")
        if digest(path) != item.get("sha256") or path.stat().st_size != item.get("bytes"):
            raise ValueError("Candidate artifact digest/size mismatch: " + path.name)
    acceptance = json.loads((directory / "acceptance.json").read_text())
    browser = json.loads((directory / "browser.json").read_text())
    if acceptance.get("checkout_sha") != expected_sha or acceptance.get("ok") is not True:
        raise ValueError("Runtime evidence is not successful on the candidate SHA")
    for report in (acceptance, browser):
        if report.get("checkout_sha") != expected_sha or report.get("run_id") != manifest.get("run_id"):
            raise ValueError("Candidate and evidence source/run binding mismatch")
        checks = report.get("checks")
        if report.get("ok") is not True or not isinstance(checks, list) or not checks or any(c.get("ok") is not True for c in checks):
            raise ValueError("Acceptance includes incomplete or failed checks")
        if report.get("customer_ga") is not False or report.get("persistent_deployment") is not False:
            raise ValueError("Ephemeral evidence cannot establish production")
    if manifest.get("run_id") != acceptance.get("run_id"):
        raise ValueError("Candidate and acceptance run binding mismatch")
    for report, required in ((acceptance, REQUIRED_RUNTIME_CHECKS), (browser, REQUIRED_WEB_CHECKS)):
        observed = {check.get("name") for check in report["checks"] if check.get("ok") is True}
        if not required <= observed:
            raise ValueError("Required release behavior was not verified")
    if browser.get("browser_errors") != []:
        raise ValueError("Browser errors remain")
    image_id = manifest.get("web_image_id", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Missing immutable Web image identity")
    return manifest


def package(directory: Path, image: str) -> dict:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("Package build is cloud-only")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    acceptance = json.loads((directory / "acceptance.json").read_text())
    if acceptance.get("ok") is not True or acceptance.get("checkout_sha") != sha:
        raise ValueError("Exact checkout acceptance must pass before packaging")
    inspection = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
    if inspection["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != sha:
        raise ValueError("Web image revision must match tested checkout")
    raw = directory / "web-image.tar"
    subprocess.run(["docker", "save", "-o", str(raw), image], check=True)
    with raw.open("rb") as source, gzip.open(directory / "web-image.tar.gz", "wb", compresslevel=6) as target:
        shutil.copyfileobj(source, target)
    raw.unlink()
    subprocess.run(["git", "archive", "--format=tar.gz", "--output=" + str(directory / "runtime-source.tar.gz"), sha,
                    "omnidesk_agent", "pyproject.toml", "README.md", "requirements.bootstrap.lock",
                    "requirements.runtime.lock", "requirements.enterprise.lock", "deploy/web-first",
                    "deploy/docker/config.production.example.yaml", "scripts/web_first_release.py",
                    "scripts/web_first_host.py", "docs/WEB_FIRST_RELEASE.md",
                    "docs/runbooks/PLANNER_MEMORY_ISOLATION.md",
                    "docs/runbooks/POSTGRES_RUNTIME_LIFECYCLE.md"], check=True)
    manifest = {
        "schema": SCHEMA, "scope": "web-only", "release_tier": "controlled-pilot-candidate",
        "checkout_sha": sha, "run_id": os.environ["GITHUB_RUN_ID"],
        "repository": os.environ["GITHUB_REPOSITORY"],
        "web_image_id": inspection["Id"], "native_distribution": [],
        "customer_ga": False, "persistent_deployment": False,
        "artifacts": [{"name": name, "sha256": digest(directory / name), "bytes": (directory / name).stat().st_size}
                      for name in sorted(PAYLOADS)],
        "remaining": ["independent reviewed main", "protected release provenance", "persistent host",
                      "production TLS and ingress", "production browser/model acceptance", "production recovery and soak"],
    }
    (directory / "web-candidate.json").write_text(json.dumps(manifest, indent=2) + "\n")
    verify(directory, sha)
    hashes = [f"{digest(path)}  {path.name}" for path in sorted(directory.iterdir())
              if path.is_file() and path.name != "SHA256SUMS"]
    (directory / "SHA256SUMS").write_text("\n".join(hashes) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    pack = actions.add_parser("package")
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--image", required=True)
    check = actions.add_parser("verify")
    check.add_argument("--directory", type=Path, required=True)
    check.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    if args.action == "package":
        manifest = package(args.output, args.image)
    else:
        manifest = verify(args.directory, args.expected_sha)
    print(json.dumps({"verified": True, "scope": manifest["scope"], "checkout_sha": manifest["checkout_sha"],
                      "customer_ga": False, "persistent_deployment": False}, sort_keys=True))


if __name__ == "__main__":
    main()
