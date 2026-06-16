<#
  STS Advisor - setup / configurer
  --------------------------------
  Points CommunicationMod at the bridge in THIS folder and sanity-checks the
  prerequisites. Safe to re-run. No admin rights needed.

  Run it:  right-click -> "Run with PowerShell", or from a terminal:
           powershell -ExecutionPolicy Bypass -File install.ps1
#>
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

Write-Host ""
Write-Host "=== STS Advisor setup ===" -ForegroundColor Cyan
Write-Host "Project folder: $here"
Write-Host ""

# 1) Bridge file present? -----------------------------------------------------
$bridge = Join-Path $here 'sts_advisor.py'
if (-not (Test-Path $bridge)) {
    Write-Host "ERROR: sts_advisor.py is not next to this script." -ForegroundColor Red
    Write-Host "Run install.ps1 from inside the extracted project folder." -ForegroundColor Red
    exit 1
}

# 2) Python (the bridge is launched via the Python launcher) ------------------
$pyCmd = $null
foreach ($c in @('py', 'python')) {
    try {
        $v = (& $c --version) 2>&1
        if ($LASTEXITCODE -eq 0) { $pyCmd = $c; Write-Host "[ok] Python: $v (via '$c')" -ForegroundColor Green; break }
    } catch {}
}
if (-not $pyCmd) {
    Write-Host "[x] Python 3 not found." -ForegroundColor Red
    Write-Host "    Install from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run." -ForegroundColor Red
    exit 1
}

# 3) Claude Code CLI ----------------------------------------------------------
$claudeOk = $false
try {
    $cv = (& claude --version) 2>&1
    if ($LASTEXITCODE -eq 0) { $claudeOk = $true; Write-Host "[ok] Claude Code: $cv" -ForegroundColor Green }
} catch {}
if (-not $claudeOk) {
    Write-Host "[x] Claude Code CLI not found." -ForegroundColor Red
    Write-Host "    Install it from https://docs.claude.com/claude-code , then run 'claude' once and log in." -ForegroundColor Red
    exit 1
}
Write-Host "[!] Make sure you've run 'claude' once and logged in (Max or Pro). This advisor uses YOUR Claude subscription." -ForegroundColor Yellow

# 4) Build the launch command (handle spaces in the path via 8.3 short path) --
$scriptPath = $bridge
if ($scriptPath -match '\s') {
    try {
        $fso = New-Object -ComObject Scripting.FileSystemObject
        $scriptPath = $fso.GetFile($scriptPath).ShortPath
    } catch {
        Write-Host "[!] Path has spaces and short-path lookup failed. Move the folder somewhere without spaces (e.g. C:\Tools\sts-advisor)." -ForegroundColor Yellow
    }
}
$cmd = "$pyCmd " + ($scriptPath -replace '\\', '/')

# 5) Write the CommunicationMod config (merge with any existing keys) ----------
$cfgDir  = Join-Path $env:LOCALAPPDATA 'ModTheSpire\CommunicationMod'
$cfgFile = Join-Path $cfgDir 'config.properties'
New-Item -ItemType Directory -Force $cfgDir | Out-Null

$props = [ordered]@{}
if (Test-Path $cfgFile) {
    Get-Content $cfgFile | ForEach-Object {
        if ($_ -match '^\s*#') { return }
        if ($_ -match '^\s*([^=]+?)\s*=\s*(.*)$') { $props[$matches[1]] = $matches[2] }
    }
}
$props['command']        = $cmd
$props['runAtGameStart'] = 'true'
if (-not $props.Contains('verbose'))                 { $props['verbose'] = 'true' }
if (-not $props.Contains('maxInitializationTimeout')){ $props['maxInitializationTimeout'] = '10' }

$lines = @("#Written by STS Advisor install.ps1")
foreach ($k in $props.Keys) { $lines += "$k=$($props[$k])" }
Set-Content -Path $cfgFile -Value $lines -Encoding ascii

Write-Host ""
Write-Host "[ok] Configured CommunicationMod:" -ForegroundColor Green
Write-Host "     $cfgFile"
Write-Host "     command=$cmd"

# 6) Next steps ---------------------------------------------------------------
Write-Host ""
Write-Host "=== Almost there - finish these by hand ===" -ForegroundColor Cyan
Write-Host "1) In Steam, subscribe to these Workshop items, then launch once via ModTheSpire to install them:"
Write-Host "     ModTheSpire       https://steamcommunity.com/sharedfiles/filedetails/?id=1605060445"
Write-Host "     BaseMod           https://steamcommunity.com/sharedfiles/filedetails/?id=1605833019"
Write-Host "     CommunicationMod  https://steamcommunity.com/sharedfiles/filedetails/?id=2131373661"
Write-Host "2) Confirm you're logged into Claude Code:  claude   (then /login if needed)"
Write-Host "3) Launch Slay the Spire via ModTheSpire with BaseMod + CommunicationMod enabled."
Write-Host "4) (Optional) advice overlay window:  $pyCmd `"$here\sts_viewer.py`""
Write-Host ""
Write-Host "Advice + logs will appear in:  $here\state\" -ForegroundColor Green
Write-Host "Done. Climb well." -ForegroundColor Cyan
