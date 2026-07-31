# Agent Brain - publish to GitHub.
#
# Run this instead of typing git commands. It checks everything first, explains what it
# is doing, and STOPS if anything looks wrong. It will not push without asking.
#
#   Right-click this file -> "Run with PowerShell"
#   or:  powershell -ExecutionPolicy Bypass -File publish.ps1
#
# ASCII ONLY IN THIS FILE. PowerShell 5.1 reads BOM-less files as ANSI; a UTF-8 dash
# becomes a stray quote and breaks the whole script. Keep every character below 0x80.

# Continue, not Stop: with Stop, ANY unexpected error kills the script instantly and
# the window closes before you can read why. Every step checks its own result instead.
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

# Always leave evidence, even if the window vanishes for a reason we did not predict.
try { Start-Transcript -Path (Join-Path $PSScriptRoot "publish-log.txt") -Force | Out-Null } catch {}

function Finish ($code) {
  try { Stop-Transcript | Out-Null } catch {}
  Write-Host ""
  Write-Host "  A copy of everything above was saved to publish-log.txt" -ForegroundColor DarkGray
  Write-Host "  If something went wrong, send that file." -ForegroundColor DarkGray
  Write-Host ""
  Read-Host "Press Enter to close"
  exit $code
}

trap {
  Write-Host ""
  Write-Host "  UNEXPECTED ERROR:" -ForegroundColor Red
  Write-Host "  $_" -ForegroundColor Red
  Write-Host "  at line $($_.InvocationInfo.ScriptLineNumber)" -ForegroundColor DarkGray
  Finish 1
}

function Say  ($m) { Write-Host $m -ForegroundColor Cyan }
function Good ($m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  !!   $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host ""; Write-Host "  STOPPED: $m" -ForegroundColor Red;
                     Write-Host ""; Finish 1 }

Write-Host ""
Write-Host "  Agent Brain - publish to GitHub" -ForegroundColor White
Write-Host "  ------------------------------------------------------------"
Write-Host "  Nothing is uploaded until you confirm at the end."
Write-Host ""

# --- 1. tools present? ------------------------------------------------------
Say "[1/7] Checking your tools..."
try { $gitv = (git --version) } catch {
  Die "git is not installed, or not on PATH. Install it from https://git-scm.com/download/win then run this again."
}
Good $gitv
try { $pyv = (python --version 2>&1) } catch { Die "python is not on PATH." }
Good $pyv

# --- 2. tests ---------------------------------------------------------------
Say "[2/7] Running the test suite (this is the first time it runs on YOUR machine)..."
python -m unittest discover -s tests 2>&1 | Select-Object -Last 4
if ($LASTEXITCODE -ne 0) {
  Die "Tests failed. Do not publish yet - copy the output above and ask about it."
}
Good "tests passed"

python tools/selfcheck.py | Select-Object -Last 1
if ($LASTEXITCODE -ne 0) { Warn "selfcheck found something - not fatal, but worth a look" }
else { Good "selfcheck clean" }

# --- 3. github username -----------------------------------------------------
Say "[3/7] Your GitHub username"
$user = Read-Host "  Type your GitHub username (the name in github.com/NAME)"
if ([string]::IsNullOrWhiteSpace($user)) { Die "No username given." }
$files = @("pyproject.toml", "CHANGELOG.md", ".github\ISSUE_TEMPLATE\config.yml", "README.md")
foreach ($f in $files) {
  if (Test-Path $f) {
    $t = Get-Content $f -Raw
    if ($t -match "YOURNAME") {
      ($t -replace "YOURNAME", $user) | Set-Content $f -NoNewline -Encoding UTF8
      Good "updated $f"
    }
  }
}

# --- 4. init ----------------------------------------------------------------
Say "[4/7] Preparing the repository..."
if (Test-Path ".git") { Good "already a git repository" }
else { git init | Out-Null; Good "created a new git repository" }
git add -A

