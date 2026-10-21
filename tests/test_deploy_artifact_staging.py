"""systemd artifact deploys must not hash one file and install another.

Regression for the review finding where scripts/deploy_artifact.sh uploaded the
wheel to the predictable /tmp/omnidesk-release-artifact-<sha>.whl and the root
helper hashed that path, then copied it again before installing (TOCTOU).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

CLIENT = Path("scripts/deploy_artifact.sh").resolve()
HELPER = Path("deploy/systemd/omnidesk-deploy-artifact").resolve()
WHEEL_NAME = "omnidesk_agent-1.2.3-py3-none-any.whl"
GOOD = b"good wheel bytes"
EVIL = b"attacker wheel bytes"
REMOTE_DIR = "/tmp/omnidesk-deploy.AbCdEf1234"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bash_supports_at_q() -> bool:
    # The helper's health check uses ${var@Q} (bash >= 4.4). macOS ships bash 3.2,
    # where everything up to and including the install still runs.
    probe = subprocess.run(["bash", "-c", 'x=1; echo "${x@Q}"'], capture_output=True, text=True)
    return probe.returncode == 0


def _write_exe(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


# --------------------------------------------------------------------------
# Client: scripts/deploy_artifact.sh --mode systemd
# --------------------------------------------------------------------------


def _client_env(tmp_path: Path, mktemp_output: str = REMOTE_DIR, sudo_exit: int = 0) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "remote.log"
    _write_exe(
        bin_dir / "ssh",
        f"""printf 'ssh %s\\n' "${{@: -1}}" >> {str(log)!r}
case "${{@: -1}}" in
  *mktemp*) printf '%s\\n' {mktemp_output!r} ;;
  sudo*) exit {sudo_exit} ;;
esac
""",
    )
    _write_exe(bin_dir / "scp", f"""printf 'scp %s %s\\n' "$1" "$2" >> {str(log)!r}\n""")
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "OMNIDESK_DEPLOY_HOST": "deploy.example",
        "OMNIDESK_DEPLOY_USER": "deployer",
    }
    return env, log


def _run_client(tmp_path: Path, env: dict[str, str], name: str = WHEEL_NAME) -> subprocess.CompletedProcess[str]:
    artifact = tmp_path / "dist" / name
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_bytes(GOOD)
    return subprocess.run(
        ["bash", str(CLIENT), "--artifact", str(artifact), "--environment", "staging", "--mode", "systemd",
         "--expected-version", "1.2.3", "--artifact-sha256", _sha(GOOD)],
        env=env, capture_output=True, text=True, cwd=tmp_path,
    )


def test_systemd_client_stages_in_a_fresh_private_remote_dir(tmp_path):
    env, log = _client_env(tmp_path)
    result = _run_client(tmp_path, env)
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    assert "/tmp/omnidesk-release-artifact-" not in "\n".join(lines)
    assert lines[0] == "ssh umask 077 && mktemp -d /tmp/omnidesk-deploy.XXXXXXXXXX"
    remote_artifact = f"{REMOTE_DIR}/{WHEEL_NAME}"
    assert lines[1].endswith(f" deployer@deploy.example:{remote_artifact}")
    assert lines[2].startswith(f"ssh sudo '/usr/local/bin/omnidesk-deploy-artifact' --artifact '{remote_artifact}' --sha256 '{_sha(GOOD)}'")
    assert lines[3] == f"ssh rm -rf -- '{REMOTE_DIR}'"
    assert len(lines) == 4


def test_systemd_client_removes_the_remote_dir_when_the_helper_fails(tmp_path):
    env, log = _client_env(tmp_path, sudo_exit=65)
    result = _run_client(tmp_path, env)
    assert result.returncode == 65
    assert log.read_text(encoding="utf-8").splitlines()[-1] == f"ssh rm -rf -- '{REMOTE_DIR}'"


@pytest.mark.parametrize(
    "mktemp_output",
    [
        "/tmp/omnidesk-release-artifact-x",
        "/tmp/omnidesk-deploy.short",
        "/tmp/omnidesk-deploy.AbCdEf1234/../../etc",
        "/tmp/omnidesk-deploy.AbCdEf123'; id; '",
        "",
    ],
)
def test_systemd_client_rejects_an_unexpected_remote_dir(tmp_path, mktemp_output):
    env, log = _client_env(tmp_path, mktemp_output=mktemp_output)
    result = _run_client(tmp_path, env)
    assert result.returncode == 65
    assert "unexpected remote staging directory" in result.stderr
    assert [line for line in log.read_text(encoding="utf-8").splitlines() if not line.startswith("ssh umask")] == []


@pytest.mark.parametrize("name", ["artifact.whl.tar", "x y-1-py3-none-any.whl", "-rf.whl", "a'b-1-py3-none-any.whl"])
def test_systemd_client_requires_a_wheel_file_name(tmp_path, name):
    env, log = _client_env(tmp_path)
    result = _run_client(tmp_path, env, name=name)
    assert result.returncode == 64
    assert "must be a wheel file name" in result.stderr
    assert not log.exists()


# --------------------------------------------------------------------------
# Root helper: deploy/systemd/omnidesk-deploy-artifact
# --------------------------------------------------------------------------


class Helper:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.install_dir = tmp_path / "opt" / "omnidesk"
        self.upload_dir = tmp_path / "upload"
        self.upload_dir.mkdir(mode=0o700)
        self.artifact = self.upload_dir / WHEEL_NAME
        self.artifact.write_bytes(GOOD)
        self.pip_log = tmp_path / "pip.log"
        self.installed = tmp_path / "installed"
        self.installed.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_exe(bin_dir / "systemctl", "exit 0\n")
        venv_bin = self.install_dir / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        # Fake venv python: record which file pip installs and its bytes at install time.
        _write_exe(
            venv_bin / "python",
            f"""if [[ "$1 $2" == "-m pip" ]]; then
  wheel="${{@: -1}}"
  n=$(wc -l < {str(self.pip_log)!r} 2>/dev/null || echo 0)
  printf '%s\\n' "$wheel" >> {str(self.pip_log)!r}
  cp -- "$wheel" {str(self.installed)!r}/$((n + 0))
  exit 0
