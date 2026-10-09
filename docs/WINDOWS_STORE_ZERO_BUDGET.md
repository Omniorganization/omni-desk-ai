# Windows Store input preparation without a signing subscription

`scripts/package_windows_store_msix.ps1` packages an already built x64 Windows PE into an **unsigned Store submission input**. It does not sign, install, execute, publish or certify the application. Microsoft Store certification/signing, the owner's actual registered package identity and install/update acceptance remain outstanding. Existing EXE/MSI installers do not receive free signing merely by linking them in the Store.

Primary guidance: <https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/publish-first-app> and <https://learn.microsoft.com/en-us/windows/msix/package/sign-msix-package-guide>. Tauri's built-in bundle workflow currently generates EXE/MSI, so this helper adds a separate packaging route: <https://v2.tauri.app/distribute/microsoft-store/>.

## Actual owner inputs

Complete the appropriate individual/company registration at <https://developer.microsoft.com/en-us/microsoft-store/register>, using the true legal/business role. Do not assume the personal noncommercial account shown by default fits a commercial organization. The owner must complete new credentials, identity verification and agreements on official pages. No account, Store listing, certificate or final signature has been created by this patch.

After reserving the actual application identity, copy the Package Identity Name and Publisher subject exactly from Partner Center; supply DisplayName, PublisherDisplayName and a Store version such as `1.12.7.0`. Test fixtures in CI are explicitly synthetic and must never be submitted as the owner's identity. No private key or token is accepted by the helper. Run all packaging/build/install testing in the authorized cloud, not on this Mac.

Build the native application using the repository's locked Rust/npm dependencies and independently approved source. Pass the genuine `omnidesk_desktop.exe` and a directory containing `Square44x44Logo.png`, `Square150x150Logo.png` and `StoreLogo.png` to the helper. It requires an official installed Windows SDK and uses MakeAppx schema/package validation, rejects non-x64 PE files and invalid versions/identity paths, escapes XML values and refuses output overwrite. It copies only the declared executable and three public logo assets.

The output JSON binds the executable/package hashes to the provided source commit, but does not independently prove the executable was built from that commit: retain the actual trusted build-run/provenance evidence. It explicitly records that identity and signature verification were not performed. This record is **not** imported as passing external GA evidence.

## Remaining platform work

The package declares `runFullTrust` because the existing Tauri executable is a desktop process; no extra shell/native app commands, secrets or weakened request/device signing are introduced. The Store must approve the restricted capability and the actual application behavior. This manifest is a starting desktop packaging route, not proof of Store certification or compatibility.

System WebView2 is an explicit runtime prerequisite and is not bundled by this helper. Additional native runtime dependencies, secure storage and device enrollment must be verified on clean supported Windows machines. If those checks require a fixed WebView2/native payload, add a separately reviewed pinned dependency packaging change; do not silently download arbitrary runtimes or require users to disable signature validation.

After successful Store certification, download the real Store-produced package, verify its OS-trusted package signature, identity, block map and version and bind the exact bytes/build/source to the evidence. Run actual install, launch, enrolled signed requests, protected credential storage, upgrade and uninstall checks on the supported Windows versions. The present release gate's Windows Authenticode fields must not be filled from an unsigned MSIX; any Store-specific evidence support requires a reviewed extension with equally strict raw signature/identity validation. Until then customer GA remains blocked.

## Validation, risk and rollback

The Windows cloud preflight compiles a minimal x64 **test-only** fixture (never executes it), packages it through the real SDK, reads the package/manifest and checks its digest, XML escaping and absence of a claimed signature. Negative controls reject overwrite, invalid Store version, overflow, non-PE input and path injection. This is packaging verification, not a test of the real Tauri runtime or a publisher signature.

Risks: missing system/runtime dependencies, desktop-package restrictions and Store acceptance. The existing customer installer path is unchanged. Roll back by selecting the previously independently verified release/distribution route and removing this unused input helper through a reviewed PR; no Store application was published and no user installation is modified here.
