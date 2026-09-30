# Compatible glib security backport

Owner: security-release-owner. Scope: Linux Tauri/GTK glib 0.18 ABI only.

The published glib 0.18.5 crate is retained verbatim as `glib-0.18.5.crate`,
SHA-256 `233daaf6e83ae6a12a52055f568f9d7cf4671dabb78ff9560ab6da230ce00ee5`.
Its 121 extracted files retain the normalized manifest, MIT LICENSE and COPYRIGHT.
The ONLY vendor source delta is the two-line upstream fix for
GHSA-wrw7-89jp-8q8g/RUSTSEC-2024-0429: mutable `p` and `&mut p` in
`VariantStrIter::impl_get`. The variadic GLib function writes to this output
pointer; passing an immutable Rust reference was undefined behavior.

Provenance: https://github.com/gtk-rs/gtk-rs-core/pull/1343
Fix: `b5a4071e439bef2b5eea76c3aa25e5ae84839e34`.
Merge: `05dff0ee696f9bcd8617cd48c4b812d046d440cb`.
Published source: `42b9caf98e03ded086362d9653ca58fe94dc8658`.
The crate remains version 0.18.5; it is NOT relabeled as a fixed 0.20 release.

The desktop Cargo patch replaces every transitive glib consumer. Because a path
lock has no registry checksum, `scripts/check_glib_backport.py` verifies the
retained archive hash, complete source inventory, exact allowed patch, Cargo
manifest/lock and (in Linux Actions) resolved metadata path. Scanner omission of
a local dependency alone is never remediation evidence. All advisory ignores
and the former Linux exception are removed; source integrity is blocking.

Validation: the real Linux Actions job runs optimized baseline and patched
iterator regressions, preserves their logs/status and Cargo metadata; the
patched test is mandatory. Baseline UB can be compiler-dependent and a passing
baseline is not proof of safety. Full locked Linux clippy/build/tests and the
Windows/macOS/iOS/Android suites remain mandatory. No runtime validation is
claimed by this source note before those exact-head Actions finish.

Risk: maintaining a pinned compatibility backport introduces source ownership
and packaging responsibilities. Native GNOME library licensing remains separate.
Removal criteria: a reviewed Tauri/GTK migration supporting upstream glib >=0.20
passes the same complete gates; remove the archive, vendor, patch and integrity
gate together. Rollback: revert via a signed reviewed PR and rerun all gates;
restoring unpatched registry 0.18.5 reopens the advisory and is not a safe release.
