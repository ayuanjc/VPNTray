# 打发布包：生成可分发给别人的 zip（onedir 版，启动快、误报少）
# 用法: pwsh -File release.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# 版本号只认一个源：vpntray\webui.py 的 UI_VER。
# 以前这里写死 1.0.0，界面早就 1.7 了压缩包还叫 1.0.0，发出去对不上。
# 支持两段（1.7）和三段（1.7.1）；两段时自动补 .0。
$m = Select-String -Path (Join-Path $PSScriptRoot 'vpntray\webui.py') `
                   -Pattern '^UI_VER\s*=\s*"(\d+\.\d+(?:\.\d+)?)"' | Select-Object -First 1
if (-not $m) { throw '读不到 vpntray\webui.py 里的 UI_VER' }
$uiVer = $m.Matches[0].Groups[1].Value
$ver = if ($uiVer.Split('.').Count -ge 3) { $uiVer } else { "$uiVer.0" }
Write-Host "版本: $ver" -ForegroundColor DarkGray
$stage = Join-Path $PSScriptRoot "release\VPNTray-$ver"
$zip = Join-Path $PSScriptRoot "release\VPNTray-$ver-win64.zip"

Write-Host "[1/5] 生成图标" -ForegroundColor Cyan
$icon = Join-Path $PSScriptRoot 'assets\app.ico'
if (-not (Test-Path $icon)) { python -X utf8 tools\make_icon.py }

Write-Host "[2/5] PyInstaller onedir 打包" -ForegroundColor Cyan
$raw = Join-Path $PSScriptRoot 'release\VPNTray'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
if (Test-Path $raw) { Remove-Item $raw -Recurse -Force }
python -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name VPNTray `
    --icon $icon `
    --add-data "$PSScriptRoot\assets;assets" `
    --hidden-import pystray._win32 `
    --hidden-import PIL._imaging `
    --hidden-import webview.platforms.winforms `
    --hidden-import clr_loader `
    --hidden-import pythonnet `
    --collect-data webview `
    --exclude-module tkinter `
    --distpath (Join-Path $PSScriptRoot 'release') `
    --workpath (Join-Path $PSScriptRoot 'build') `
    --specpath (Join-Path $PSScriptRoot 'build') `
    (Join-Path $PSScriptRoot 'run.pyw')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }
Move-Item $raw $stage -Force

Write-Host "[3/5] 放入内核与地理数据" -ForegroundColor Cyan
$binDst = Join-Path $stage 'bin'
New-Item -ItemType Directory -Force -Path $binDst | Out-Null
foreach ($f in 'mihomo.exe', 'GeoIP.dat', 'GeoSite.dat', 'country.mmdb') {
    Copy-Item (Join-Path $PSScriptRoot "bin\$f") $binDst -Force
    $mb = [math]::Round((Get-Item (Join-Path $binDst $f)).Length / 1MB, 1)
    Write-Host ("    {0}  {1} MB" -f $f, $mb)
}
# NTFS 大小写不敏感，GeoIP.dat / geoip.dat 视为同一文件，无需额外复制

Write-Host "[4/5] 放入说明与 data 目录" -ForegroundColor Cyan
Copy-Item (Join-Path $PSScriptRoot '首次使用.txt') $stage -Force
Copy-Item (Join-Path $PSScriptRoot 'README.md') $stage -Force
$dataDst = Join-Path $stage 'data'
New-Item -ItemType Directory -Force -Path $dataDst | Out-Null
Get-ChildItem $dataDst -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
# 只带上纯净度/IP/城市缓存，别人的订阅和设置一律不带
$puritySrc = Join-Path $PSScriptRoot 'data\purity.json'
if (Test-Path $puritySrc) {
    Copy-Item $puritySrc $dataDst -Force
    $n = 0
    try {
        $j = Get-Content $puritySrc -Raw -Encoding UTF8 | ConvertFrom-Json
        $n = ($j.PSObject.Properties | Measure-Object).Count
    } catch { $n = 0 }
    Write-Host ("    purity.json  预置 {0} 个节点的 IP/城市/纯净度" -f $n)
} else {
    Write-Host "    (没有 purity.json，跳过)" -ForegroundColor Yellow
}

Write-Host "[5/5] 压缩" -ForegroundColor Cyan
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $stage -DestinationPath $zip -CompressionLevel Optimal
$zipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
$dirMb = [math]::Round(((Get-ChildItem $stage -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)

Write-Host ""
Write-Host "完成" -ForegroundColor Green
Write-Host "  目录: $stage  ($dirMb MB)"
Write-Host "  压缩包: $zip  ($zipMb MB)"
Write-Host ""
Write-Host "发这个 zip 给朋友，解压后双击 VPNTray.exe 即可。"
