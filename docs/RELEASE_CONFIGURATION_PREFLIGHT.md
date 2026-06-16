# Release Configuration Preflight

This repository now treats GitHub release configuration as a first-class release gate. The preflight check fails before expensive build, signing, deploy, or smoke jobs when required secrets or variables are absent.

The script never prints secret values. It reports only missing or invalid variable names.

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

## Staging

Set these `staging` environment secrets:

- `OMNIDESK_RELEASE_SIGNING_KEY`
- `OMNIDESK_STAGING_ADMIN_TOKEN`
- `OMNIDESK_STAGING_SANDBOX_TOKEN`
- `OMNIDESK_STAGING_SANDBOX_HMAC_SECRET`

Set these `staging` environment variables:

- `OMNIDESK_STAGING_BASE_URL`
- `OMNIDESK_STAGING_SANDBOX_URL`

For `docker-compose` deploys, also set:

- `OMNIDESK_STAGING_COMPOSE_FILE`
- `OMNIDESK_STAGING_SERVICE`

For `kubectl` deploys, also set:

- `OMNIDESK_STAGING_KUBE_CONTEXT`
- `OMNIDESK_STAGING_NAMESPACE`
- `OMNIDESK_STAGING_DEPLOYMENT_NAME`
- `OMNIDESK_STAGING_CONTAINER_NAME`
- `OMNIDESK_STAGING_IMAGE`

For `systemd` deploys, also set:

- `OMNIDESK_STAGING_HOST`
- `OMNIDESK_STAGING_USER`
- `OMNIDESK_STAGING_REMOTE_DEPLOY_SCRIPT`

## Production

Set these `production` environment secrets:

- `OMNIDESK_RELEASE_SIGNING_KEY`
- `OMNIDESK_PRODUCTION_ADMIN_TOKEN`
- `OMNIDESK_PRODUCTION_SANDBOX_TOKEN`
- `OMNIDESK_PRODUCTION_SANDBOX_HMAC_SECRET`

Set these `production` environment variables:

- `OMNIDESK_PRODUCTION_BASE_URL`
- `OMNIDESK_PRODUCTION_SANDBOX_URL`

For `docker-compose` deploys, also set:

- `OMNIDESK_PRODUCTION_COMPOSE_FILE`
- `OMNIDESK_PRODUCTION_SERVICE`

For `kubectl` deploys, also set:

- `OMNIDESK_PRODUCTION_KUBE_CONTEXT`
- `OMNIDESK_PRODUCTION_NAMESPACE`
- `OMNIDESK_PRODUCTION_DEPLOYMENT_NAME`
- `OMNIDESK_PRODUCTION_CONTAINER_NAME`
- `OMNIDESK_PRODUCTION_IMAGE`

For `systemd` deploys, also set:

- `OMNIDESK_PRODUCTION_HOST`
- `OMNIDESK_PRODUCTION_USER`
- `OMNIDESK_PRODUCTION_REMOTE_DEPLOY_SCRIPT`

Production promotion forbids `noop` deploy mode. Production `kubectl` images must be digest-pinned.

## Local Check

Run a local no-value audit with environment variables already exported:

```bash
python scripts/check_release_configuration.py --scope release
python scripts/check_release_configuration.py --scope staging --deploy-mode docker-compose --require-sandbox-smoke
python scripts/check_release_configuration.py --scope production --deploy-mode kubectl --require-sandbox-smoke
```

Passing preflight does not prove that credentials are valid; it proves the release pipeline has the minimum real configuration needed to start.
