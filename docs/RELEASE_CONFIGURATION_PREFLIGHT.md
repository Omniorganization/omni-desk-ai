# Release Configuration Preflight

This repository treats GitHub release configuration as a first-class release gate. The preflight check fails before expensive build, signing, deploy, or smoke jobs when required secrets or variables are absent.

The script never prints secret values. It reports only missing or invalid variable names. Configured values are treated as missing when they are empty or only whitespace. Values with leading or trailing whitespace fail preflight because they usually fail later in signing, deploy, or smoke-test scripts.

Version 1.11.4 extends release governance from backend/mobile signing into first-class tri-app preflight scopes for Web Admin, Desktop, Mobile runtime, and the shared cross-end approval/audit path.

## Common Output

Text output is the default:

```bash
python scripts/check_release_configuration.py --scope release
```

Structured JSON output and artifact reports are supported for every scope:

```bash
python scripts/check_release_configuration.py \
  --scope tri-app \
  --format json \
  --report-path dist/tri-app-preflight.json
```

The JSON payload contains `scope`, `deploy_mode`, `ok`, `issue_count`, and an array of issues with `severity`, `kind`, `name`, and `message`.

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

`OMNI_IOS_APPLE_TEAM_ID` must be the 10-character Apple team id used by the provisioning profile.

## Staging / Production / Rollback

Set these downstream environment secrets before deploy/smoke preflight:

- `OMNIDESK_RELEASE_SIGNING_KEY`
- `OMNIDESK_SMOKE_ADMIN_TOKEN`

Set these downstream environment variables:

- `OMNIDESK_SMOKE_BASE_URL`

When sandbox smoke is required, also set:

- `OMNIDESK_SMOKE_SANDBOX_TOKEN`
- `OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET`
- `OMNIDESK_SMOKE_SANDBOX_URL`

For `docker-compose` deploys, also set:

- `OMNIDESK_DEPLOY_COMPOSE_FILE`
- `OMNIDESK_DEPLOY_SERVICE`

For `kubectl` deploys, also set:

- `OMNIDESK_DEPLOY_KUBE_CONTEXT`
- `OMNIDESK_DEPLOY_NAMESPACE`
- `OMNIDESK_DEPLOYMENT_NAME`
- `OMNIDESK_CONTAINER_NAME`
- `OMNIDESK_IMAGE`

`OMNIDESK_IMAGE` must be pinned as `image@sha256:<64 lowercase hex chars>` for every downstream `kubectl` deployment.

For `systemd` deploys, also set:

- `OMNIDESK_DEPLOY_HOST`
- `OMNIDESK_DEPLOY_USER`

Optional `systemd` variable:

- `OMNIDESK_REMOTE_DEPLOY_SCRIPT`

If omitted, the deploy script uses `/usr/local/bin/omnidesk-deploy-artifact`. If set, it must be a canonical absolute path under `/usr/local/bin` and must not contain `.` or `..` path segments.

Production promotion forbids `noop` deploy mode.

## Web Admin Scope

Run:

```bash
python scripts/check_release_configuration.py --scope web-admin
```

Required secrets:

- `WEB_ADMIN_ADMIN_TOKEN`
- `WEB_ADMIN_AUTH_SECRET`

Required variables:

- `WEB_ADMIN_BASE_URL`
- `WEB_ADMIN_API_BASE_URL`
- `WEB_ADMIN_IMAGE`

Rules:

- `WEB_ADMIN_BASE_URL` and `WEB_ADMIN_API_BASE_URL` must be https URLs, except localhost http is accepted for local smoke.
- `WEB_ADMIN_IMAGE` must be digest-pinned as `image@sha256:<64 lowercase hex chars>`.

## Desktop Scope

Run:

```bash
python scripts/check_release_configuration.py --scope desktop
```

Required secrets:

- `DESKTOP_BRIDGE_TOKEN`
- `DESKTOP_BRIDGE_HMAC_SECRET`

