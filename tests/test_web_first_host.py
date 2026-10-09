from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import web_first_host as host


@pytest.fixture
def isolated_host(tmp_path, monkeypatch):
    """Exercise configuration generation without host writes or real credentials."""
    destination, state = tmp_path / "config", tmp_path / "state"
    paths = {"/etc/omnidesk/web-first": destination, "/var/lib/omnidesk-web-first": state}
    monkeypatch.setattr(host, "Path", lambda value: paths.get(str(value), Path(value)))
    monkeypatch.setattr(host.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host.os, "chown", lambda *_: None)
    monkeypatch.setattr(host.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1001, pw_gid=1001))
    monkeypatch.setenv("HOST_TEST_DSN", "postgresql://fixture:fixture@127.0.0.1/host_test")
    fixture_secret = "test-fixture-not-a-real-secret-" + "x" * 48
    monkeypatch.setattr(host.secrets, "token_urlsafe", lambda _: fixture_secret)
    monkeypatch.setattr(host.Fernet, "generate_key", lambda: fixture_secret.encode())
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    public = SimpleNamespace(public_bytes=lambda *_: b"matching test fixture")
    identity = SimpleNamespace(public_key=lambda: public)
    monkeypatch.setattr(x509, "load_pem_x509_certificate", lambda _: identity)
    monkeypatch.setattr(serialization, "load_pem_private_key", lambda *_args, **_kwargs: identity)
    calls = []
    monkeypatch.setattr(host.subprocess, "run", lambda argv, **_: calls.append(argv))
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    for path in (cert, key):
        path.write_text("Test fixture only; not a certificate or private key")
    args = SimpleNamespace(origin="https://gateway.testmonico.com", user="omnidesk", actor="host-fixture",
                           dsn_env="HOST_TEST_DSN", cert=cert, key=key, model="smollm2:135m",
                           gateway_only=False, web_image_id="sha256:" + "b" * 64)
    return args, destination, state, calls, fixture_secret


@pytest.mark.parametrize("gateway_only", [False, True])
def test_host_generates_only_requested_services_with_matching_model(isolated_host, gateway_only, capsys):
    args, destination, state, calls, fixture_secret = isolated_host
    args.gateway_only = gateway_only
    if gateway_only:
        args.web_image_id = None
    host.init(args)
    config = yaml.safe_load((destination / "config.yaml").read_text())
    assert config["runtime"]["required_ollama_models"] == [args.model]
    assert {p["model"] for p in config["models"]["profiles"].values()} == {args.model}
    assert config["gateway"]["host"] == "127.0.0.1"
    assert config["gateway"]["allow_local_admin_without_token"] is False
    assert config["gateway"]["admin_allowed_ips"] == ["127.0.0.1"]
    assert config["app_sync"]["require_device_signed_requests_in_production"] is True
    assert config["sandbox"]["runner_url"] == "http://127.0.0.1:18890"
    assert config["sandbox"]["docker_network"] == "none"
    services = {p.name for p in destination.glob("*.service")}
    assert services == {"omnidesk-web-gateway.service", "omnidesk-web-runner.service"} | (
        set() if gateway_only else {"omnidesk-web-first.service"})
    assert (destination / "web.env").exists() is not gateway_only
    if not gateway_only:
        assert "OMNI_WEB_IMAGE_ID=" + args.web_image_id in (destination / "web.env").read_text()
    nginx = (destination / "nginx.conf").read_text()
    assert "proxy_pass http://127.0.0.1:" + ("18789;" if gateway_only else "13000;") in nginx
    assert "@" not in nginx
    assert state.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir())
    assert calls == [["openssl", "verify", "-verify_hostname", "gateway.testmonico.com",
                      "-untrusted", str(args.cert), str(args.cert)]]
    assert fixture_secret not in capsys.readouterr().out
    original = (destination / "runtime.env").read_bytes()
    with pytest.raises(ValueError, match="must not be overwritten"):
        host.init(args)
    assert (destination / "runtime.env").read_bytes() == original


@pytest.mark.parametrize("image", [None, "latest", "sha256:" + "b" * 63])
def test_default_host_refuses_missing_or_mutable_web_image_before_writing(isolated_host, image):
    args, destination, state, *_ = isolated_host
    args.web_image_id = image
    with pytest.raises(ValueError, match="immutable Web image"):
        host.init(args)
    assert not destination.exists()
    assert not state.exists()


