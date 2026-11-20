# Gmail channel

## Dependencies

`google-auth-oauthlib` and `google-api-python-client` are the `gmail` extra. They pull in `requests`/`urllib3`, which the default production locks deliberately exclude (`tests/test_security_tooling_dependency_boundary.py`), so they are not in `requirements.runtime.lock` or `requirements.enterprise.lock`. A deployment that enables Gmail installs the hash-pinned `requirements.gmail.lock` instead (runtime dependencies plus the Gmail extra):

```bash
python -m pip install --require-hashes -r requirements.gmail.lock
```

The dev and test locks include the extra, so CI's license and vulnerability audits cover these packages. Gmail stays disabled unless `channels.gmail.enabled` is set, and an enabled channel needs `OMNIDESK_GMAIL_TOKEN_ENCRYPTION_KEY` (tokens are encrypted at rest).

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
