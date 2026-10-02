$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repo = Split-Path $PSScriptRoot -Parent
$output = Join-Path $env:RUNNER_TEMP 'windows-store-client'
$assets = Join-Path $output 'Assets'
[void][IO.Directory]::CreateDirectory($assets)
$executable = Join-Path $repo 'apps/desktop-tauri/src-tauri/target/release/omnidesk_desktop.exe'
$config = Get-Content -Raw (Join-Path $repo 'apps/desktop-tauri/src-tauri/tauri.conf.json') | ConvertFrom-Json
$version = "$($config.version).0"
$provided = @($env:STORE_IDENTITY_NAME, $env:STORE_PUBLISHER, $env:STORE_PUBLISHER_DISPLAY_NAME)
$providedCount = @($provided | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($providedCount -ne 0 -and $providedCount -ne 3) { throw 'Provide all three actual identity fields, or none for preflight.' }
$synthetic = $providedCount -eq 0
$identity = if ($synthetic) { 'OmniDesk.PreflightOnly' } else { $env:STORE_IDENTITY_NAME }
$publisher = if ($synthetic) { 'CN=00000000-0000-0000-0000-000000000000' } else { $env:STORE_PUBLISHER }
$publisherDisplay = if ($synthetic) { 'TEST IDENTITY - NOT FOR SUBMISSION' } else { $env:STORE_PUBLISHER_DISPLAY_NAME }
Add-Type -AssemblyName System.Drawing
$image = [Drawing.Image]::FromFile((Join-Path $repo 'apps/desktop-tauri/src-tauri/icons/icon.png'))
try {
    foreach ($entry in @(@('Square44x44Logo.png', 44), @('Square150x150Logo.png', 150), @('StoreLogo.png', 50))) {
        $bitmap = [Drawing.Bitmap]::new([int]$entry[1], [int]$entry[1])
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.DrawImage($image, 0, 0, [int]$entry[1], [int]$entry[1])
            $bitmap.Save((Join-Path $assets $entry[0]), [Drawing.Imaging.ImageFormat]::Png)
        } finally { $graphics.Dispose(); $bitmap.Dispose() }
    }
} finally { $image.Dispose() }
& (Join-Path $PSScriptRoot 'package_windows_store_msix.ps1') -Executable $executable -Assets $assets `
    -IdentityName $identity -Publisher $publisher -DisplayName $config.productName `
    -PublisherDisplayName $publisherDisplay -Version $version -SourceCommit $env:GITHUB_SHA -OutputDirectory $output
$package = Join-Path $output "$identity.msix"
$zip = [IO.Compression.ZipFile]::OpenRead($package)
try {
    if ($null -ne $zip.GetEntry('AppxSignature.p7x')) { throw 'Unsigned input unexpectedly contains a signature.' }
    $entry = $zip.GetEntry('omnidesk_desktop.exe')
    if ($null -eq $entry) { throw 'Real client is absent from the package.' }
    $stream = $entry.Open()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $embeddedHash = [Convert]::ToHexString($sha.ComputeHash($stream)).ToLowerInvariant() } finally { $sha.Dispose(); $stream.Dispose() }
    if ($embeddedHash -ne (Get-FileHash $executable -Algorithm SHA256).Hash.ToLowerInvariant()) { throw 'Packaged client differs from release build.' }
    $reader = [IO.StreamReader]::new($zip.GetEntry('AppxManifest.xml').Open())
    try { [xml]$manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
    if ($manifest.Package.Identity.Name -ne $identity -or $manifest.Package.Identity.Publisher -ne $publisher -or $manifest.Package.Identity.Version -ne $version) { throw 'Package identity/version mismatch.' }
    if ($manifest.Package.Applications.Application.Executable -ne 'omnidesk_desktop.exe') { throw 'Wrong launch target.' }
} finally { $zip.Dispose() }
$record = [ordered]@{
    schema = 'omnidesk-windows-store-client-preflight/v1'
    source_commit = $env:GITHUB_SHA
    build_run = "$env:GITHUB_SERVER_URL/$env:GITHUB_REPOSITORY/actions/runs/$env:GITHUB_RUN_ID"
    synthetic_identity = $synthetic
    submission_ready = $false
    executable_sha256 = $embeddedHash
    package_sha256 = (Get-FileHash $package -Algorithm SHA256).Hash.ToLowerInvariant()
    package_payload_verified = $true
    unpackaged_cloud_launch = $false
    installed_package_verified = $false
    store_certification_verified = $false
    native_publisher_signing_verified = $false
    production_device_enrollment_verified = $false
    customer_ga = $false
    scope = 'Actual embedded release client, SDK packaging, payload digest and unpackaged Windows cloud launch only.'
}
$process = $null
try {
    $process = Start-Process -FilePath $executable -PassThru -RedirectStandardOutput (Join-Path $output 'launch.stdout.log') -RedirectStandardError (Join-Path $output 'launch.stderr.log')
    $windowTitle = ''
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Seconds 1
        $process.Refresh()
        if ($process.HasExited) { throw "Client exited during startup: $($process.ExitCode)" }
        $windowTitle = $process.MainWindowTitle
        if ($windowTitle -eq 'Omni Desktop Runtime') { break }
    }
    if ($windowTitle -ne 'Omni Desktop Runtime') { throw 'Client did not create the expected Windows window.' }
    Start-Sleep -Seconds 5
    $process.Refresh()
    if ($process.HasExited) { throw 'Client exited after creating its window.' }
    $record.unpackaged_cloud_launch = $true
    $record.window_title = $windowTitle
    Write-Output 'PASS real release client created and sustained expected Windows window; not MSIX installation or Store certification.'
} finally {
    $record | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $output 'client-preflight.json') -Encoding utf8
    if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id }
}
$files = @(Get-ChildItem $output -File -Recurse | Sort-Object FullName)
$lines = foreach ($file in $files) { "$((Get-FileHash $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant())  $([IO.Path]::GetRelativePath($output, $file.FullName).Replace('\', '/'))" }
$lines | Set-Content (Join-Path $output 'SHA256SUMS') -Encoding utf8
