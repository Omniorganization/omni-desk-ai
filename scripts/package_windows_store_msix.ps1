param(
    [Parameter(Mandatory)][string]$Executable,
    [Parameter(Mandatory)][string]$Assets,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9.-]{3,50}$')][string]$IdentityName,
    [Parameter(Mandatory)][ValidatePattern('^CN=.+$')][string]$Publisher,
    [Parameter(Mandatory)][string]$DisplayName,
    [Parameter(Mandatory)][string]$PublisherDisplayName,
    [Parameter(Mandatory)][ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+\.0$')][string]$Version,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit,
    [Parameter(Mandatory)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
foreach ($component in $Version.Split('.')) {
    if ([decimal]$component -gt 65535) { throw 'MSIX version component exceeds 65535.' }
}
if ($Version.StartsWith('0.')) { throw 'MSIX major version must be greater than zero.' }
$executablePath = (Resolve-Path -LiteralPath $Executable).Path
$sdkTools = @(Get-ChildItem -Path "${env:ProgramFiles(x86)}/Windows Kits/10/bin/*/x64/makeappx.exe" | Sort-Object FullName -Descending)
if ($sdkTools.Count -eq 0) { throw 'Official Windows SDK MakeAppx is required.' }
$makeappx = $sdkTools[0].FullName
foreach ($logo in @('Square44x44Logo.png', 'Square150x150Logo.png', 'StoreLogo.png')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Assets $logo) -PathType Leaf)) { throw "Missing Store asset: $logo" }
}
[void][IO.Directory]::CreateDirectory($OutputDirectory)
$packagePath = Join-Path $OutputDirectory "$IdentityName.msix"
$evidencePath = Join-Path $OutputDirectory 'store-package-input.json'
if ((Test-Path -LiteralPath $packagePath) -or (Test-Path -LiteralPath $evidencePath)) { throw 'Refusing to overwrite an existing package or evidence record.' }
$stage = Join-Path ([IO.Path]::GetTempPath()) ("omnidesk-msix-" + [guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory((Join-Path $stage 'Assets'))
try {
    Copy-Item -LiteralPath $executablePath -Destination (Join-Path $stage 'omnidesk_desktop.exe')
    $reader = [IO.BinaryReader]::new([IO.File]::OpenRead((Join-Path $stage 'omnidesk_desktop.exe')))
    try {
        if ($reader.BaseStream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) { throw 'Input is not a Windows PE executable.' }
        $reader.BaseStream.Position = 0x3C
        $peOffset = $reader.ReadUInt32()
        if ($peOffset -lt 64 -or $peOffset -gt $reader.BaseStream.Length - 24) { throw 'Invalid PE header offset.' }
        $reader.BaseStream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x4550 -or $reader.ReadUInt16() -ne 0x8664) { throw 'Input must be an x64 Windows PE executable.' }
        $reader.BaseStream.Position = $peOffset + 20
        $optionalSize = $reader.ReadUInt16()
        $characteristics = $reader.ReadUInt16()
        if (($characteristics -band 0x0002) -eq 0 -or ($characteristics -band 0x2000) -ne 0) { throw 'Input must be an executable image, not a DLL.' }
        if ($optionalSize -lt 112 -or $optionalSize -gt $reader.BaseStream.Length - $peOffset - 24 -or $reader.ReadUInt16() -ne 0x020B) { throw 'Input must have a complete x64 PE32+ optional header.' }
        # Managed x64 EXEs can use a CLR entry token with no native PE entry point.
    } finally { $reader.Dispose() }
    foreach ($logo in @('Square44x44Logo.png', 'Square150x150Logo.png', 'StoreLogo.png')) {
        Copy-Item -LiteralPath (Join-Path $Assets $logo) -Destination (Join-Path $stage 'Assets' $logo)
    }
    $nameXml = [Security.SecurityElement]::Escape($IdentityName)
    $publisherXml = [Security.SecurityElement]::Escape($Publisher)
    $displayXml = [Security.SecurityElement]::Escape($DisplayName)
    $publisherDisplayXml = [Security.SecurityElement]::Escape($PublisherDisplayName)
    $manifest = @"
<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10" xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10" xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities" IgnorableNamespaces="uap rescap">
  <Identity Name="$nameXml" Publisher="$publisherXml" Version="$Version" ProcessorArchitecture="x64" />
  <Properties><DisplayName>$displayXml</DisplayName><PublisherDisplayName>$publisherDisplayXml</PublisherDisplayName><Logo>Assets\StoreLogo.png</Logo></Properties>
  <Dependencies><TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.19041.0" MaxVersionTested="10.0.26100.0" /></Dependencies>
  <Resources><Resource Language="en-us" /><Resource Language="zh-cn" /></Resources>
  <Applications><Application Id="App" Executable="omnidesk_desktop.exe" EntryPoint="Windows.FullTrustApplication"><uap:VisualElements DisplayName="$displayXml" Description="$displayXml" BackgroundColor="transparent" Square150x150Logo="Assets\Square150x150Logo.png" Square44x44Logo="Assets\Square44x44Logo.png" /></Application></Applications>
  <Capabilities><rescap:Capability Name="runFullTrust" /></Capabilities>
</Package>
"@
    [IO.File]::WriteAllText((Join-Path $stage 'AppxManifest.xml'), $manifest, [Text.UTF8Encoding]::new($false))
    & $makeappx pack /d $stage /p $packagePath
    if ($LASTEXITCODE -ne 0) { throw "MakeAppx schema/package validation failed: $LASTEXITCODE" }
    $evidence = [ordered]@{
        schema = 'omnidesk-windows-store-input/v1'
        status = 'packaged-unsigned-store-input'
        source_commit = $SourceCommit
        executable_sha256 = (Get-FileHash -LiteralPath (Join-Path $stage 'omnidesk_desktop.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
        package_sha256 = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
        identity_name = $IdentityName
        publisher = $Publisher
        version = $Version
        identity_verification = 'not-performed; use actual Partner Center identity before submission'
        signing_verification = 'not-performed; Microsoft Store certification/signature is still required'
        webview2_requirement = 'system WebView2 runtime must be independently verified; not bundled'
        policy = 'Not install/distribution/Authenticode/Store acceptance or Real GA evidence.'
    }
    $evidence | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $evidencePath -Encoding utf8
    Write-Output $packagePath
} finally {
    # Only delete this helper's fresh GUID scratch directory, never user outputs.
    Remove-Item -LiteralPath $stage -Recurse -Force
}
