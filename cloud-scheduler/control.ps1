param(
    [Parameter(Mandatory=$true)][string]$BaseUrl,
    [ValidateSet('status','tick','check')][string]$Operation = 'status'
)
$ErrorActionPreference = 'Stop'
$uri = [Uri]$BaseUrl
if ($uri.Scheme -ne 'https' -or $uri.Host -notmatch '^macro-summary-clock\.[a-z0-9-]+\.workers\.dev$') {
    throw 'Expected the deployed macro-summary-clock workers.dev URL'
}
$localConfig = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '../../config.json') | ConvertFrom-Json
if (!$localConfig.webhook_secret) { throw 'Local control credential is unavailable' }
$hash = [System.Security.Cryptography.SHA256]::Create()
try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($localConfig.webhook_secret + ':macro-summary-clock-v1')
    $controlToken = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()
    $method = if ($Operation -in @('tick','check')) {'POST'} else {'GET'}
    $result = Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + '/' + $Operation) -Method $method `
        -Headers @{Authorization = 'Bearer ' + $controlToken} -TimeoutSec 60
    $result | ConvertTo-Json -Depth 10
} finally {
    $hash.Dispose()
    $controlToken = $null
    $localConfig = $null
}
