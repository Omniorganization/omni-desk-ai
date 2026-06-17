# Release Configuration Preflight

This repository treats GitHub release configuration as a first-class release gate. The preflight check fails before expensive build, signing, deploy, promotion, rollback, or smoke jobs when required secrets or variables are absent or malformed.

The script never prints secret values. It reports only missing or invalid variable names. In JSON mode it emits structured issue metadata with `severity`, `kind`, `name`, and `message`.

Configured values are treated as missing when they are empty or only whitespace. Values with leading or trailing whitespace fail preflight because they usually fail later in signing, deploy, or smoke-test scripts.

## Release Build

Set these repository secrets before running `Release Build`:

- `OMNI_ANDROID_KEYSTORE_BASE64`
- `OMNI_ANDROID_KEYSTORE_PASSWORD`
- `OMNI_ANDROID_KEY_ALIAS`
- `OMNI_ANDROID_KEY_PASSWORD`
- `OMNI_ANDROID_GOOGLE_SERVICES_JSON`
- `OMNI_IOS_CERTIFICATE_P12_BASE64`
- `OMNI_IOS_CERTIFICATE_PASSWORD`
- `OMNI_IOS_PROVISIONING_PROFILE_BASE64`
- `OMNI_IOS_KEYCHAIN_PASSWORD`
- `OMNIDESK_RELEASE_SIGNING_KEY`

Set these repository variables:

- `OMNI_IOS_APPLE_TEAM_ID`
- `OMNIDESK_SANDBOX_RUNNER_DIGEST`

Optional iOS variables:

- `OMNI_IOS_BUNDLE_ID`
- `OMNI_IOS_EXPORT_METHOD`

`OMNIDESK_SANDBOX_RUNNER_DIGEST` must be a final OCI digest in the form `sha256:<64 lowercase hex chars>`.

`OMNI_IOS_APPLE_TEAM_ID` must be the 10-character uppercase alphanumeric Apple team id used by the provisioning profile.

## Staging

Set these `staging` environment secrets:

- `OMNIDESK_RELEASE_SIGNING_KEY`
- `OMNIDESK_STAGING_ADMIN_TOKEN`
- `OMNIDESK_STAGING_SANDBOX_TOKEN`
- `OMNIDESK_STAGING_SANDBOX_HMAC_SECRET`

Set these `staging` environment variables:

- `OMNIDESK_STAGING_BASE_URL`
- `OMNIDESK_STAGING_SANDBOX_URL`

The workflow should map staging-specific names to the generic preflight names before invoking the checker:

```text
OMNIDESK_STAGING_BASE_URL          -> OMNIDESK_SMOKE_BASE_URL
OMNIDESK_STAGING_SANDBOX_URL       -> OMNIDESK_SMOKE_SANDBOX_URL
OMNIDESK_STAGING_ADMIN_TOKEN       -> OMNIDESK_SMOKE_ADMIN_TOKEN
OMNIDESK_STAGING_SANDBOX_TOKEN     -> OMNIDESK_SMOKE_SANDBOX_TOKEN
OMNIDESK_STAGING_SANDBOX_HMAC_SECRET -> OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET
```

For `docker-compose` deploys, also set and map:

- `OMNIDESK_STAGING_COMPOSE_FILE` -> `OMNIDESK_DEPLOY_COMPOSE_FILE`
- `OMNIDESK_STAGING_SERVICE` -> `OMNIDESK_DEPLOY_SERVICE`

For `kubectl` deploys, also set and map:

- `OMNIDESK_STAGING_KUBE_CONTEXT` -> `OMNIDESK_DEPLOY_KUBE_CONTEXT`
- `OMNIDESK_STAGING_NAMESPACE` -> `OMNIDESK_DEPLOY_NAMESPACE`
- `OMNIDESK_STAGING_DEPLOYMENT_NAME` -> `OMNIDESK_DEPLOYMENT_NAME`
- `OMNIDESK_STAGING_CONTAINER_NAME` -> `OMNIDESK_CONTAINER_NAME`
- `OMNIDESK_STAGING_IMAGE` -> `OMNIDESK_IMAGE`

`OMNIDESK_STAGING_IMAGE` must be pinned as `image@sha256:<64 lowercase hex chars>` because staging deploys compare it with the release artifact metadata digest.

For `systemd` deploys, also set and map:

- `OMNIDESK_STAGING_HOST` -> `OMNIDESK_DEPLOY_HOST`
- `OMNIDESK_STAGING_USER` -> `OMNIDESK_DEPLOY_USER`

Optional `systemd` variable:

- `OMNIDESK_STAGING_REMOTE_DEPLOY_SCRIPT` -> `OMNIDESK_REMOTE_DEPLOY_SCRIPT`

If omitted, the deploy script uses `/usr/local/bin/omnidesk-deploy-artifact`. If set, it must be an absolute canonical path under `/usr/local/bin`, must not contain `.` or `..` traversal segments, and must use safe token characters only.

## Production

Set these `production` environment secrets:

- `OMNIDESK_RELEASE_SIGNING_KEY`
- `OMNIDESK_PRODUCTION_ADMIN_TOKEN`
- `OMNIDESK_PRODUCTION_SANDBOX_TOKEN`
- `OMNIDESK_PRODUCTION_SANDBOX_HMAC_SECRET`

Set these `production` environment variables:

- `OMNIDESK_PRODUCTION_BASE_URL`
- `OMNIDESK_PRODUCTION_SANDBOX_URL`

Map production-specific values to the same generic preflight names used by staging:

