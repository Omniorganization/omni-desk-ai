# Gmail channel

## Dependencies

`google-auth-oauthlib` and `google-api-python-client` are the `gmail` extra. They pull in `requests`/`urllib3`, which the default production locks deliberately exclude (`tests/test_security_tooling_dependency_boundary.py`), so they are not in `requirements.runtime.lock` or `requirements.enterprise.lock`. A deployment that enables Gmail installs the hash-pinned `requirements.gmail.lock` instead (runtime dependencies plus the Gmail extra):

```bash
python -m pip install --require-hashes -r requirements.gmail.lock
```

`requirements.gmail.lock` is the explicit exception to the production-lock urllib3 ban, and it is audited on its own: the Security workflow hash-checks it, runs `pip-audit` on it, and runs the license gate in an environment installed from it (`scripts/check_security_workflow_policy.py` requires all three). `tests/test_security_tooling_dependency_boundary.py` pins its terms: patched urllib3, no audit tooling, and exactly the enterprise lock's versions for every package they share. The dev and test locks also include the extra so the test suite runs against the real libraries. Gmail stays disabled unless `channels.gmail.enabled` is set, and an enabled channel needs `OMNIDESK_GMAIL_TOKEN_ENCRYPTION_KEY` (tokens are encrypted at rest).

### Container image

The default image built from `Dockerfile` does **not** include the Gmail libraries: its runtime stage installs `requirements.bootstrap.lock`, `requirements.runtime.lock` and `requirements.enterprise.lock`, then removes pip, so Gmail cannot be added to a finished image. With `channels.gmail.enabled: true` that image fails at the first Gmail call with "Install google-auth-oauthlib …".

A Gmail-enabled deployment builds its own image from the same Dockerfile with two lines changed in the runtime stage, copying the lock in and installing it right after the enterprise lock:

```Dockerfile
COPY requirements.bootstrap.lock requirements.runtime.lock requirements.enterprise.lock requirements.gmail.lock /tmp/
# ...
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.enterprise.lock \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.gmail.lock \
```

and adds `/tmp/requirements.gmail.lock` to the `rm -rf` cleanup. Because the Gmail lock pins every shared package at the enterprise lock's version, that extra install only adds the 19 Gmail packages and changes nothing else in the image. Scan and sign that image like the default one before promoting it.

## Inbound: push notifications through Cloud Pub/Sub

Gmail does not call webhooks itself. It publishes a notification to a Cloud Pub/Sub topic after `users.watch`, and a push subscription forwards it to `POST /webhooks/gmail`:

1. Create a Pub/Sub topic and grant `gmail-api-push@system.gserviceaccount.com` the *Pub/Sub Publisher* role on it.
2. Create a push subscription to `https://<public gateway host>/webhooks/gmail` with **authentication enabled**: pick a service account and set the audience (by default the endpoint URL).
3. Set `channels.gmail.push_audience` to that audience and `channels.gmail.push_service_account_email` to that service account. The route answers 404 until both are set.
4. Run `scripts/gmail_watch.py --config <config.yaml> --topic projects/<project>/topics/<topic>` and schedule it at least daily: Gmail stops pushing seven days after the last watch call.

Every push must carry the subscription's Google-signed OIDC token (`iss` accounts.google.com, the configured audience, `email` = the service account, `email_verified`); anything else gets 403. A notification only carries the mailbox's new history id, so the gateway reads `users.history.list` (INBOX, `messageAdded`) from the stored cursor (`gmail_history_state.json` next to the token file), fetches each new message's `From`/`Subject` metadata, applies `allowed_senders` by **exact address** (a display name that contains an allowed address no longer passes), and queues the rest as jobs. The first notification only stores the cursor.

Handling is idempotent: the cursor moves only after a successful read, and the job queue deduplicates Gmail message ids, so a redelivered or replayed notification can only re-read history. A failed read answers 503 and Pub/Sub retries it. Gmail API calls run in a worker thread, never on the event loop.

The gateway needs egress to `www.googleapis.com` (certificates and the Gmail API) and a public HTTPS endpoint for the push subscription; none of this has been exercised against a real Gmail account yet.

## Outbound

`channels.send_email` in `omnidesk_agent/tools/channel_send.py` still only builds the raw message after approval; `GmailChannel.send_email` exists but is not wired to the tool.
