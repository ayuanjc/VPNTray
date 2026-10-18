# 一键发布到 GitHub Releases
#
# 用法：
#   1) 第一次：先把仓库建好并推上去
#        $env:GITHUB_TOKEN = "ghp_xxx"          # 需要 repo 权限的 PAT
#        .\publish.ps1 -Init
#   2) 以后每次发版
#        $env:GITHUB_TOKEN = "ghp_xxx"
#        .\publish.ps1
#
# 参数：
#   -Version 1.8.0     指定版本（默认读 vpntray/__init__.py 里的 __version__）
#   -Repo owner/name   默认 ayuanjc/VPNTray
#   -NoBuild           跳过打包，直接发布已有的 release/*.zip
#   -Draft             发成草稿（不自动对用户生效）
#   -WhatIf            只打印将要做什么，不动任何东西
#
# 需要：git、Python（build.ps1 用）、GITHUB_TOKEN 环境变量

[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$Repo = "ayuanjc/VPNTray",
    [string]$Token = $env:GITHUB_TOKEN,
    [switch]$Init,
    [switch]$NoBuild,
    [switch]$Draft,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "  OK  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  !!  $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "`n失败：$msg" -ForegroundColor Red; exit 1 }

# ────────────────────────── 0. 前置检查 ──────────────────────────
Step 0 "前置检查"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "找不到 git" }

if (-not $Version) {
    $initPy = "vpntray\__init__.py"
    if (-not (Test-Path $initPy)) { Die "找不到 $initPy" }
    $m = Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"'
    if (-not $m) { Die "解析不出 __version__" }
    $Version = $m.Matches[0].Groups[1].Value
}
$tag = "v$Version"
Ok "版本 $Version（tag $tag）"

# 版本三处必须一致，否则前端会自己刷新成对不上
$checks = @{
    "vpntray\__init__.py"  = '__version__\s*=\s*"' + [regex]::Escape($Version) + '"'
    "vpntray\webui.py"     = 'UI_VER\s*=\s*"' + [regex]::Escape($Version) + '"'
    "assets\index.html"    = "const UI_VER = '" + [regex]::Escape($Version) + "';"
}
foreach ($f in $checks.Keys) {
    if (-not (Select-String -Path $f -Pattern $checks[$f] -Quiet)) {
        Die "$f 里的版本号不是 $Version（三处必须一致：__init__.py / webui.py / assets/index.html）"
    }
}
Ok "三处版本号一致"

if (-not $Token) {
    Warn "没有 GITHUB_TOKEN：只能本地提交和打 tag，推不上去"
    Warn "去 https://github.com/settings/tokens 建一个带 repo 权限的 PAT，然后："
    Warn '  $env:GITHUB_TOKEN = "ghp_xxx"'
}

# ────────────────────────── 1. 仓库初始化 ──────────────────────────
Step 1 "Git 仓库"

if (-not (Test-Path ".git")) {
    if (-not $Init) {
        Die "当前目录不是 git 仓库。先跑 .\publish.ps1 -Init"
    }
    if ($WhatIf) {
        Ok "(WhatIf) 会执行 git init"
    } else {
        git init -q
        git branch -M main
        Ok "已 git init（分支 main）"
    }
} else {
    $top = (git rev-parse --show-toplevel) -replace '/', '\'
    if ($top.TrimEnd('\') -ne $PSScriptRoot.TrimEnd('\')) {
        Warn "这个目录不是独立的 git 仓库，它属于上层仓库：$top"
        Warn "直接提交会把上层仓库的无关文件一起带上。建议在本目录 git init。"
        if (-not $Init) { Die "确认要在本目录建独立仓库，就加 -Init 重跑" }
    } else {
        Ok "git 仓库正常（$top）"
    }
}

# remote
$remote = (git remote get-url origin 2>$null)
if (-not $remote) {
    if ($WhatIf) {
        Ok "(WhatIf) 会设置 origin -> https://github.com/$Repo.git"
    } else {
        git remote add origin "https://github.com/$Repo.git"
        Ok "origin = https://github.com/$Repo.git"
    }
} else {
    Ok "origin = $remote"
}

# ────────────────────────── 2. 打包 ──────────────────────────
Step 2 "打包"

$zip = "release\VPNTray-$Version-win64.zip"
if ($NoBuild) {
    if (-not (Test-Path $zip)) { Die "-NoBuild 但 $zip 不存在" }
    Ok "跳过打包，使用已有 $zip"
} elseif ($WhatIf) {
    Ok "(WhatIf) 会执行 build.ps1 + release.ps1"
} else {
    # exe 被占用时 PyInstaller 会 PermissionDenied：先把正在跑的程序正常关掉
    $running = Get-Process -Name "VPNTray" -ErrorAction SilentlyContinue
    if ($running) {
        Warn "VPNTray 正在运行，先让它退出（会还原系统代理）"
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:7899/api/quit" -Method Post `
                -ContentType "application/json" -Body "{}" -TimeoutSec 5 | Out-Null
        } catch {
            try {
                Invoke-RestMethod -Uri "http://127.0.0.1:7892/api/quit" -Method Post `
                    -ContentType "application/json" -Body "{}" -TimeoutSec 5 | Out-Null
            } catch { Warn "关不掉，手动退出后再跑" }
        }
        Start-Sleep -Seconds 3
    }

    & powershell -ExecutionPolicy Bypass -File .\build.ps1
    if ($LASTEXITCODE -ne 0) { Die "build.ps1 失败" }
    & pwsh -NoProfile -File .\release.ps1
    if ($LASTEXITCODE -ne 0) { Die "release.ps1 失败" }
    if (-not (Test-Path $zip)) { Die "打包完却没有 $zip" }
    $mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Ok "$zip（$mb MB）"
}

# ────────────────────────── 3. 提交 ──────────────────────────
Step 3 "提交代码"

$status = git status --porcelain
if (-not $status) {
    Ok "工作区干净，没有要提交的改动"
} elseif ($WhatIf) {
    Ok "(WhatIf) 会提交 $((($status -split "`n").Count)) 个变更"
} else {
    git add -A
    git commit -q -m "chore: release $tag"
    Ok "已提交 $tag"
}

# ────────────────────────── 4. 打 tag ──────────────────────────
Step 4 "打 tag"

$exists = git tag --list $tag
if ($exists) {
    Warn "tag $tag 已存在，跳过（要重发就先 git tag -d $tag 再跑）"
} elseif ($WhatIf) {
    Ok "(WhatIf) 会 git tag -a $tag"
} else {
    git tag -a $tag -m "VPN Tray $Version"
    Ok "已打 tag $tag"
}

# ────────────────────────── 5. 推送 ──────────────────────────
Step 5 "推送"

if (-not $Token) {
    Warn "没有 GITHUB_TOKEN，跳过推送"
} elseif ($WhatIf) {
    Ok "(WhatIf) 会推送 main + $tag"
} else {
    # token 走 http.extraheader，不写进 .git/config，也不进 remote url
    $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$Token"))
    $hdr = "AUTHORIZATION: Basic $b64"
    git -c http.extraheader=$hdr push -u origin main
    if ($LASTEXITCODE -ne 0) { Die "推送 main 失败（token 权限不够，或仓库还没建）" }
    git -c http.extraheader=$hdr push origin $tag
    if ($LASTEXITCODE -ne 0) { Die "推送 tag 失败" }
    Ok "已推送 main 和 $tag"
}

# ────────────────────────── 6. 发布 Release ──────────────────────────
Step 6 "GitHub Release"

if (-not $Token) {
    Warn "没有 GITHUB_TOKEN，跳过（之后可手动在网页上建 Release 并上传 $zip）"
} elseif ($WhatIf) {
    Ok "(WhatIf) 会在 $Repo 建 Release $tag 并上传 $zip"
} else {
    $api = "https://api.github.com/repos/$Repo/releases"
    $h = @{
        Authorization = "Bearer $Token"
        Accept        = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
    }

    # 已经发过就复用，避免重复建
    $rel = $null
    try {
        $rel = Invoke-RestMethod -Uri "$api/tags/$tag" -Headers $h -Method Get -TimeoutSec 20
        Ok "Release $tag 已存在，复用（id=$($rel.id)）"
    } catch {
        $body = @{
            tag_name   = $tag
            name       = "VPN Tray $Version"
            body       = "版本 $Version。`n`n安装：下载 VPNTray-$Version-win64.zip，解压后双击 VPNTray.exe。`n已有旧版本的可以直接在控制台「关于」里点检查更新自动升级。"
            draft      = [bool]$Draft
            prerelease = $false
        } | ConvertTo-Json -Depth 4
        $rel = Invoke-RestMethod -Uri $api -Headers $h -Method Post -Body $body -ContentType "application/json" -TimeoutSec 30
        Ok "已创建 Release $tag（id=$($rel.id)）"
    }

    # 同名资产存在就删掉再传，否则 422
    $existing = Invoke-RestMethod -Uri "$($rel.upload_url -replace '\{.*$','')" -Headers $h -Method Get -TimeoutSec 20
    foreach ($a in $existing) {
        if ($a.name -eq (Split-Path $zip -Leaf)) {
            Invoke-RestMethod -Uri $a.url -Headers $h -Method Delete -TimeoutSec 20 | Out-Null
            Warn "删掉同名旧资产 $($a.name)"
        }
    }

    Write-Host "  上传 $zip …" -NoNewline
    $upUrl = ($rel.upload_url -replace '\{.*$','') + "?name=" + [uri]::EscapeDataString((Split-Path $zip -Leaf))
    $upH = @{
        Authorization = "Bearer $Token"
        Accept        = "application/vnd.github+json"
        "Content-Type" = "application/zip"
    }
    $asset = Invoke-RestMethod -Uri $upUrl -Headers $upH -Method Post -InFile $zip -TimeoutSec 900
    Write-Host ""
    Ok "已上传 $($asset.name)（$([math]::Round($asset.size/1MB,1)) MB）"

    if ($rel.html_url) {
        Write-Host "`n发布页：$($rel.html_url)" -ForegroundColor Cyan
    }
}

Write-Host "`n完成。" -ForegroundColor Green
if (-not $Token) {
    Write-Host "下一步：设置 GITHUB_TOKEN 后重跑，或在网页上手动建 Release。" -ForegroundColor Yellow
}
