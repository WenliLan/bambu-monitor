# bumbo_stats release build script (ASCII-only for Windows PowerShell 5.1)
# Usage: powershell -File build_release.ps1 [-Version 0.5.12]
# Steps: clean-tree check -> git archive export -> secret scan -> wheel verify -> cleanup -> zip -> git tag
param(
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"
$Repo = $PSScriptRoot
$Parent = Split-Path $Repo -Parent
$OutDir = Join-Path $Parent "bumbo_stats_release"
$RelName = Split-Path $OutDir -Leaf

# 0. version from pyproject.toml if not given
if (-not $Version) {
    $Version = (Select-String -Path (Join-Path $Repo "pyproject.toml") -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value
}
Write-Host "== Building v$Version ==" -ForegroundColor Cyan

# 1. working tree must be clean (release content = HEAD)
$dirty = (git -C $Repo status --porcelain | Measure-Object).Count
if ($dirty -gt 0) { Write-Host "Working tree has $dirty uncommitted change(s); commit first." -ForegroundColor Red; exit 1 }

# 2. export HEAD via file (NOT pipe: PowerShell text pipe corrupts binary tar)
if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
$tar = Join-Path $Repo "_release.tar"
git -C $Repo archive --format=tar --prefix="$RelName/" -o $tar HEAD
tar -x -f $tar -C $Parent
Remove-Item $tar

# 2b. inject release notes (template in repo, version stamped at build time)
if (Test-Path (Join-Path $Repo "RELEASE_NOTES.md")) {
    (Get-Content (Join-Path $Repo "RELEASE_NOTES.md") -Raw -Encoding UTF8) -replace '\{VERSION\}', $Version |
        Set-Content (Join-Path $OutDir "RELEASE_NOTES.txt") -Encoding UTF8
}

# 2c. photo collection template dirs (kept out of git; users add own photos to keep optimizing)
$photoRoot = Join-Path $OutDir "bambu_ai_deploy_dinoknn\data"
foreach ($d in @("normal", "anomaly", "uncertain")) {
    $sub = Join-Path $photoRoot $d
    New-Item -ItemType Directory -Force -Path $sub | Out-Null
    "# place $d photos (.jpg/.png) here" | Set-Content (Join-Path $sub "PLACE_PHOTOS_HERE.txt") -Encoding ASCII
}
$readmeLines = @(
  'Photo collection folders (used to keep improving detection on YOUR machine):',
  '  normal\    put photos of good prints here',
  '  anomaly\   put photos of failed prints (stringing, collapse, clog...) here',
  '  uncertain\ pending manual review, not auto-added',
  'Two ways to use them:',
  '  1. hot-update (recommended): pip install watchdog; python scripts/monitor.py',
  '     -> watcher auto-extracts features and adds normal\anomaly to gallery, no restart',
  '  2. batch rebuild: python scripts/init_gallery.py  (rebuilds gallery from these folders)',
  'Note: rebuild replaces the shipped gallery; to keep both, run bootstrap.py --src data instead.'
)
Set-Content (Join-Path $photoRoot "README.txt") -Value $readmeLines -Encoding ASCII

# 3. secret scan (fail on hit)
Write-Host "== Secret scan ==" -ForegroundColor Cyan
$pat = 'wrdz' + '|' + 'access_code:\s*["' + "'" + '][0-9a-zA-Z]{6}' + '|' + 'secret:\s*["' + "'" + '][0-9a-zA-Z]{8}' + '|' + 'token:\s*["' + "'" + '][0-9a-zA-Z]{20}' + '|' + '192\.168\.31\.'
$hits = Get-ChildItem $OutDir -Recurse -File | Select-String -Pattern $pat
if ($hits) { $hits | Select-Object -First 5; Write-Host "SENSITIVE CONTENT FOUND, abort release" -ForegroundColor Red; exit 1 }
Write-Host "PASS (0 hits)"

# 4. wheel build verification inside the exported dir (proves recipients can install)
Write-Host "== Wheel verify ==" -ForegroundColor Cyan
Push-Location $OutDir
python -m pip wheel --no-deps --no-build-isolation -w dist . | Out-Null
$whl = Get-ChildItem dist\*.whl -ErrorAction SilentlyContinue | Select-Object -First 1
Pop-Location
if (-not $whl) { Write-Host "wheel build FAILED" -ForegroundColor Red; exit 1 }

# 5. remove build leftovers + zip
Get-ChildItem $OutDir -Recurse -Force -Directory |
    Where-Object { $_.Name -match "egg-info|^build$|^dist$|__pycache__" } |
    Remove-Item -Recurse -Force
$zip = Join-Path $Parent "bumbo_stats_release_v$Version.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $OutDir -DestinationPath $zip

# 6. git tag for traceability (check-then-create: git stderr would trip ErrorActionPreference)
$has_tag = (git -C $Repo tag -l "v$Version" | Measure-Object).Count
if ($has_tag -eq 0) { git -C $Repo tag "v$Version" }

$files = (Get-ChildItem $OutDir -Recurse -File | Measure-Object).Count
Write-Host "== DONE ==" -ForegroundColor Green
Write-Host "Dir:   $OutDir ($files files)"
Write-Host "Zip:   $zip"
Write-Host "Tag:   v$Version $(if ($has_tag) { '(already exists)' } else { '(created)' }) - push later: git push origin v$Version"
