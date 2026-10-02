$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = Join-Path $env:RUNNER_TEMP ('omnidesk-store-tests-' + [guid]::NewGuid().ToString('N'))
$assets = Join-Path $root 'assets'
[void][IO.Directory]::CreateDirectory($assets)
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$source = Join-Path $root 'Fixture.cs'
[IO.File]::WriteAllText($source, 'public class Fixture { public static void Main() {} }')
$executable = Join-Path $root 'fixture.exe'
& $compiler /nologo /platform:x64 /target:winexe "/out:$executable" $source
if ($LASTEXITCODE -ne 0) { throw 'Failed to compile cloud-only packaging fixture.' }
Add-Type -AssemblyName System.Drawing
foreach ($entry in @(@('Square44x44Logo.png', 44), @('Square150x150Logo.png', 150), @('StoreLogo.png', 50))) {
    $bitmap = [Drawing.Bitmap]::new([int]$entry[1], [int]$entry[1])
    try { $bitmap.Save((Join-Path $assets $entry[0]), [Drawing.Imaging.ImageFormat]::Png) } finally { $bitmap.Dispose() }
}
$params = @{
    Executable = $executable
    Assets = $assets
    IdentityName = 'OmniDesk.CITestOnly'
    Publisher = 'CN=00000000-0000-0000-0000-000000000000'
    DisplayName = 'Test < & > Only'
    PublisherDisplayName = 'Test & Only Publisher'
    Version = '1.12.7.0'
    SourceCommit = $env:GITHUB_SHA
    OutputDirectory = (Join-Path $root 'positive')
}
$script = Join-Path $PSScriptRoot 'package_windows_store_msix.ps1'
& $script @params
$record = Get-Content -Raw (Join-Path $params.OutputDirectory 'store-package-input.json') | ConvertFrom-Json
if ($record.status -ne 'packaged-unsigned-store-input' -or $record.source_commit -ne $env:GITHUB_SHA) { throw 'Package scope/source binding lost.' }
$package = Join-Path $params.OutputDirectory 'OmniDesk.CITestOnly.msix'
if ($record.package_sha256 -ne (Get-FileHash $package -Algorithm SHA256).Hash.ToLowerInvariant()) { throw 'Package digest mismatch.' }
$zip = [IO.Compression.ZipFile]::OpenRead($package)
try {
    if ($null -ne $zip.GetEntry('AppxSignature.p7x')) { throw 'Fixture must never be labeled Store-signed.' }
    $reader = [IO.StreamReader]::new($zip.GetEntry('AppxManifest.xml').Open())
    try { [xml]$manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
    if ($manifest.Package.Properties.DisplayName -ne $params.DisplayName) { throw 'XML special-character escaping failed.' }
} finally { $zip.Dispose() }
$cases = @(
    @{ Name = 'overwrite'; Patch = @{} },
    @{ Name = 'non-store-version'; Patch = @{ Version = '1.0.0.1'; OutputDirectory = (Join-Path $root 'bad-version') } },
    @{ Name = 'version-overflow'; Patch = @{ Version = '65536.0.0.0'; OutputDirectory = (Join-Path $root 'overflow') } },
    @{ Name = 'not-pe'; Patch = @{ Executable = $source; OutputDirectory = (Join-Path $root 'not-pe') } },
    @{ Name = 'identity-path-injection'; Patch = @{ IdentityName = '../escape'; OutputDirectory = (Join-Path $root 'bad-name') } }
)
foreach ($case in $cases) {
    $negative = $params.Clone()
    foreach ($key in $case.Patch.Keys) { $negative[$key] = $case.Patch[$key] }
    $rejected = $false
    try { & $script @negative } catch { $rejected = $true }
    if (-not $rejected) { throw "Negative control was accepted: $($case.Name)" }
    Write-Output "PASS $($case.Name)"
}
Write-Output 'PASS package schema, digest, XML escaping and unsigned classification; fixture was not executed.'
