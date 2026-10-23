# 打包成单文件 exe：输出到项目根目录，与 bin/ data/ 同级
#
# ⚠ 2026-10-23 实测记录：本机装着「游戏加加 GamePP」时，onefile 版一启动就崩。
#   事件日志 faulting module = GPP64.dll，code 0xC0000005；偶尔表现成 PyInstaller
#   弹窗 "Could not create temporary directory!"（bootloader 还没解包就被注入的 DLL 打死）。
#   同一份源码用 --onedir 打包（见 release.ps1）完全正常 —— 本机自用优先走 release.ps1，
#   或用 release.ps1 的产物；要跑 onefile 版得先彻底退出游戏加加。
#   TEMP/ACL/磁盘/Defender 均已排查排除，别再往这三个方向查。
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
