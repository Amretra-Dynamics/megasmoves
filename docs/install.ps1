# Installs the same MegasMoves Windows product as the download button.
# Account setup stays inside the installed app. No credentials are accessed here.
[CmdletBinding()]
param(
    [uri]$BaseUri = 'https://megasmoves.io/',
    [switch]$VerifyOnly
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Megas Moves requires 64-bit Windows.' }
if (-not $BaseUri.IsAbsoluteUri -or $BaseUri.UserInfo -or $BaseUri.Query -or $BaseUri.Fragment -or $BaseUri.AbsolutePath -ne '/') {
    throw 'Use an absolute site origin without a path, query or user information.'
}
$isPreview = $BaseUri.IsLoopback
if ($BaseUri.Scheme -ne 'https' -and -not ($isPreview -and $BaseUri.Scheme -eq 'http')) {
    throw 'Installation requires HTTPS, except for a local preview.'
}
if (-not $isPreview -and $BaseUri.Host -ne 'megasmoves.io') { throw 'Use the official Megas Moves site.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
if (-not (Get-Command Invoke-WebRequest -ErrorAction SilentlyContinue)) {
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
}
$catalogUri = [uri]::new($BaseUri, 'downloads.json')
$response = Invoke-WebRequest -Uri $catalogUri -UseBasicParsing -TimeoutSec 30 -MaximumRedirection 0
if ([Text.Encoding]::UTF8.GetByteCount([string]$response.Content) -gt 262144) { throw 'The release catalog is too large.' }
$catalog = $response.Content | ConvertFrom-Json
$expectedScope = if ($isPreview) { 'local-development-preview' } else { 'public-release' }
if ($catalog.scope -ne $expectedScope) { throw 'A verified release catalog is not available for this site.' }
$installers = @($catalog.artifacts | Where-Object { $_.title -eq 'Windows installer' })
if ($installers.Count -ne 1) { throw 'The catalog must contain exactly one Windows installer.' }
$artifact = $installers[0]
$releasePattern = '^https://github\.com/Amretra-Dynamics/megasmoves/releases/download/[A-Za-z0-9._-]+/' + [regex]::Escape([string]$artifact.filename) + '$'
$releaseAsset = [string]$artifact.href -cmatch $releasePattern
if ($artifact.filename -notmatch '^MegasSetup-[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?\.exe$' -or
    $artifact.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
    (-not $releaseAsset -and $artifact.href -cne ('./downloads/' + $artifact.filename)) -or
    $artifact.size_bytes -isnot [ValueType] -or
    [double]$artifact.size_bytes -ne [math]::Floor([double]$artifact.size_bytes) -or
    [long]$artifact.size_bytes -lt 2 -or [long]$artifact.size_bytes -gt 2147483648) {
    throw 'The installer record is invalid.'
}
$downloadUri = if ($isPreview) {
    [uri]('http://127.0.0.1:3106/megasmoves/downloads/' + $artifact.filename)
} elseif ($releaseAsset) {
    [uri]$artifact.href
} else {
    [uri]::new($BaseUri, [string]$artifact.href)
}
$stagingDir = Join-Path ([IO.Path]::GetTempPath()) ('MegasMoves-setup-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stagingDir | Out-Null
$installerPath = Join-Path $stagingDir ([string]$artifact.filename)
try {
    Write-Host ('Downloading Megas Moves ' + $artifact.version + '...')
    # Official GitHub Releases redirects to its HTTPS asset CDN. Hash and length
    # are checked before execution; arbitrary catalog download hosts are rejected.
    $redirects = if ($releaseAsset -and -not $isPreview) { 3 } else { 0 }
    Invoke-WebRequest -Uri $downloadUri -UseBasicParsing -OutFile $installerPath -TimeoutSec 600 -MaximumRedirection $redirects
    if ((Get-Item -LiteralPath $installerPath).Length -ne [long]$artifact.size_bytes) { throw 'Installer size does not match the catalog.' }
    $hashStream = [IO.File]::OpenRead($installerPath)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $actualHash = [BitConverter]::ToString($hasher.ComputeHash($hashStream)).Replace('-', '').ToLowerInvariant() }
    finally { $hashStream.Dispose(); $hasher.Dispose() }
    if ($actualHash -ine $artifact.sha256) { throw 'Installer checksum does not match the catalog.' }
    $stream = [IO.File]::OpenRead($installerPath)
    try { if ($stream.ReadByte() -ne 77 -or $stream.ReadByte() -ne 90) { throw 'The installer is not a Windows executable.' } }
    finally { $stream.Dispose() }
    if ($VerifyOnly) {
        [pscustomobject]@{ verified = $true; version = $artifact.version; sha256 = $artifact.sha256; size_bytes = $artifact.size_bytes; installed = $false }
        return
    }
    Write-Host 'Verified. Installing Megas Moves in place...'
    $result = Start-Process -FilePath $installerPath -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -WindowStyle Hidden -PassThru
    if (-not $result.WaitForExit(600000)) { throw 'Installation exceeded the time limit; check the installer before running it again.' }
    if ($result.ExitCode -ne 0) { throw ('Installation failed with exit code ' + $result.ExitCode) }
    $appPath = Join-Path $env:LOCALAPPDATA 'MegasMoves\bin\MegasMoves.exe'
    $cliPath = Join-Path $env:LOCALAPPDATA 'MegasMoves\bin\megas.cmd'
    if (-not (Test-Path -LiteralPath $appPath -PathType Leaf) -or -not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
        throw 'Installation did not produce the expected app and terminal launcher.'
    }
    $env:PATH = (Split-Path -Parent $cliPath) + ';' + $env:PATH
    Start-Process -FilePath $appPath -ArgumentList @('--background') -WindowStyle Hidden
    Write-Host 'Megas Moves is installed. Open a new terminal and run megas --help, or open MegasMoves from Start.'
} finally {
    # Remove only the exact files created by this run. No recursive deletion or data migration.
    if (Test-Path -LiteralPath $installerPath -PathType Leaf) { Remove-Item -LiteralPath $installerPath -Force }
    if (Test-Path -LiteralPath $stagingDir -PathType Container) { Remove-Item -LiteralPath $stagingDir -Force }
}