Required variables:

- `DESKTOP_AGENT_BASE_URL`
- `DESKTOP_UPDATE_ENDPOINT`
- `DESKTOP_APP_IDENTIFIER`
- `DESKTOP_BRIDGE_ORIGIN`

Rules:

- URLs must be https, except localhost http is accepted for local agent and local bridge smoke.
- `DESKTOP_APP_IDENTIFIER` must be a reverse-DNS app identifier such as `com.omnidesk.agent`.
- `DESKTOP_BRIDGE_ORIGIN` must be an origin only: scheme + host + optional port, without path/query/fragment.

## Mobile Runtime Scope

Run:

```bash
python scripts/check_release_configuration.py --scope mobile
```

Required secrets:

- `MOBILE_APPROVAL_TOKEN`
- `MOBILE_PUSH_HMAC_SECRET`

Required variables:

- `MOBILE_API_BASE_URL`
- `MOBILE_APPROVAL_CALLBACK_URL`
- `OMNI_ANDROID_PACKAGE_NAME`
- `OMNI_IOS_BUNDLE_ID`

Rules:

- Mobile runtime URLs must be https, except localhost http is accepted for local smoke.
- `OMNI_ANDROID_PACKAGE_NAME` must be a lowercase Android package name.
- `OMNI_IOS_BUNDLE_ID` must be a reverse-DNS iOS bundle id.

## Tri-App Scope

Run:

```bash
python scripts/check_release_configuration.py --scope tri-app --format json --report-path dist/tri-app-preflight.json
```

Required secrets:

- `TRI_APP_ADMIN_TOKEN`
- `TRI_APP_MOBILE_APPROVAL_TOKEN`
- `TRI_APP_DESKTOP_AGENT_TOKEN`
- `TRI_APP_AUDIT_HMAC_SECRET`

Required variables:

- `TRI_APP_BACKEND_BASE_URL`
- `TRI_APP_WEB_ADMIN_BASE_URL`
- `TRI_APP_MOBILE_CALLBACK_URL`
- `TRI_APP_DESKTOP_AGENT_URL`
- `TRI_APP_ORG_ID`

Rules:

- All tri-app endpoints must be https, except localhost http is accepted for local Desktop Agent smoke.
- `TRI_APP_ORG_ID` must be 3-64 safe characters.
- The scope represents the minimum configuration needed to test Desktop -> Backend -> Mobile approval -> Backend audit -> Web Admin visibility flows.

## Local Check Examples

```bash
python scripts/check_release_configuration.py --scope release
python scripts/check_release_configuration.py --scope staging --deploy-mode docker-compose --require-sandbox-smoke
python scripts/check_release_configuration.py --scope production --deploy-mode kubectl --require-sandbox-smoke
python scripts/check_release_configuration.py --scope web-admin
python scripts/check_release_configuration.py --scope desktop
python scripts/check_release_configuration.py --scope mobile
python scripts/check_release_configuration.py --scope tri-app --format json --report-path dist/tri-app-preflight.json
```

Passing preflight does not prove that credentials are valid; it proves the release pipeline has the minimum real configuration needed to start the relevant release, deployment, or tri-app smoke stage.


## iOS Real-Device Evidence

Set this variable before running the iOS evidence preflight:

- `IOS_EVIDENCE_RAW_DIR`
- `IOS_EVIDENCE_EXPECTED_VERSION`

The directory must contain:

- `native-build/flutter-ios-release.json`
- `signed-artifacts/ios-signed-ipa.json`
- `push/apns-live-delivery.json`

Run:

```bash
python scripts/check_release_configuration.py --scope ios-evidence
python scripts/import_ios_real_device_evidence.py \
  --raw-dir "$IOS_EVIDENCE_RAW_DIR" \
  --expected-version "$IOS_EVIDENCE_EXPECTED_VERSION" \
  --copy \
  --write-report release/ios-real-device-evidence-import-report.json
```

