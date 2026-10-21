# Release signing and evidence trust

What a staging, production or rollback job trusts, and what the repository owner
must configure in GitHub for that trust to hold. The repository enforces the
workflow side with `scripts/check_security_workflow_policy.py`,
`scripts/check_workflow_governance.py --require-real-workflows` and
`scripts/check_supply_chain_standard.py`; the GitHub settings below cannot be
set from the repository.

## Trust model

- **Signer identity.** Cosign certificates must match
  `https://github.com/<owner>/<repo>/.github/workflows/release.yml@refs/heads/main`.
  Every `gh attestation verify` pins `--signer-workflow` to `release.yml`,
  `--source-ref refs/heads/main` and `--deny-self-hosted-runners`. Release tags
  are not a trusted ref: anyone with push access can create a tag on an
  unreviewed commit.
- **Signing secrets.** The `build`, `mobile-android-release` and
  `mobile-ios-release` jobs of `release.yml` run only on `refs/heads/main` and
  in the `release` environment, which holds the Ed25519 release key and the
  Android/iOS signing material. A workflow run from any other branch cannot
  read them.
- **Release signatures.** `scripts/finalize_release_payload.py` is the last
  write to `dist/`: it writes `checksums.txt` over every file (wheel, SBOM,
  metadata, native bundle, locked Helm values, SLSA provenance, Cosign
  certificates), signs each with Ed25519 and verifies the result, so
  `verify_release_artifact.py --require-signatures` holds for the published
  artifact. Verifiers use the committed `release/release-signing-public-key.pem`,
  run with `OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES=1`, and hold no signing
  secret. The algorithm comes from the verifier, never from the unsigned
  `release_signatures.json`; an empty or partial manifest is rejected.
- **External evidence.** Workflows that take evidence from another run id use
  `scripts/fetch_ci_evidence_artifact.py`. The run must be a producer listed for
  that consumer in `release/evidence-source-workflows.json`, have succeeded on
  `main` in this repository (not a fork or pull request), and have a head
  commit equal to (Release) or an ancestor of (other consumers) the consuming
  commit. The artifact is unpacked outside the workspace and only the expected
  evidence JSON files and the artifacts they reference are copied. pytest
  collects only `tests/`, so nothing in `release/`, `dist/`, `current/` or
  `previous/` is imported.

## Owner actions in GitHub

1. **Create or update the `release` environment** (Settings → Environments → `release`):
   - Deployment branches and tags: *Selected branches and tags*, one branch rule
     `main`, no tag rules.
   - Required reviewers: the release owners (at least one person other than the
     dispatcher), with *Prevent self-review* enabled.
   - Clear *Allow administrators to bypass configured protection rules*.
2. **Move the signing secrets into the `release` environment** and delete them
   from repository and organization secrets:
   `OMNIDESK_RELEASE_SIGNING_ED25519_KEY` (new, step 3),
   `OMNI_ANDROID_KEYSTORE_BASE64`, `OMNI_ANDROID_KEYSTORE_PASSWORD`,
   `OMNI_ANDROID_KEY_ALIAS`, `OMNI_ANDROID_KEY_PASSWORD`,
   `OMNI_IOS_DISTRIBUTION_CERT_BASE64`, `OMNI_IOS_DISTRIBUTION_CERT_PASSWORD`,
   `OMNI_IOS_PROVISIONING_PROFILE_BASE64`, `OMNI_IOS_EXPORT_OPTIONS_PLIST_BASE64`,
   `OMNI_IOS_KEYCHAIN_PASSWORD`. `OMNI_ANDROID_GOOGLE_SERVICES_JSON` may move too.
3. **Store the Ed25519 release private key in the `release` environment.** The
   public key is committed at `release/release-signing-public-key.pem` (#188).
   The matching private key PEM must be the `release` environment secret
   `OMNIDESK_RELEASE_SIGNING_ED25519_KEY` only (not a repository or organization
   secret), with an offline backup. The release job verifies its own signatures
   against the committed public key, so a secret that does not match it fails
   the release. To rotate, run `python scripts/generate_release_signing_key.py`
   on a trusted machine and replace the public key through a reviewed pull
   request. Never commit the private key.
4. **Remove the legacy HMAC key** `OMNIDESK_RELEASE_SIGNING_KEY` from the
   repository, the organization, and the `staging` and `production`
   environments. No workflow reads it any more.
5. **Restrict `staging` and `production` deployment branches to `main`** and keep
   their required reviewers, so the verifier workflows that deploy are always
   reviewed code.
6. **Keep `main` protected** (pull request reviews with CODEOWNERS, no force
   push, no deletion, restricted push). `release.yml@refs/heads/main` is only
   trustworthy because every commit on `main` was reviewed. A tag ruleset is not
   required for signing trust; optionally restrict creation of `v*` tags to
   release owners so tags do not look like releases.
7. **Register evidence producers.** Release-artifact, AWS Device Farm,
   live-services and staging-operations producer workflows do not exist on
   `main` yet (only BrowserStack does). Add each
   workflow and list it under the consumer in
   `release/evidence-source-workflows.json` in a reviewed change before using its
   run ids in the control plane.

## Risk notes

- Until steps 1–3 are done, release jobs wait for or are refused by the
  `release` environment, or stop at "Sign and verify the complete release
  payload with Ed25519" (secret missing or not matching the committed public
  key): releases fail closed.
- Verifiers now always require Ed25519 (`OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES`
  is fixed to 1 instead of the `vars.` switch #183 added), so HMAC-signed
  artifacts are refused immediately.
- Artifacts released before this change are HMAC-signed and may have been
  attested from a tag ref, so they can no longer be deployed or used as a
  rollback target. A rollback drill needs two releases built after this change.
- A release now waits for `release` environment approval twice: once for the
  mobile jobs and once for the signing job.
- Evidence runs from branches, pull requests, forks, failed runs or
  unregistered producers are rejected. Files in an evidence artifact that are
  neither expected evidence JSON nor referenced by it are dropped (listed as
  `ignored_files` in the fetch/import report).
- Cosign now signs the payload and SLSA provenance before the Ed25519 layer
  exists, so it no longer signs `checksums.txt`, `release_signatures.json` or
  `*.sig`; the Ed25519 manifest covers the Cosign certificates instead, and the
  GitHub attestation still covers every file in `dist/`. SLSA subjects are the
  payload files plus the image digest.
- `--deny-self-hosted-runners` rejects attestations from self-hosted runners;
  moving the release build to a self-hosted runner requires changing the
  verifiers in a reviewed change.

## Rollback

Revert the commit that introduced this document. Reverting re-opens the issues
it fixed (tag-ref signing, repository-level signing secrets, evidence imports
from arbitrary runs, HMAC downgrade), so prefer finishing the owner actions over
reverting. After a revert, staging, production and rollback need
`OMNIDESK_RELEASE_SIGNING_KEY` again in their environments, and the release job
needs it as a secret it can read.
