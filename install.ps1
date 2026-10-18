# 一键安装：桌面快捷方式 + （可选）开始菜单项
# 用法: 右键「使用 PowerShell 运行」，或在终端执行 pwsh -File install.ps1
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$exe = Join-Path $root 'VPNTray.exe'
$target = $exe
$args_ = ''
if (-not (Test-Path $exe)) {
    # 没有打包 exe 时，退回到 pythonw 启动
    $pyw = Join-Path (Split-Path (Get-Command python).Source) 'pythonw.exe'
    if (-not (Test-Path $pyw)) { throw "找不到 pythonw.exe，请先用 build.ps1 打包，或确认 Python 安装" }
    $target = $pyw
    $args_ = '"' + (Join-Path $root 'run.pyw') + '"'
}

$shell = New-Object -ComObject WScript.Shell

function New-Shortcut([string]$path, [string]$desc) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $args_
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = (Join-Path $root 'assets\app.ico')
    $lnk.Description = $desc
    $lnk.Save()
    Write-Host "创建: $path"
}

New-Shortcut ([Environment]::GetFolderPath('Desktop') + '\VPNTray.lnk') 'VPN Tray — 常驻代理与节点切换'
New-Shortcut ([Environment]::GetFolderPath('Programs') + '\VPNTray.lnk') 'VPN Tray'

Write-Host ''
Write-Host '安装完成。双击桌面 VPNTray 启动，图标出现在右下角托盘。' -ForegroundColor Green
Write-Host '首次启动会自动：拉订阅 → 写配置 → 启动内核 → 接管系统代理 → 自动挑一个可用节点。'
Write-Host '开机自启：托盘菜单里勾选「开机自动启动」。'