def test_gateway_only_cli_accepts_no_web_image(monkeypatch):
    calls = []
    monkeypatch.setattr(host, "init", calls.append)
    argv = ["host", "init", "--origin", "https://gateway.testmonico.com", "--user", "omnidesk",
            "--actor", "host-fixture", "--cert", "/fixture/cert", "--key", "/fixture/key"]
    monkeypatch.setattr(sys, "argv", argv + ["--gateway-only"])
    host.main()
    assert calls[0].gateway_only is True and calls[0].web_image_id is None
    for extra in ([], ["--gateway-only", "--web-image-id", "sha256:" + "b" * 64]):
        monkeypatch.setattr(sys, "argv", argv + extra)
        with pytest.raises(SystemExit) as error:
            host.main()
        assert error.value.code == 2
    assert len(calls) == 1


def test_gateway_ingress_allows_only_monico_routes_and_preserves_signature_paths():
    template = Path("deploy/web-first/nginx.gateway-only.conf.template").read_text()
    patterns = [re.compile(value) for value in re.findall(r'location ~ "([^"]+)"', template)]
    allowed = ["/admin/session/identity", "/api/chat/stream", "/app/bootstrap", "/app/projects",
               "/app/projects/p_1", "/app/conversations", "/app/conversations/c_1/messages",
               "/app/conversations/c_1/ask", "/app/approvals", "/app/approvals/a_1/decide",
               "/app/notifications", "/app/devices/register", "/app/devices/enrollment/start",
               *["/app/devices/enrollment/e_1/" + step for step in ("complete", "challenge", "verify")],
               "/app/tasks/t_1", "/app/projects/" + "a" * 160]
    denied = ["/", "/ready", "/admin/status", "/admin/session/identity/extra", "/api/chat",
              "/api/chat/stream/extra", "/app/ws", "/app/sync", "/app/desktop/claim",
              "/app/push/dispatch", "/app/devices/revoke", "/app/tasks/t_1/status", "/app/tasks",
              "/app/projects/", "/app/projects/p_1/extra", "/app/projects/" + "a" * 161,
              "/APP/projects", "/app/projects/../devices/revoke"]
    for path in allowed:
        assert any(pattern.fullmatch(path) for pattern in patterns), path
    for path in denied:
        assert not any(pattern.fullmatch(path) for pattern in patterns), path
    assert "location / { return 404; }" in template
    assert len(re.findall(r"^\s*proxy_pass\s+", template, re.MULTILINE)) == 1
    assert "proxy_pass http://127.0.0.1:18789;" in template  # No URI rewrite of signed requests.
    assert "proxy_http_version 1.1;" in template
    assert "proxy_buffering off;" in template and "proxy_cache off;" in template
    assert not re.search(r"18890|11434|5432|13000", template)


@pytest.mark.asyncio
async def test_gateway_proxy_headers_keep_loopback_allowlist_and_require_valid_token(monkeypatch):
    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from omnidesk_agent.security.admin_auth import AdminAuth

    template = Path("deploy/web-first/nginx.gateway-only.conf.template").read_text()
    assert 'proxy_set_header X-Forwarded-For "";' in template  # nginx removes even client-supplied XFF.
    monkeypatch.setenv("HOST_TEST_VIEWER", "test-fixture-viewer-token")
    auth = AdminAuth(admin_token_env="HOST_TEST_ADMIN", viewer_token_env="HOST_TEST_VIEWER",
                     operator_token_env="HOST_TEST_OPERATOR", owner_token_env="HOST_TEST_OWNER",
                     allow_local_without_token=False, allowed_ips=["127.0.0.1"])
    decisions = []

    async def app(scope, _receive, _send):
        decisions.append(await auth.verify_request(Request(scope)))

    middleware = ProxyHeadersMiddleware(app)  # Same trusted-loopback default as uvicorn.run.
    for forwarded, token in ((None, "test-fixture-viewer-token"), (None, "wrong"),
                             (b"203.0.113.7", "test-fixture-viewer-token")):
        headers = [(b"authorization", ("Bearer " + token).encode()), (b"x-forwarded-proto", b"https")]
        if forwarded:
            headers.append((b"x-forwarded-for", forwarded))
        scope = {"type": "http", "method": "GET", "path": "/admin/session/identity", "query_string": b"",
                 "scheme": "http", "server": ("127.0.0.1", 18789), "client": ("127.0.0.1", 40000), "headers": headers}
        await middleware(scope, None, None)
    assert decisions[0].ok and decisions[0].role == "viewer"
    assert not decisions[1].ok and decisions[1].reason == "missing or invalid admin token"
    assert not decisions[2].ok and decisions[2].reason == "client IP is not allowed"
