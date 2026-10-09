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
$library = Join-Path $root 'fixture.dll'
& $compiler /nologo /platform:x64 /target:library "/out:$library" $source
if ($LASTEXITCODE -ne 0) { throw 'Failed to compile DLL rejection fixture.' }
$originalBytes = [IO.File]::ReadAllBytes($executable)
$peOffset = [BitConverter]::ToInt32($originalBytes, 0x3C)
$nonExecutable = Join-Path $root 'non-executable.exe'
$invalidOptionalHeader = Join-Path $root 'invalid-optional-header.exe'
$missingEntryPoint = Join-Path $root 'missing-entry-point.exe'
$bytes = [byte[]]$originalBytes.Clone()
$bytes[$peOffset + 22] = $bytes[$peOffset + 22] -band 0xFD
[IO.File]::WriteAllBytes($nonExecutable, $bytes)
$bytes = [byte[]]$originalBytes.Clone()
$bytes[$peOffset + 24] = 0
$bytes[$peOffset + 25] = 0
[IO.File]::WriteAllBytes($invalidOptionalHeader, $bytes)
$bytes = [byte[]]$originalBytes.Clone()
[Array]::Clear($bytes, $peOffset + 40, 4)
[IO.File]::WriteAllBytes($missingEntryPoint, $bytes)
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
# Replace the source after staging to exercise the package-to-evidence race deterministically.
try {
    & {
        function Get-FileHash {
            param([string]$LiteralPath, [string]$Algorithm)
            [IO.File]::WriteAllText($executable, 'changed-after-copy')
            Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $LiteralPath -Algorithm $Algorithm
        }
        & $script @params
    }
} finally { [IO.File]::WriteAllBytes($executable, $originalBytes) }
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
    $stream = $zip.GetEntry('omnidesk_desktop.exe').Open()
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $executableDigest = [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() } finally { $stream.Dispose(); $hash.Dispose() }
    if ($record.executable_sha256 -ne $executableDigest) { throw 'Executable evidence must hash the exact packaged bytes despite source replacement.' }
} finally { $zip.Dispose() }
$cases = @(
    @{ Name = 'overwrite'; Patch = @{} },
    @{ Name = 'non-store-version'; Patch = @{ Version = '1.0.0.1'; OutputDirectory = (Join-Path $root 'bad-version') } },
    @{ Name = 'version-overflow'; Patch = @{ Version = '65536.0.0.0'; OutputDirectory = (Join-Path $root 'overflow') } },
    @{ Name = 'not-pe'; Patch = @{ Executable = $source; OutputDirectory = (Join-Path $root 'not-pe') } },
    @{ Name = 'dll'; Patch = @{ Executable = $library; OutputDirectory = (Join-Path $root 'dll') }; ExpectedMessage = 'Input must be an executable image, not a DLL.' },
    @{ Name = 'non-executable-image'; Patch = @{ Executable = $nonExecutable; OutputDirectory = (Join-Path $root 'non-executable') }; ExpectedMessage = 'Input must be an executable image, not a DLL.' },
    @{ Name = 'invalid-optional-header'; Patch = @{ Executable = $invalidOptionalHeader; OutputDirectory = (Join-Path $root 'bad-optional') }; ExpectedMessage = 'Input must have a complete x64 PE32+ optional header.' },
    @{ Name = 'missing-entry-point'; Patch = @{ Executable = $missingEntryPoint; OutputDirectory = (Join-Path $root 'missing-entry') }; ExpectedMessage = 'Input executable must have an entry point.' },
    @{ Name = 'identity-path-injection'; Patch = @{ IdentityName = '../escape'; OutputDirectory = (Join-Path $root 'bad-name') } }
)
foreach ($case in $cases) {
    $negative = $params.Clone()
    foreach ($key in $case.Patch.Keys) { $negative[$key] = $case.Patch[$key] }
    $rejected = $false
    try { & $script @negative } catch {
        if ($case.ContainsKey('ExpectedMessage') -and $_.Exception.Message -ne $case.ExpectedMessage) { throw }
        $rejected = $true
    }
    if (-not $rejected) { throw "Negative control was accepted: $($case.Name)" }
    Write-Output "PASS $($case.Name)"
}
Write-Output 'PASS package schema, staged executable digest after source replacement, PE rejection, XML escaping and unsigned classification; fixture was not executed.'

$tokens = $null
$errors = $null
$client = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'verify_windows_store_client.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'Client verifier PowerShell syntax is invalid.' }
$cleanup = $client.Find({
    param($node)
    $node -is [Management.Automation.Language.TryStatementAst] -and $null -ne $node.Finally -and
        $node.Finally.Extent.Text.Contains('Client cleanup timed out.')
}, $true).Finally.Extent.Text
$cleanup = [scriptblock]::Create($cleanup.Substring(1, $cleanup.Length - 2))
$output = Join-Path $root 'client-cleanup'
[void][IO.Directory]::CreateDirectory($output)
$record = @{ kind = 'redirected-process-cleanup-control'; customer_ga = $false }
$env:OMNIDESK_CLEANUP_READY = Join-Path $output 'ready'
$childCommand = @'
[Console]::WriteLine("stdout-marker")
[Console]::Error.WriteLine("stderr-marker")
$child = [Diagnostics.Process]::Start((Join-Path $PSHOME "pwsh.exe"), "-NoProfile -NonInteractive -Command Start-Sleep -Seconds 30")
[IO.File]::WriteAllText($env:OMNIDESK_CLEANUP_READY, $child.Id.ToString())
Start-Sleep -Seconds 30
'@
$encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childCommand))
$process = Start-Process -FilePath (Get-Process -Id $PID).Path -PassThru -ArgumentList @(
    '-NoProfile', '-NonInteractive', '-EncodedCommand', $encodedCommand
) -RedirectStandardOutput (Join-Path $output 'launch.stdout.log') -RedirectStandardError (Join-Path $output 'launch.stderr.log')
try {
    for ($attempt = 0; $attempt -lt 100 -and -not (Test-Path $env:OMNIDESK_CLEANUP_READY); $attempt++) { Start-Sleep -Milliseconds 100 }
    if (-not (Test-Path $env:OMNIDESK_CLEANUP_READY)) { throw 'Redirected cleanup child did not start.' }
} finally {
    & $cleanup
    Remove-Item Env:OMNIDESK_CLEANUP_READY
}
foreach ($stream in @('stdout', 'stderr')) {
    $log = Join-Path $output "launch.$stream.log"
    if ((Get-FileHash $log -Algorithm SHA256).Hash.Length -ne 64) { throw "Cleanup left $stream locked." }
    if ((Get-Content -Raw $log) -notmatch "$stream-marker") { throw "Cleanup did not drain $stream." }
}
$descendant = Get-Process -Id ([int](Get-Content -Raw (Join-Path $output 'ready'))) -ErrorAction SilentlyContinue
if ($null -ne $descendant) { throw 'Cleanup left a descendant holding inherited stream handles.' }
Write-Output 'PASS real redirected process tree cleanup drains both streams and releases logs before hashing.'
