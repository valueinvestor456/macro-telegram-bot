param(
    [Parameter(Mandatory=$true)][string]$NodePath,
    [Parameter(Mandatory=$true)][string]$WranglerPath,
    [switch]$UseExistingGhLogin
)
$ErrorActionPreference = 'Stop'
# Transfer credentials through stdin only: never command arguments, files or logs.
$dispatchToken = $env:MACRO_DISPATCH_TOKEN
if (!$dispatchToken -and $UseExistingGhLogin) {
    $dispatchToken = & gh auth token
    if ($LASTEXITCODE -ne 0) { throw 'GitHub authentication is unavailable' }
}
if (!$dispatchToken) { throw 'Provide MACRO_DISPATCH_TOKEN or explicitly select UseExistingGhLogin' }
$localConfig = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '../../config.json') | ConvertFrom-Json
if (!$localConfig.webhook_secret) { throw 'Local control credential is unavailable' }
$hash = [System.Security.Cryptography.SHA256]::Create()
try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($localConfig.webhook_secret + ':macro-summary-clock-v1')
    $controlToken = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()
    @{ GH_DISPATCH_TOKEN = $dispatchToken.Trim(); CONTROL_TOKEN = $controlToken } |
        ConvertTo-Json -Compress |
        & $NodePath $WranglerPath secret bulk --config (Join-Path $PSScriptRoot 'wrangler.jsonc')
    if ($LASTEXITCODE -ne 0) { throw 'Cloud scheduler secret provisioning failed' }
} finally {
    $hash.Dispose()
    $dispatchToken = $null
    $controlToken = $null
    $localConfig = $null
}