# --- 5. THE SAFETY CHECK ----------------------------------------------------
Say "[5/7] Checking that none of your private data is about to be committed..."
$staged = git diff --cached --name-only
$bad = $staged | Where-Object {
  $_ -match "^notes/" -or $_ -match "^vault/" -or $_ -match "^graph/" -or
  $_ -match "\.brain/" -or $_ -match "settings\.json$" -or $_ -match "\.jsonl$" -or
  $_ -match "key\.dpapi$" -or $_ -match "^\.env"
}
if ($bad) {
  Write-Host ""
  Write-Host "  These look like PRIVATE files and must not be published:" -ForegroundColor Red
  $bad | ForEach-Object { Write-Host "     $_" -ForegroundColor Red }
  git reset | Out-Null
  Die "Nothing was committed. The .gitignore is not working - ask about this before retrying."
}
Good "no conversation data, no keys, no settings"

$secretHits = git diff --cached -U0 |
  Select-String -Pattern 'sk-or-v1-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{35}|ghp_[A-Za-z0-9]{20,}' |
  Where-Object { $_ -notmatch "SECRET_RES|re\.compile|REDACTED|CANARY" }
if ($secretHits) {
  Write-Host ""
  $secretHits | ForEach-Object { Write-Host "     $_" -ForegroundColor Red }
  git reset | Out-Null
  Die "Something that looks like an API key is in the files. Nothing was committed."
}
Good "no API keys found"

$count = ($staged | Measure-Object).Count
Good "$count files ready to publish"

# --- 6. commit --------------------------------------------------------------
Say "[6/7] Saving a snapshot (commit)..."
git -c user.useConfigOnly=false commit -m "Agent Brain 1.0.0 - cross-agent memory for AI coding assistants" | Out-Null
if ($LASTEXITCODE -ne 0) {
  Warn "Commit did not run. If git asked for your name/email, set them once:"
  Write-Host '     git config --global user.name  "Your Name"'
  Write-Host '     git config --global user.email "you@example.com"'
  Die "Then run this script again."
}
Good "committed"
git tag -a v1.0.0 -m "1.0.0" 2>$null
git branch -M main
Good "tagged v1.0.0 on branch main"

# --- 7. push ----------------------------------------------------------------
Say "[7/7] Publishing"
Write-Host ""
Write-Host "  Before continuing, create an EMPTY repository on GitHub:" -ForegroundColor White
Write-Host "     https://github.com/new"
Write-Host "     Name: agent-brain"
Write-Host "     Do NOT tick 'Add a README', 'Add .gitignore' or 'Choose a license'"
Write-Host "     (you already have all three - ticking them causes a conflict)"
Write-Host ""
$go = Read-Host "  Created it? Type YES to upload, or anything else to stop here"
if ($go -ne "YES") {
  Write-Host ""
  Good "Stopped. Everything is saved locally. To upload later, run this script again."
  Finish 0
}

$url = "https://github.com/$user/agent-brain.git"
git remote remove origin 2>$null
git remote add origin $url
Say "  pushing to $url"
git push -u origin main --tags
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Warn "The upload failed. The usual reasons:"
  Write-Host "     - the repository name on GitHub is not exactly 'agent-brain'"
  Write-Host "     - you were not signed in (a browser window may have opened)"
  Write-Host "     - the repo was created WITH a README, so it already has content"
  Write-Host ""
  Write-Host "  Your work is safe locally. Fix the above and run this script again."
  Finish 1
}

Write-Host ""
Write-Host "  DONE. Your project is live at:" -ForegroundColor Green
Write-Host "     https://github.com/$user/agent-brain" -ForegroundColor White
Write-Host ""
Write-Host "  Worth doing next:"
Write-Host "     - add a screenshot at docs\screenshot-home.png, then commit again"
Write-Host "     - check the Actions tab: the tests run automatically on 3 operating systems"
Write-Host ""
Finish 0
