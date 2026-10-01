<#
.SYNOPSIS
    Lint the invokr extension and sign it through addons.mozilla.org (AMO).

.DESCRIPTION
    Firefox release builds only install add-ons that carry a Mozilla signature,
    so a temporary add-on loaded from about:debugging is discarded on every
    restart. Signing once produces an .xpi that installs permanently.

    The signature is requested on the "unlisted" channel: the add-on is
    self-distributed and signed automatically, without a listing on AMO.

    Credentials come from WEB_EXT_API_KEY / WEB_EXT_API_SECRET if they are
    already in the environment; otherwise they are read from the user-scope
    environment, so they never have to appear on the command line or in this
    repository. Generate them at:
    https://addons.mozilla.org/developers/addon/api/key/

.EXAMPLE
    .\tools\sign-extension.ps1

.EXAMPLE
    # Sign a different source directory into a different artifact folder.
    .\tools\sign-extension.ps1 -SourceDir extension -ArtifactsDir dist
#>
[CmdletBinding()]
param(
    [string] $SourceDir,
    [string] $ArtifactsDir,
    [string] $Channel = 'unlisted'
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 does not populate $PSScriptRoot while parameter
# defaults are still being evaluated, so resolve the layout here instead.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

if (-not $SourceDir) { $SourceDir = Join-Path $repoRoot 'extension' }
if (-not $ArtifactsDir) { $ArtifactsDir = Join-Path $repoRoot 'web-ext-artifacts' }

function Get-CredentialValue {
    param([Parameter(Mandatory = $true)][string] $Name)

    foreach ($scope in 'Process', 'User', 'Machine') {
        $value = [Environment]::GetEnvironmentVariable($Name, $scope)
        if ($value) { return $value }
    }
    return $null
}

$apiKey = Get-CredentialValue 'WEB_EXT_API_KEY'
$apiSecret = Get-CredentialValue 'WEB_EXT_API_SECRET'

if (-not $apiKey -or -not $apiSecret) {
    $missing = @()
    if (-not $apiKey) { $missing += 'WEB_EXT_API_KEY' }
    if (-not $apiSecret) { $missing += 'WEB_EXT_API_SECRET' }

    Write-Host ''
    Write-Host "Missing $($missing -join ' and ')." -ForegroundColor Red
    Write-Host ''
    Write-Host 'Generate API credentials at https://addons.mozilla.org/developers/addon/api/key/'
    Write-Host 'then store them once, in your user environment:'
    Write-Host ''
    Write-Host "  [Environment]::SetEnvironmentVariable('WEB_EXT_API_KEY','user:12345:67890','User')"
    Write-Host "  [Environment]::SetEnvironmentVariable('WEB_EXT_API_SECRET','<secret>','User')"
    Write-Host ''
    Write-Host 'The JWT issuer is the API key; the JWT secret is the secret.'
    Write-Host 'Open a new terminal after setting them, or this script will still read the old value.'
    exit 1
}

# web-ext reads these itself; no secret is ever passed as an argument.
$env:WEB_EXT_API_KEY = $apiKey
$env:WEB_EXT_API_SECRET = $apiSecret

# Keep npx's cache inside the repository so the run also works under a sandbox
# that only permits writes to the workspace.
$env:npm_config_cache = Join-Path $repoRoot '.npm-cache'

$source = (Resolve-Path -LiteralPath $SourceDir).Path
if (-not (Test-Path -LiteralPath $ArtifactsDir)) {
    New-Item -ItemType Directory -Force -Path $ArtifactsDir | Out-Null
}
$artifacts = (Resolve-Path -LiteralPath $ArtifactsDir).Path

# Note what is already here, so the freshly signed file can be identified even
# though AMO, not this script, chooses its name.
$before = @{}
Get-ChildItem -LiteralPath $artifacts -Filter '*.xpi' -ErrorAction SilentlyContinue |
    ForEach-Object { $before[$_.FullName] = $_.LastWriteTimeUtc }

Write-Host "Linting $source" -ForegroundColor Cyan
npx --yes web-ext@latest lint --source-dir=$source --output=text
if ($LASTEXITCODE -ne 0) {
    throw 'Lint reported errors. AMO would reject this build, so signing was skipped.'
}

Write-Host "Signing ($Channel)" -ForegroundColor Cyan
npx --yes web-ext@latest sign `
    --source-dir=$source `
    --artifacts-dir=$artifacts `
    --channel=$Channel
if ($LASTEXITCODE -ne 0) {
    throw 'Signing failed. The AMO response above says why.'
}

$all = @(Get-ChildItem -LiteralPath $artifacts -Filter '*.xpi')
$fresh = @($all | Where-Object {
    -not $before.ContainsKey($_.FullName) -or $before[$_.FullName] -ne $_.LastWriteTimeUtc
})

$xpi = @(($fresh + $all) | Sort-Object LastWriteTime -Descending | Select-Object -First 1)[0]
if (-not $xpi) {
    throw "web-ext reported success but no .xpi is in $artifacts"
}

# AMO names its output after its own record ID. The signature covers the archive
# contents rather than the filename, so also write the name a release should
# publish: invokr-<version>.xpi.
$version = (Get-Content -LiteralPath (Join-Path $source 'manifest.json') -Raw | ConvertFrom-Json).version
$release = Join-Path $artifacts "invokr-$version.xpi"
Copy-Item -LiteralPath $xpi.FullName -Destination $release -Force

Write-Host ''
Write-Host "Signed by AMO : $($xpi.FullName)" -ForegroundColor Green
Write-Host "Release asset : $release" -ForegroundColor Green
Write-Host ''
Write-Host 'Attach the release asset to a GitHub release, or install it directly:'
Write-Host 'about:addons > gear icon > Install Add-on From File...'
