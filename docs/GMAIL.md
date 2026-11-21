# Gmail channel

## Dependencies

`google-auth-oauthlib` and `google-api-python-client` are the `gmail` extra. They pull in `requests`/`urllib3`, which the default production locks deliberately exclude (`tests/test_security_tooling_dependency_boundary.py`), so they are not in `requirements.runtime.lock` or `requirements.enterprise.lock`. A deployment that enables Gmail installs the hash-pinned `requirements.gmail.lock` instead (runtime dependencies plus the Gmail extra):

```bash
python -m pip install --require-hashes -r requirements.gmail.lock
```

`requirements.gmail.lock` is the explicit exception to the production-lock urllib3 ban, and it is audited on its own: the Security workflow hash-checks it, runs `pip-audit` on it, and runs the license gate in an environment installed from it (`scripts/check_security_workflow_policy.py` requires all three; `tests/test_security_tooling_dependency_boundary.py` checks that deleting the license gate fails the policy). `tests/test_security_tooling_dependency_boundary.py` pins its terms: patched urllib3, no audit tooling, and exactly the enterprise lock's versions for every package they share. The dev and test locks also include the extra so the test suite runs against the real libraries. Gmail stays disabled unless `channels.gmail.enabled` is set, and an enabled channel needs `OMNIDESK_GMAIL_TOKEN_ENCRYPTION_KEY` (tokens are encrypted at rest).

### Container image

The default image built from `Dockerfile` does **not** include the Gmail libraries: its runtime stage installs `requirements.bootstrap.lock`, `requirements.runtime.lock` and `requirements.enterprise.lock`, then removes pip, so Gmail cannot be added to a finished image. With `channels.gmail.enabled: true` on that image, the OAuth flow fails with "Install google-auth-oauthlib to use Gmail OAuth flow", and every push notification after the first fails to read history with "Install google-auth and google-api-python-client": the route logs that cause, counts it in `omnidesk_gmail_push_read_failures_total{reason="RuntimeError"}` and answers 503, so Pub/Sub keeps retrying until the image is fixed.

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

Every push must carry the subscription's Google-signed OIDC token (`iss` accounts.google.com, the configured audience, `email` = the service account, `email_verified`); anything else gets 403. A notification only carries the mailbox's new history id, so the gateway reads `users.history.list` (INBOX, `messageAdded`) from the stored cursor (`gmail_history_state.json` next to the token file), fetches each new message's `From`/`Subject` metadata, applies `allowed_senders` by **exact address** (a display name that contains an allowed address no longer passes), and queues the rest as jobs. The first notification only stores the cursor; normally `scripts/gmail_watch.py` has stored one already.

### Sender allowlist

An empty `allowed_senders` admits every sender. Production refuses that once push is configured (`push_audience` or `push_service_account_email` set): `validate_production_config` reports `channels.gmail must configure at least one production allowlist when push is configured: allowed_senders`, as it does for every other inbound channel. Gmail used only outbound needs no allowlist.

`allowed_senders` matches the `From` header, which the sender writes. It is **not sender authentication**: a message with a forged `From: boss@company.com` passes unless the sending domain's DMARC policy makes Gmail reject it or file it as spam (spam is not in INBOX, so it is never read). The queued job's actor is that `From` address. List only addresses whose domains publish an enforcing DMARC policy (`p=reject` or `p=quarantine`), and keep the high-risk-capability approval requirement that production already enforces.

### Delivery guarantees and failure handling

- **No mail is dropped on retry.** The cursor is saved only after every message read for a notification is queued. If queueing fails, the route answers 500, the cursor stays, and Pub/Sub's redelivery reads the same messages again; the job queue deduplicates Gmail message ids, so nothing is queued twice. The response reports `queued` (new jobs) and `duplicates`.
- **Bounded work per request, forward progress on a backlog.** One notification reads at most one `history.list` page of 25 records (`MAX_HISTORY_RECORDS_PER_PUSH`), so one history call plus one `messages.get` per new message stays inside Pub/Sub's push acknowledgement deadline (10 s by default). When more records remain, the cursor moves to the last record read and the response says `"more": true`; the next notification continues from there. After an outage, Pub/Sub redelivers the notifications it held, and each one drains the next 25 records. After a lapsed watch, only new mail produces notifications, so a large backlog drains at 25 records per new notification.
- **Expired cursor.** Gmail keeps history for about a week and answers an older cursor with 404 (its advice is a full sync). The gateway does not replay that gap: it moves the cursor to the notification's history id, logs a warning naming both ids, and counts `omnidesk_gmail_history_gaps_total`. Mail in the gap stays in the mailbox but is not queued. `scripts/gmail_watch.py` keeps an existing cursor on renewal, so a renewal never skips mail that Gmail can still return; an expired one is reset this way by the next notification.
- **Deleted message.** A message deleted between arriving and being read (`messages.get` 404) is skipped, logged by id, and counted in `omnidesk_gmail_messages_missing_total`.
- **Any other read failure** (missing Google libraries, a revoked or expired refresh token, a Gmail 5xx) is logged with its exception type and redacted message, counted in `omnidesk_gmail_push_read_failures_total{reason=<exception type>}`, and answered with 503, so Pub/Sub retries with the cursor unchanged. A failure that persists needs an operator: alert on that counter.
- Concurrent notifications can finish out of order; the stored cursor only moves forward.

Gmail API calls run in a worker thread, never on the event loop. The cursor is a file next to the token file, so each gateway replica keeps its own. A replica whose cursor is behind only re-reads history, and a job queue shared by all replicas (PostgreSQL) deduplicates those messages; with per-replica SQLite queues, route the push subscription to a single replica.

The gateway needs egress to `www.googleapis.com` (certificates and the Gmail API) and a public HTTPS endpoint for the push subscription; none of this has been exercised against a real Gmail account yet.

## Outbound

`channels.send_email` in `omnidesk_agent/tools/channel_send.py` still only builds the raw message after approval; `GmailChannel.send_email` exists but is not wired to the tool.
