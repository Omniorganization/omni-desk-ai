"""The watchdog unit and the failure-injection drill plan must match the gateway they protect.

Why: the self-healing drill is release evidence (self_healing_failure_injection).
A watchdog that watches a different config than the gateway reads, restarts
the wrong unit, or a drill whose "bad" config the gateway would accept, would
make the drill either fail for the wrong reason or pass without testing
anything. These checks fail on exactly that drift. Nothing here deploys the
unit or writes evidence; the drill runs only on the real host.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "omnidesk-watchdog.service"
GATEWAY_UNIT = ROOT / "deploy" / "systemd" / "omnidesk-agent.production.service"
PLAN = ROOT / "deploy" / "self-healing" / "drill-plan.bad-config.json"
MAKE_BAD = ROOT / "deploy" / "self-healing" / "make_bad_config.py"
DEPLOY_HELPER = ROOT / "deploy" / "systemd" / "omnidesk-deploy-artifact"


def _exec_start(unit: Path) -> list[str]:
    text = unit.read_text(encoding="utf-8").replace("\\\n", " ")
    line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    return shlex.split(line.split("=", 1)[1])


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == flag]


def test_watchdog_protects_the_config_and_unit_the_gateway_actually_uses() -> None:
    argv = _exec_start(UNIT)
    assert argv[1:3] == ["-m", "omnidesk_agent.self_healing.watchdog"]
    gateway = _exec_start(GATEWAY_UNIT)
    assert _flag_values(argv, "--config") == _flag_values(gateway, "--config")
    # The deploy helper installs and restarts the gateway under this name.
    (service,) = re.findall(r'SERVICE="\$\{OMNIDESK_SYSTEMD_SERVICE:-([A-Za-z0-9_.@-]+)\}"', DEPLOY_HELPER.read_text(encoding="utf-8"))
    assert _flag_values(argv, "--restart-cmd") == ["/usr/bin/systemctl", "restart", f"{service}.service"]
    (health,) = _flag_values(argv, "--health-url")
    assert health.startswith("http://127.0.0.1:") and health.endswith("/health")
    assert _flag_values(argv, "--state-dir") == ["/var/lib/omnidesk-watchdog"]


def test_drill_plan_watches_the_watchdog_log_and_injects_without_a_shell() -> None:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    (injection,) = plan["injections"]
    assert plan["health_url"] == _flag_values(_exec_start(UNIT), "--health-url")[0]
    assert injection["audit_log"] == "/var/lib/omnidesk-watchdog/watchdog.jsonl"
    assert injection["event"] == "recovered"
    assert injection["action"] == "rollback_config_and_restart"
    for step in injection["inject"]:
        assert isinstance(step, list) and step and all(isinstance(arg, str) for arg in step)
        assert Path(step[0]).is_absolute() and Path(step[0]).name not in {"sh", "bash", "dash", "zsh"}
    assert injection["inject"][-1] == ["/usr/bin/systemctl", "restart", "omnidesk-agent.service"]
    # The drill breaks exactly the file the gateway reads, and writes it in place.
    config = _flag_values(_exec_start(GATEWAY_UNIT), "--config")[0]
    generate, write, _restart = injection["inject"]
    assert generate[1].endswith("/make_bad_config.py") and generate[2] == config
    assert write == ["/usr/bin/cp", generate[3], config]
    assert "gateway.allow_local_admin_without_token must be false in production" in injection["description"]


def test_the_bad_config_is_written_in_place_so_the_gateway_can_still_read_it(tmp_path: Path) -> None:
    """Why: the drill runs as root. A writer that replaces the file (install, mv,
    or cp with -p/-a/--remove-destination) leaves a new root-owned file that the
    gateway (User=omnidesk, no capabilities) cannot read. The gateway then dies on
    PermissionError before the production validator runs, and the drill still
    passes while describing a validator refusal. cp into the existing file keeps
    its inode, so owner and mode stay as the host provisioned them."""
    (injection,) = json.loads(PLAN.read_text(encoding="utf-8"))["injections"]
    write = injection["inject"][1]
    live, bad = tmp_path / "config.yaml", tmp_path / "bad-config.yaml"
    live.write_text("gateway: {}\n", encoding="utf-8")
    live.chmod(0o640)
    bad.write_text("gateway:\n  allow_local_admin_without_token: true\n", encoding="utf-8")
    bad.chmod(0o600)
    before = live.stat()
    # The plan's binary on the Linux host; the same tool elsewhere (macOS: /bin/cp).
    binary = write[0] if Path(write[0]).exists() else shutil.which(Path(write[0]).name)
    assert binary, write[0]
    subprocess.run([binary, str(bad), str(live)], check=True)
    after = live.stat()
    assert (after.st_ino, after.st_uid, after.st_gid, after.st_mode) == (before.st_ino, before.st_uid, before.st_gid, before.st_mode)
    assert live.read_text(encoding="utf-8") == bad.read_text(encoding="utf-8")


def test_the_injected_config_is_one_the_production_gateway_refuses_to_boot_with(tmp_path: Path) -> None:
    from omnidesk_agent.config import AppConfig
    from omnidesk_agent.validation.production import validate_production_config

    source = ROOT / "deploy" / "docker" / "config.production.example.yaml"
    bad = tmp_path / "bad-config.yaml"
    subprocess.run([sys.executable, str(MAKE_BAD), str(source), str(bad)], check=True, capture_output=True)
    env = {"OMNIDESK_ENV": "production"}
    issues = validate_production_config(AppConfig.model_validate(yaml.safe_load(bad.read_text(encoding="utf-8"))), env)["issues"]
    assert "gateway.allow_local_admin_without_token must be false in production" in issues
    original = validate_production_config(AppConfig.model_validate(yaml.safe_load(source.read_text(encoding="utf-8"))), env)["issues"]
    assert "gateway.allow_local_admin_without_token must be false in production" not in original


def test_bad_config_generator_never_overwrites_its_source(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("gateway: {}\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(MAKE_BAD), str(config), str(config)], capture_output=True, text=True)
    assert result.returncode == 2
    assert config.read_text(encoding="utf-8") == "gateway: {}\n"


def _watchdog_available() -> bool:
    try:
        return importlib.util.find_spec("omnidesk_agent.self_healing.watchdog") is not None
    except ModuleNotFoundError:  # omnidesk_agent.self_healing is still a single module
        return False


@pytest.mark.skipif(not _watchdog_available(), reason="the watchdog module lands with the self-healing package (#232)")
def test_unit_flags_are_accepted_by_the_watchdog_cli() -> None:
    from omnidesk_agent.self_healing import watchdog

    argv = _exec_start(UNIT)[3:]
    parser_args = {arg for arg in argv if arg.startswith("--")}
    help_text = subprocess.run([sys.executable, "-m", "omnidesk_agent.self_healing.watchdog", "--help"],
                               capture_output=True, text=True, check=True).stdout
    for flag in parser_args:
        assert flag in help_text, flag
    assert hasattr(watchdog, "GatewayWatchdog")