fi
if [[ "$1" == "-" ]]; then cat > /dev/null; exit 0; fi
exit 2
""",
        )
        self.bin_dir = bin_dir
        self.env = {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "OMNIDESK_INSTALL_DIR": str(self.install_dir),
            "OMNIDESK_RUNTIME_ENV_FILE": str(tmp_path / "etc" / "runtime.env"),
        }

    def run(self, *, artifact: Path | None = None, sha: str | None = None) -> subprocess.CompletedProcess[str]:
        # cwd: bash 3.2 can fall back to the working directory for here-document files.
        return subprocess.run(
            ["bash", str(HELPER), "--artifact", str(artifact or self.artifact), "--sha256", sha or _sha(GOOD),
             "--expected-version", "1.2.3"],
            env=self.env, capture_output=True, text=True, cwd=self.tmp,
        )

    def installs(self) -> list[tuple[str, bytes]]:
        if not self.pip_log.exists():
            return []
        paths = self.pip_log.read_text(encoding="utf-8").splitlines()
        return [(path, (self.installed / str(i)).read_bytes()) for i, path in enumerate(paths)]

    def staging_dirs(self) -> list[Path]:
        return sorted(self.install_dir.glob(".deploy-staging.*"))


def test_helper_installs_the_verified_copy_even_if_the_upload_is_swapped_after_hashing(tmp_path):
    helper = Helper(tmp_path)
    real_sha256sum = shutil.which("sha256sum")
    assert real_sha256sum, "sha256sum is required"
    evil = tmp_path / "evil.whl"
    evil.write_bytes(EVIL)
    # Attacker who can still write the uploaded path swaps it right after the helper hashes.
    _write_exe(
        helper.bin_dir / "sha256sum",
        f"""{real_sha256sum!r} "$@"
