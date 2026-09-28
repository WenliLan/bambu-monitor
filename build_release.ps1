# bumbo_stats 发布包构建脚本
# 用法: powershell -File build_release.ps1 [-Version 0.5.12]
# 流程: 校验干净工作区 -> git archive 导出 -> 敏感扫描 -> wheel 构建验证 -> 清理残留 -> 打 zip -> 打 git tag
param(
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"
$Repo = $PSScriptRoot
$OutDir = Join-Path (Split-Path $Repo -Parent) "bumbo_stats_release"
$RelName = Split-Path $OutDir -Leaf

# 0. 版本号：未指定则读 pyproject.toml
if (-not $Version) {
    $Version = (Select-String -Path (Join-Path $Repo "pyproject.toml") -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value
}
Write-Host "== 构建 v$Version ==" -ForegroundColor Cyan

# 1. 前置校验：工作区必须干净（发布内容 = HEAD，未提交的改动不会进包）
$dirty = (git -C $Repo status --porcelain | Measure-Object).Count
if ($dirty -gt 0) { Write-Host "工作区有 $dirty 项未提交，先 commit 再打包" -ForegroundColor Red; exit 1 }

# 2. 导出 HEAD（git archive 走文件而非管道，避免 PowerShell 文本管道破坏二进制）
if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
$tar = Join-Path $Repo "_release.tar"
git -C $Repo archive --format=tar --prefix="$RelName/" -o $tar HEAD
tar -x -f $tar -C (Split-Path $OutDir -Parent)
Remove-Item $tar

# 3. 敏感扫描：命中即失败退出
Write-Host "== 敏感信息扫描 ==" -ForegroundColor Cyan
$hits = Get-ChildItem $OutDir -Recurse -File |
    Select-String -Pattern "wrdz|access_code: .[0-9a-zA-Z]{6,}|secret: .[0-9a-zA-Z]{8,}|token: [0-9a-zA-Z]{20,}|192\.168\.31\."
if ($hits) { $hits | Select-Object -First 5; Write-Host "发现敏感内容，中止发布" -ForegroundColor Red; exit 1 }
Write-Host "通过（0 命中）"

# 4. wheel 构建验证（在发布目录内独立构建，证明拿到它的人装得上）
Write-Host "== wheel 构建验证 ==" -ForegroundColor Cyan
Push-Location $OutDir
python -m pip wheel --no-deps --no-build-isolation -w dist . | Out-Null
$whl = Get-ChildItem dist\*.whl | Select-Object -First 1
if (-not $whl) { Pop-Location; Write-Host "wheel 构建失败" -ForegroundColor Red; exit 1 }
Pop-Location

# 5. 清理构建残留 + 打 zip
Get-ChildItem $OutDir -Recurse -Force -Directory |
    Where-Object { $_.Name -match "egg-info|^build$|^dist$|__pycache__" } |
    Remove-Item -Recurse -Force
$zip = Join-Path (Split-Path $OutDir -Parent) "bumbo_stats_release_v$Version.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $OutDir -DestinationPath $zip

# 6. 打 tag（追溯 zip 对应哪份代码；已存在则跳过）
git -C $Repo tag "v$Version" 2>$null

$files = (Get-ChildItem $OutDir -Recurse -File | Measure-Object).Count
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "目录: $OutDir ($files 个文件)"
Write-Host "压缩包: $zip"
Write-Host "tag: v$Version 已打上（git push 时记得 git push origin v$Version）"
