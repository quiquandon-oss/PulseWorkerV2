# Staging collector -- local run on a PC (Windows PowerShell 5.1 or PowerShell 7). No Git required.
#
#   -Mode DryRun      G1-A + G1-B with LIVE public data. Writes NOTHING anywhere except local files:
#                     BTC row and history row are inserted only into an in-memory replica. No token needed.
#   -Mode SingleTick  The controlled STAGING collection test (requires migration 0020 applied to staging):
#                     verify -> open period -> 1 BTC tick -> 1 history tick -> replay both (must be duplicates)
#                     -> verify -> snapshot delta check -> readiness. Asks for the STAGING token (hidden input).
#
# Never uses CLOUDFLARE_API_TOKEN, never uses wrangler, never contacts a production Worker or database.
# Prerequisite: Python 3.11+ ("py -3 --version").  Run from an empty folder:
#   powershell -ExecutionPolicy Bypass -File .\run-local.ps1 -Sha <reviewed 40-char commit> -Mode DryRun
param(
  [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Sha,
  [ValidateSet('DryRun', 'SingleTick')][string]$Mode = 'DryRun'
)
$ErrorActionPreference = 'Stop'
$CpCommit = '0a1dfb8ce88883336ee2e712a84e49be857b1724'
$CpIndexSha256 = '06E97B2DD8C0623FF7A87640720512A19914E3762F2AEF13FF9514D803C9FE4A'
function Stop-Here($msg) { Write-Host "STOPPED: $msg" -ForegroundColor Red; exit 1 }
function Run-Step($label, $expected, [scriptblock]$cmd) {
  Write-Host "`n=== $label ===" -ForegroundColor Cyan
  & $cmd
  $code = $LASTEXITCODE
  if ($expected -notcontains $code) { Stop-Here "$label exited $code (expected $($expected -join ' or '))" }
  Write-Host "--> $label OK (exit $code)" -ForegroundColor Green
}

# 1. Code: the reviewed PulseWorkerV2 commit, and the pinned CryptoPulse page (public repos)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = "PulseWorkerV2-$Sha"
if (-not (Test-Path $Root)) {
  Invoke-WebRequest -UseBasicParsing "https://github.com/quiquandon-oss/PulseWorkerV2/archive/$Sha.zip" -OutFile "$Root.zip"
  Expand-Archive "$Root.zip" -DestinationPath . -Force
}
Set-Location $Root
Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/quiquandon-oss/CryptoPulse/$CpCommit/index.html" -OutFile cryptopulse-index.html
if ((Get-FileHash -Algorithm SHA256 cryptopulse-index.html).Hash -ne $CpIndexSha256) { Stop-Here "CryptoPulse index.html is not the pinned file" }
Write-Host "OK  pinned CryptoPulse index.html ($CpCommit)"

# 2. Private Python environment, Playwright Chromium, full collector test suite (offline)
if (-not (Test-Path .venv-collector)) { py -3 -m venv .venv-collector }
$py = ".\.venv-collector\Scripts\python.exe"
& $py -m pip install --quiet pytest pyyaml playwright==1.56.0
& $py -m playwright install chromium
$env:CRYPTOPULSE_INDEX_FILE = (Resolve-Path cryptopulse-index.html).Path
Run-Step 'Collector safety tests' @(0) { & $py -m pytest staging-collector/ -q -p no:cacheprovider }
Remove-Item Env:CLOUDFLARE_API_TOKEN -ErrorAction SilentlyContinue

if ($Mode -eq 'DryRun') {
  Run-Step 'G1-B: real BTC observation, local replica only' @(0, 6) { & $py staging-collector/collector.py btc-tick --dry-run }
  Run-Step 'G1-A: live composite capture (nothing sent)' @(0, 7) {
    & $py staging-collector/history_harness.py --index-file cryptopulse-index.html --out ..\capture-dryrun.json }
  Run-Step 'G1-A: validate capture, local replica only' @(0, 6, 7) {
    & $py staging-collector/collector.py history-ingest ..\capture-dryrun.json --dry-run }
  Set-Location ..
  Write-Host "`nDRY RUN COMPLETE. Send capture-dryrun.json (no credentials inside) for review." -ForegroundColor Green
  exit 0
}

# ---- SingleTick: STAGING writes (one BTC row, one history row, ledger entries, one period event) ----
# Stay inside one 30-minute slot so the replay must be a duplicate: wait if a slot boundary is < 6 min away.
$m = [DateTime]::UtcNow.Minute % 30
if ($m -ge 24) { $wait = (31 - $m) * 60; Write-Host "Waiting $wait s to start a fresh slot..."; Start-Sleep -Seconds $wait }

$sec = Read-Host -AsSecureString "Paste the STAGING Cloudflare API token (input hidden)"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$env:STAGING_COLLECTOR_CLOUDFLARE_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
$env:STAGING_COLLECTOR_ACCOUNT_ID = 'f58e761fbc8e62dc404d8684290af264'
$env:STAGING_COLLECTOR_DATABASE_NAME = 'pulseworker-v2-staging'
$env:STAGING_COLLECTOR_DATABASE_ID = '5458d504-2778-49ae-bd25-7751f1c49d50'
$env:STAGING_COLLECTOR_GIT_SHA = $Sha
$env:STAGING_COLLECTOR_RUN_ID = "local:$env:COMPUTERNAME:single-tick:$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
$period = "single-tick-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmm'))"
try {
  Start-Transcript -Path ..\single-tick.log | Out-Null
  Run-Step 'Verify (before)'                  @(0) { & $py staging-collector/collector.py verify --json-out ..\before.json }
  Run-Step "Open collection period $period"   @(0) { & $py staging-collector/collector.py open-period --period-id $period --note 'single-tick validation' }
  Run-Step 'BTC tick'                          @(0) { & $py staging-collector/collector.py btc-tick }
  Run-Step 'Capture composite (nothing sent)'  @(0) { & $py staging-collector/history_harness.py --index-file cryptopulse-index.html --out ..\capture.json }
  Run-Step 'History tick'                      @(0) { & $py staging-collector/collector.py history-ingest ..\capture.json }
  Run-Step 'BTC replay (must be duplicate)'    @(5) { & $py staging-collector/collector.py btc-tick }
  Run-Step 'History replay (must be duplicate)' @(5) { & $py staging-collector/collector.py history-ingest ..\capture.json }
  Run-Step 'Verify (after)'                   @(0) { & $py staging-collector/collector.py verify --json-out ..\after.json }
  Run-Step 'Snapshot delta (nothing else changed)' @(0) { & $py staging-collector/collector.py check-delta ..\before.json ..\after.json }
  Run-Step 'Readiness'                         @(0) { & $py staging-collector/collector.py status }
  Write-Host "`nSINGLE TICK VALIDATION PASSED. Send single-tick.log, before.json, after.json, capture.json." -ForegroundColor Green
} finally {
  Stop-Transcript | Out-Null
  Remove-Item Env:STAGING_COLLECTOR_CLOUDFLARE_API_TOKEN, Env:STAGING_COLLECTOR_ACCOUNT_ID, Env:STAGING_COLLECTOR_DATABASE_NAME, Env:STAGING_COLLECTOR_DATABASE_ID -ErrorAction SilentlyContinue
  Remove-Variable sec, bstr -ErrorAction SilentlyContinue
  Set-Location ..
}
