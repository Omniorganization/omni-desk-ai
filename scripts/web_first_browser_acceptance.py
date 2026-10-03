"""Real production Web image acceptance, restricted to ephemeral GitHub runners."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time


def run_browser_acceptance(*, output: Path, root: Path, env: dict, launch, request) -> dict:
    from playwright.sync_api import expect, sync_playwright

    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Browser acceptance requires GitHub Actions")
    report = {"ok": False, "scope": "web-only", "persistent_deployment": False,
              "customer_ga": False, "model_scope": "real free ephemeral Ollama smollm2:135m",
              "checks": [], "browser_errors": [], "browser_console": [], "browser": "Chromium",
              "run_id": os.environ["GITHUB_RUN_ID"],
              "checkout_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}

    def check(name, condition):
        report["checks"].append({"name": name, "ok": bool(condition)})
        print(f"Browser: {name}: {bool(condition)}", flush=True)
        if not condition:
            raise RuntimeError("Browser acceptance failed: " + name)

    cert = Path(env["ACCEPTANCE_TLS_CERT"])
    cert.chmod(0o644)
    # Trust only the generated one-day CI CA, in this disposable cloud machine.
    subprocess.run(["sudo", "cp", str(cert), "/usr/local/share/ca-certificates/omnidesk-private-ci.crt"], check=True)
    subprocess.run(["sudo", "update-ca-certificates"], check=True, capture_output=True)
    nss = Path.home() / ".pki/nssdb"
    nss.mkdir(parents=True, exist_ok=True)
    if not (nss / "cert9.db").exists():
        subprocess.run(["certutil", "-N", "--empty-password", "-d", "sql:" + str(nss)], check=True)
    subprocess.run(["certutil", "-A", "-n", "omnidesk-private-ci", "-t", "C,,",
                    "-d", "sql:" + str(nss), "-i", str(cert)], check=True)
    origin = "https://127.0.0.1:18443"
    env["OMNI_WEB_PUBLIC_ORIGIN"] = origin
    launch(["docker", "run", "--rm", "--network", "host", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:size=64m,noexec,nosuid,nodev",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
            "-e", "HOSTNAME=127.0.0.1", "-e", "PORT=13000",
            "-e", "OMNI_GATEWAY_URL=https://127.0.0.1:18789",
            "-e", "OMNI_WEB_PUBLIC_ORIGIN=" + origin,
            "-e", "NODE_EXTRA_CA_CERTS=/run/ci-ca.crt",
            "-v", str(cert) + ":/run/ci-ca.crt:ro", env["WEB_FIRST_IMAGE"]], "web-image")
    nginx = root / "nginx.conf"
    nginx.write_text(f'''pid {root}/nginx.pid;
error_log {root}/nginx-error.log warn;
events {{ worker_connections 128; }}
http {{
  access_log off;
  client_body_temp_path {root}/nginx-body;
  proxy_temp_path {root}/nginx-proxy;
  server {{
    listen 127.0.0.1:18443 ssl;
    server_name localhost;
    ssl_certificate {cert};
    ssl_certificate_key {env['ACCEPTANCE_TLS_KEY']};
    client_max_body_size 1m;
    location / {{
      proxy_pass http://127.0.0.1:13000;
      proxy_set_header Host 127.0.0.1:18443;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-Host 127.0.0.1:18443;
      proxy_buffering off;
      proxy_read_timeout 180s;
    }}
  }}
}}
''')
    launch(["nginx", "-c", str(nginx), "-g", "daemon off;"], "web-ingress")
    for _ in range(12):
        ready = subprocess.run(["curl", "--silent", "--fail", "--head", "--connect-timeout", "3",
                                "--max-time", "5", "--cacert", str(cert), origin],
                               capture_output=True, timeout=10)
        if ready.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Production Web HTTPS image startup timeout")

    status, conversation = request("/app/conversations", role="operator", payload={"title": "Web acceptance approvals"})
    check("real PostgreSQL conversation fixture", status == 200)
    status, seeded = request("/app/conversations/" + conversation["conversation"]["conversation_id"] + "/messages",
                             role="operator", payload={"content": "Cloud browser approval fixture", "risk": "high"})
    check("real pending approval fixture", status == 200 and seeded.get("approval"))
    approval_id = seeded["approval"]["approval_id"]

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            report["browser_version"] = browser.version
            context = browser.new_context(ignore_https_errors=False, viewport={"width": 1440, "height": 1080})
            page = context.new_page()
            page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
            page.on("console", lambda message: report["browser_console"].append(message.text) if message.type == "error" else None)
            response = page.goto(origin)
            csp = response.headers.get("content-security-policy", "")
            report["csp"] = csp
            check("HTTPS browser trusts test CA without TLS bypass", response.status == 200)
            check("strict CSP and Trusted Types remain enabled", "require-trusted-types-for 'script'" in csp
                  and "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp)
            # Capture pre-login rendering diagnostics without session/credential content.
            report["initial_labels"] = page.locator("label").all_text_contents()
            report["initial_body_text"] = page.locator("body").inner_text()[:6000]
            (output / "browser-initial.html").write_text(page.content())
            page.screenshot(path=str(output / "browser-initial.png"), full_page=True)
            for control in page.locator(".setting-row").all():
                if control.get_attribute("data-implemented") != "true":
                    expect(control).to_be_disabled()
            page.get_by_label("Session Token", exact=True).fill("invalid-token")
            page.get_by_role("button", name="登录并连接", exact=True).click()
            expect(page.locator(".error-banner")).to_contain_text("invalid gateway token")
            check("invalid login visibly rejected", True)
            page.get_by_label("Session Token", exact=True).fill(env["OMNIDESK_OWNER_TOKEN"])
            page.get_by_role("button", name="登录并连接", exact=True).click()
            expect(page.locator(".profile-card small")).to_contain_text("owner")
            expect(page.locator(".error-banner")).to_have_count(0)
            expect(page.get_by_role("button", name="批准", exact=True)).to_be_enabled(timeout=30000)
            check("owner authenticates and browser P-256 enrollment succeeds", True)
            cookie_metadata = [{key: cookie[key] for key in ("name", "httpOnly", "secure", "sameSite")}
                               for cookie in context.cookies()]
            report["cookie_metadata"] = cookie_metadata
            check("all session cookies are HttpOnly Secure SameSite Strict", len(cookie_metadata) == 5
                  and all(c["httpOnly"] and c["secure"] and c["sameSite"] == "Strict" for c in cookie_metadata))
            check("session token inaccessible to page JavaScript", page.evaluate("document.cookie") == "")
            check("login token removed from form memory", page.get_by_label("Session Token", exact=True).input_value() == "")
            name = "Web acceptance " + str(int(time.time()))
            page.get_by_placeholder("输入项目名称后创建").fill(name)
            page.get_by_role("button", name="创建", exact=True).click()
            expect(page.locator(".project-row").filter(has_text=name)).to_be_visible()
            stored = page.evaluate("async () => (await fetch('/api/omni/projects')).json()")
            check("project creation is committed beyond optimistic UI", any(p.get("name") == name for p in stored.get("projects", [])))
            check("interactive project create persists through real Gateway", True)

            with page.expect_response(lambda r: "/approvals/" + approval_id + "/decide" in r.url) as decision:
                page.get_by_role("button", name="批准", exact=True).click()
            decided = decision.value
            check("owner signed browser approval succeeds", decided.status == 200 and decided.json().get("ok"))
            signed_request = decided.request
            headers = {k: v for k, v in signed_request.all_headers().items()
                       if k.startswith("x-omnidesk-") or k in ("x-csrf-token", "content-type")}
            check("actual asymmetric device signature sent", "x-omnidesk-device-signature" in headers)
            replay = page.evaluate('''async ({url,body,headers}) => {
              const r = await fetch(url, {method:'POST',body,headers});
              return {status:r.status, body:await r.json()};
            }''', {"url": signed_request.url, "body": signed_request.post_data, "headers": headers})
            check("browser replay rejected by durable backend nonce", replay["status"] == 401
                  and "nonce" in json.dumps(replay["body"]).lower())
            report["signed_replay_status"] = replay["status"]
            # A real model, no credentials and no provider stub.
            page.locator(".composer-card textarea").fill("Reply briefly with one greeting.")
            with page.expect_response(lambda r: r.url.endswith("/ask"), timeout=120000) as answer:
                page.locator(".send-button").click()
            data = answer.value.json()
            report["model_response"] = {"status": answer.value.status,
                                        "detail": data.get("detail"),
                                        "provider": data.get("assistant_message", {}).get("model_provider")}
            check("browser calls real free model and persists audited answer", answer.value.status == 200
                  and data.get("assistant_message", {}).get("content")
                  and data["assistant_message"].get("model_provider") == "ollama" and data.get("audit_trace_id"))
            report["model"] = {"provider": data["assistant_message"].get("model_provider"),
                               "model": data["assistant_message"].get("model_name"),
                               "trace_present": bool(data.get("audit_trace_id"))}
            expect(page.locator(".send-button")).to_be_enabled(timeout=30000)
            page.screenshot(path=str(output / "browser-owner.png"), full_page=True)
            page.goto(origin + "/stream")
            expect(page.get_by_text("已连接 · isolated-ci-actor · owner", exact=True)).to_be_visible()
            page.get_by_placeholder("输入问题", exact=True).fill("Reply briefly with one greeting.")
            with page.expect_response(lambda r: r.url.endswith("/api/omni/chat/stream"), timeout=120000) as streamed:
                page.get_by_role("button", name="开始生成", exact=True).click()
            check("authenticated stream workspace receives actual SSE", streamed.value.status == 200
                  and "text/event-stream" in streamed.value.headers.get("content-type", ""))
            expect(page.get_by_text("已完成 · 审计后流式交付", exact=True)).to_be_visible(timeout=120000)
            check("stream workspace labels audited delivery truthfully", True)
            page.screenshot(path=str(output / "browser-stream.png"), full_page=True)
            page.goto(origin)
            page.reload()
            expect(page.get_by_text("session ready", exact=True)).to_be_visible()
            page.get_by_role("button", name="连接应用", exact=False).click()
            expect(page.locator(".project-row").filter(has_text=name)).to_be_visible()
            check("reload restores verified session and PostgreSQL project", True)
            refreshed_csp = page.evaluate("async () => (await fetch('/')).headers.get('content-security-policy')")
            check("CSP nonce differs between responses", csp != refreshed_csp and "nonce-" in refreshed_csp)
            storage = page.evaluate("async () => ({local:localStorage.length,session:sessionStorage.length,dbs:await indexedDB.databases()})")
            check("no secrets or private keys stored in browser storage", storage["local"] == 0
                  and storage["session"] == 0 and storage["dbs"] == [])
            expect(page.locator(".send-button")).to_be_enabled()
            page.get_by_role("button", name="退出登录", exact=True).click()
            expect(page.get_by_text("未建立 session", exact=True)).to_be_visible()
            check("logout removes every session cookie", context.cookies() == [])
            denied = page.evaluate("async () => {const r=await fetch('/api/omni/bootstrap');return r.status}")
            check("logout blocks subsequent backend access", denied == 401)

            page.get_by_label("Session Token", exact=True).fill(env["OMNIDESK_VIEWER_TOKEN"])
            page.get_by_label("Role", exact=True).select_option("owner")
            page.get_by_role("button", name="登录并连接", exact=True).click()
            expect(page.locator(".profile-card small")).to_contain_text("viewer")
            expect(page.locator(".error-banner")).to_have_count(0)
            expect(page.locator(".project-row").filter(has_text=name)).to_be_visible()
            check("viewer login reads data without privileged enrollment", True)
            check("untrusted role selection cannot elevate viewer", page.locator(".send-button").is_disabled())
            current = page.evaluate("async () => (await fetch('/api/session/current')).json()")
            csrf = current["csrfToken"]
            denied = page.evaluate('''async (csrf) => {
              const r=await fetch('/api/omni/projects',{method:'POST',headers:{'content-type':'application/json','x-csrf-token':csrf},body:JSON.stringify({name:'viewer-forbidden'})});
              return r.status;
            }''', csrf)
            check("viewer write rejected by backend RBAC", denied == 403)
            no_csrf = page.evaluate("async () => (await fetch('/api/omni/projects',{method:'POST',headers:{'content-type':'application/json'},body:'{}'})).status")
            check("missing CSRF rejected before backend mutation", no_csrf == 403)
            # Expiry is tested with the actual browser cookie expiry mechanism.
            expired = context.cookies()
            for cookie in expired:
                cookie["expires"] = int(time.time()) - 60
            context.add_cookies(expired)
            page.reload()
            expect(page.get_by_text("未建立 session", exact=True)).to_be_visible()
            check("expired session cannot restore authenticated state", page.evaluate("async () => (await fetch('/api/session/current')).status") == 401)
            page.screenshot(path=str(output / "browser-expired.png"), full_page=True)
            check("no browser runtime errors", report["browser_errors"] == [])
            context.close()
            browser.close()
            report["ok"] = True
    except Exception as error:
        # Transport diagnostics can echo request cookies; never publish them.
        safe_error = re.sub(r"(?im)(cookie:|authorization:)[^\n]*", r"\1 [REDACTED]", str(error))
        report["error"] = safe_error
        raise RuntimeError(safe_error) from None
    finally:
        serialized = json.dumps(report, indent=2) + "\n"
        for name, value in env.items():
            if value and any(fragment in name for fragment in ("TOKEN", "SECRET", "PEPPER", "KEY")):
                serialized = serialized.replace(value, "[REDACTED]")
        (output / "browser.json").write_text(serialized)
    return report