cp -- {str(evil)!r} {str(helper.artifact)!r}
""",
    )
    result = helper.run()
    assert helper.artifact.read_bytes() == EVIL  # the race really happened
    installs = helper.installs()
    assert len(installs) == 1, result.stderr
    installed_path, installed_bytes = installs[0]
    assert installed_bytes == GOOD
    assert installed_path != str(helper.artifact)
    assert Path(installed_path).name == WHEEL_NAME  # pip only accepts wheel file names
    assert Path(installed_path).parent.parent == helper.install_dir
    retained = helper.install_dir / "artifacts" / f"1.2.3-{_sha(GOOD)}" / WHEEL_NAME
    assert retained.read_bytes() == GOOD
    assert os.readlink(helper.install_dir / "artifacts" / "current.whl") == str(retained)
    assert helper.staging_dirs() == []
    if _bash_supports_at_q():
        assert result.returncode == 0, result.stderr
        assert f"OMNIDESK_ARTIFACT_SHA256={_sha(GOOD)}" in (tmp_path / "etc" / "runtime.env").read_text(encoding="utf-8")


def test_helper_rejects_a_symlinked_upload(tmp_path):
    helper = Helper(tmp_path)
    target = tmp_path / "elsewhere.whl"
    target.write_bytes(GOOD)
    link = helper.upload_dir / ("link-" + WHEEL_NAME)
    link.symlink_to(target)
    result = helper.run(artifact=link)
    assert result.returncode == 66
    assert "must not be a symlink" in result.stderr
    assert helper.installs() == []


def test_helper_rejects_a_non_regular_upload(tmp_path):
    helper = Helper(tmp_path)
    directory = helper.upload_dir / WHEEL_NAME.replace(".whl", "-dir.whl")
    directory.mkdir()
    result = helper.run(artifact=directory)
    assert result.returncode == 66
    assert helper.installs() == []


def test_helper_rejects_an_upload_swapped_for_a_symlink_during_the_copy(tmp_path):
    helper = Helper(tmp_path)
    real_cp = shutil.which("cp")
    other = tmp_path / "root-only-secret"
    other.write_bytes(GOOD)
    marker = tmp_path / "swapped"
    # Swap the upload for a symlink between the helper's checks and its copy.
    _write_exe(
        helper.bin_dir / "cp",
        f"""if [[ ! -e {str(marker)!r} && "${{@: -2:1}}" == {str(helper.artifact)!r} ]]; then
  : > {str(marker)!r}
  rm -f -- {str(helper.artifact)!r}
  ln -s {str(other)!r} {str(helper.artifact)!r}
fi
exec {real_cp!r} "$@"
""",
    )
    result = helper.run()
    assert marker.exists()
    assert result.returncode == 66
    assert "symlink or non-regular file" in result.stderr
    assert helper.installs() == []
    assert helper.staging_dirs() == []


def test_helper_rejects_a_hash_mismatch_and_cleans_up(tmp_path):
    helper = Helper(tmp_path)
    result = helper.run(sha="0" * 64)
    assert result.returncode == 65
    assert "artifact sha256 mismatch" in result.stderr
    assert helper.installs() == []
    assert helper.staging_dirs() == []
    assert not (helper.install_dir / "artifacts" / "current.whl").exists()


@pytest.mark.parametrize("name", ["artifact.zip", "current.whl.bak", ".hidden.whl"])
def test_helper_requires_a_wheel_file_name(tmp_path, name):
    helper = Helper(tmp_path)
    renamed = helper.upload_dir / name
    helper.artifact.rename(renamed)
    result = helper.run(artifact=renamed)
    assert result.returncode == 64
    assert "wheel file name" in result.stderr
    assert helper.installs() == []


def test_helper_keeps_the_existing_input_validation():
    text = HELPER.read_text(encoding="utf-8")
    assert '"$EXPECTED_VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$' in text
    assert "is_sha256 \"$EXPECTED_SHA\"" in text
    assert "is_image_digest \"$IMAGE_DIGEST\"" in text
    # Hash and install the root-only copy, never the uploaded path.
    assert 'sha256sum "$STAGED_WHEEL"' in text
    assert 'pip install --no-deps --force-reinstall "$STAGED_WHEEL"' in text
    assert 'sha256sum "$ARTIFACT"' not in text
