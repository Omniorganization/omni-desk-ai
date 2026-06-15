# OmniDesk AI 1.07 Production GA Release Integrity Source Package

This package hardens the 1.06 GA closure line around release integrity: version drift detection, package cleanliness, deterministic source zips, and CI checkout compatibility.

## 1.07 GA optimization direction

- Treat version identity as a whole-product contract, not only a Python package field.
- Reject frontend, native, and runtime-generated artifacts before release packaging.
- Keep clean zip generation non-mutating so packaging never edits the source tree it is packaging.
- Allow `.git` only when validating a live CI checkout; generated packages remain VCS-free.
- Keep external GA evidence explicit instead of fabricating signer, registry, push, soak, rollback, or backup/restore proof.

## Remaining external evidence boundary

- Real macOS notarization and Windows code signing.
- Real Android/iOS signed build artifacts.
- APNS/FCM live push validation.
- Registry attestations with final OCI digests.
- Multi-instance PostgreSQL soak, rollback, and backup/restore drill evidence.
