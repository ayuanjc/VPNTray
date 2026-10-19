# 打包成单文件 exe：输出到项目根目录，与 bin/ data/ 同级
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$icon = Join-Path $PSScriptRoot 'assets\app.ico'
if (-not (Test-Path $icon)) {
    python -X utf8 tools\make_icon.py
}
if (Test-Path 'VPNTray.exe') { Remove-Item 'VPNTray.exe' -Force }

python -m PyInstaller --noconfirm --clean --onefile --windowed `
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
    --distpath $PSScriptRoot `
    --workpath (Join-Path $PSScriptRoot 'build') `
    --specpath (Join-Path $PSScriptRoot 'build') `
    (Join-Path $PSScriptRoot 'run.pyw')

if ($LASTEXITCODE -ne 0 -or -not (Test-Path 'VPNTray.exe')) {
    Write-Host "构建失败" -ForegroundColor Red
    exit 1
}
$size = [math]::Round((Get-Item 'VPNTray.exe').Length / 1MB, 1)
Write-Host "OK: VPNTray.exe ($size MB)" -ForegroundColor Green