## Mobile Real Device

Set these secrets:

- `MOBILE_APPROVAL_TOKEN`

Set these variables:

- `IOS_EVIDENCE_RAW_DIR`
- `IOS_EVIDENCE_EXPECTED_VERSION`
- `IOS_DEVICE_UDID`
- `IOS_DEVICE_NAME`
- `IOS_SIGNED_IPA_PATH`
- `MOBILE_API_BASE_URL`
- `MOBILE_APPROVAL_CALLBACK_URL`
- `OMNI_IOS_BUNDLE_ID`

`IOS_SIGNED_IPA_PATH` must point to a signed `.ipa` artifact.

## Tri-App Live Smoke

Set the same tri-app secrets and variables as `--scope tri-app`, plus:

- `TRI_APP_LIVE_SMOKE_SCENARIO_ID`
- `TRI_APP_LIVE_SMOKE_REPORT_PATH`

The report path must be a safe relative path such as `dist/tri-app-live-smoke.json`.

Run:

```bash
python scripts/check_release_configuration.py --scope tri-app-live-smoke --format json --report-path dist/tri-app-live-smoke-preflight.json
```

---

## 1.11.7 Real GA Evidence Semantic Closure Addendum

The following scopes now perform semantic evidence validation rather than configuration-only checks:

```bash
python scripts/check_release_configuration.py --scope ios-evidence --format json --report-path dist/ios-evidence-preflight.json
python scripts/check_release_configuration.py --scope tri-app-live-smoke --format json --report-path dist/tri-app-live-smoke-preflight.json
```

### iOS Evidence Closure

`IOS_EVIDENCE_RAW_DIR` must contain:

- `native-build/flutter-ios-release.json`
- `signed-artifacts/ios-signed-ipa.json`
- `push/apns-live-delivery.json`

Each JSON document must be semantically valid and must reference at least one artifact file under the raw evidence directory. The artifact path must be relative, canonical, and must not contain `.` or `..` segments. The referenced file must exist and its SHA256 must match the JSON value.

Every iOS evidence document must match the expected release version passed to `scripts/import_ios_real_device_evidence.py --expected-version`. Native build evidence must have `exit_code: 0`.

APNS live delivery evidence must include an artifact reference to delivery evidence, not the signed `.ipa`. The artifact `kind` must be one of `apns_provider_receipt`, `device_notification_log`, or `firebase_delivery_receipt`:

```json
{
  "artifacts": [
    {
      "kind": "apns_provider_receipt",
      "path": "artifacts/ios/apns-delivery-receipt.json",
      "sha256": "<real lowercase 64-char sha256>"
    }
  ]
}
```

### Tri-App Live Smoke Closure

`TRI_APP_LIVE_SMOKE_REPORT_PATH` must point to an existing safe relative JSON report. The report must prove the full roundtrip:

- desktop action proposed
- backend approval created
- mobile push received
- mobile approval decision submitted
- desktop action resumed
- audit event written
- Web Admin audit visible

Use the importer for final evidence copy/reporting:

```bash
python scripts/import_tri_app_live_smoke_evidence.py \
  --report "$TRI_APP_LIVE_SMOKE_REPORT_PATH" \
  --expected-org-id "$TRI_APP_ORG_ID" \
  --expected-scenario-id "$TRI_APP_LIVE_SMOKE_SCENARIO_ID" \
  --copy \
  --write-report release/tri-app-live-smoke-evidence-import-report.json
```

### Workflow Governance Closure

Run the workflow governance checker against the real release workflow:

```bash
python scripts/check_workflow_governance.py . --require-real-workflows
```

It verifies that `.github/workflows/release.yml` contains the expected tri-app preflight, iOS evidence import, tri-app live smoke import, evidence report upload, release metadata, and attestation wiring. Patch snippets alone are no longer sufficient for real release governance closure.