```text
OMNIDESK_PRODUCTION_BASE_URL          -> OMNIDESK_SMOKE_BASE_URL
OMNIDESK_PRODUCTION_SANDBOX_URL       -> OMNIDESK_SMOKE_SANDBOX_URL
OMNIDESK_PRODUCTION_ADMIN_TOKEN       -> OMNIDESK_SMOKE_ADMIN_TOKEN
OMNIDESK_PRODUCTION_SANDBOX_TOKEN     -> OMNIDESK_SMOKE_SANDBOX_TOKEN
OMNIDESK_PRODUCTION_SANDBOX_HMAC_SECRET -> OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET
```

For `docker-compose` deploys, also set:

- `OMNIDESK_PRODUCTION_COMPOSE_FILE`
- `OMNIDESK_PRODUCTION_SERVICE`

For `kubectl` deploys, also set:

- `OMNIDESK_PRODUCTION_KUBE_CONTEXT`
- `OMNIDESK_PRODUCTION_NAMESPACE`
- `OMNIDESK_PRODUCTION_DEPLOYMENT_NAME`
- `OMNIDESK_PRODUCTION_CONTAINER_NAME`
- `OMNIDESK_PRODUCTION_IMAGE`

`OMNIDESK_PRODUCTION_IMAGE` must be pinned as `image@sha256:<64 lowercase hex chars>`.

For `systemd` deploys, also set:

- `OMNIDESK_PRODUCTION_HOST`
- `OMNIDESK_PRODUCTION_USER`

Optional `systemd` variable:

- `OMNIDESK_PRODUCTION_REMOTE_DEPLOY_SCRIPT`

If omitted, the deploy script uses `/usr/local/bin/omnidesk-deploy-artifact`. If set, it must stay under `/usr/local/bin` and must be canonical.

Production promotion forbids `noop` deploy mode. All `kubectl` deployment images must be digest-pinned so the deploy target matches the signed release metadata.

## Web Admin Preflight

Run this scope before Web Admin release, admin-gate, or live smoke jobs:

```bash
python scripts/check_release_configuration.py --scope web-admin
```

Required secrets:

- `OMNIDESK_WEB_ADMIN_ADMIN_TOKEN`

Required variables:

- `OMNIDESK_WEB_ADMIN_BASE_URL`
- `OMNIDESK_WEB_ADMIN_API_BASE_URL`
- `OMNIDESK_WEB_ADMIN_SESSION_PATH`
- `OMNIDESK_WEB_ADMIN_APPROVAL_PATH`
- `OMNIDESK_WEB_ADMIN_AUDIT_PATH`
- `OMNIDESK_WEB_ADMIN_NOTIFICATION_PATH`

The URL variables must be `http` or `https` URLs with a host. The path variables must be absolute canonical HTTP paths, for example `/api/smoke/audit`.

## Desktop Preflight

Run this scope before Desktop release, installer smoke, or app-to-backend smoke jobs:

```bash
python scripts/check_release_configuration.py --scope desktop
```

Required secrets:

- `OMNIDESK_DESKTOP_CLIENT_TOKEN`

Required variables:

- `OMNIDESK_DESKTOP_API_BASE_URL`
- `OMNIDESK_DESKTOP_SESSION_PATH`
- `OMNIDESK_DESKTOP_APPROVAL_PATH`
- `OMNIDESK_DESKTOP_AUDIT_PATH`
- `OMNIDESK_DESKTOP_NOTIFICATION_PATH`
- `OMNIDESK_DESKTOP_UPDATE_CHANNEL`

`OMNIDESK_DESKTOP_UPDATE_CHANNEL` must be one of `stable`, `beta`, `internal`, or `nightly`.

## Tri-App Chain Smoke

The tri-app chain smoke test verifies the same session, approval, audit, and notification chain from Web Admin, Desktop, and Mobile client identities.

Preflight:

```bash
python scripts/check_release_configuration.py --scope tri-app-smoke
```

Live smoke:

```bash
python scripts/tri_app_chain_smoke_test.py \
  --expected-version "$EXPECTED_VERSION" \
  --require-correlation-echo \
  --format json
```

Required secrets:

- `OMNIDESK_WEB_ADMIN_ADMIN_TOKEN`
- `OMNIDESK_DESKTOP_CLIENT_TOKEN`
- `OMNIDESK_MOBILE_CLIENT_TOKEN`

Required variables:

- `OMNIDESK_WEB_ADMIN_BASE_URL`
- `OMNIDESK_DESKTOP_API_BASE_URL`
- `OMNIDESK_MOBILE_API_BASE_URL`
- `OMNIDESK_CHAIN_SESSION_PATH`
- `OMNIDESK_CHAIN_APPROVAL_PATH`
- `OMNIDESK_CHAIN_AUDIT_PATH`
- `OMNIDESK_CHAIN_NOTIFICATION_PATH`

The smoke script sends POST checks for `session` and `approval`, then GET checks for `audit` and `notification`. It passes a generated correlation id through headers and payload/query parameters. With `--require-correlation-echo`, each endpoint must echo that id.

## Local Check

Run a local no-value audit with environment variables already exported:

```bash
python scripts/check_release_configuration.py --scope release
python scripts/check_release_configuration.py --scope staging --deploy-mode docker-compose --require-sandbox-smoke
python scripts/check_release_configuration.py --scope production --deploy-mode kubectl --require-sandbox-smoke
python scripts/check_release_configuration.py --scope web-admin
python scripts/check_release_configuration.py --scope desktop
python scripts/check_release_configuration.py --scope tri-app-smoke
```

JSON output is available for release dashboards or evidence archival:

```bash
python scripts/check_release_configuration.py \
  --scope production \
  --deploy-mode kubectl \
  --require-sandbox-smoke \
  --format json \
  --report-path dist/production-preflight.json
```

Passing preflight does not prove that credentials are valid; it proves the release pipeline has the minimum real configuration needed to start. Credential validity, signing success, artifact availability, deploy reachability, and smoke-test permissions still require live release evidence.
