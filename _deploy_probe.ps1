$ErrorActionPreference = 'Continue'
"== 进程 =="
Get-Process | Where-Object { $_.ProcessName -match 'VPNTray|mihomo' } | Select-Object Id, ProcessName, Path | Format-Table -AutoSize | Out-String
"== 模块占用 =="
$prefix = 'E:\VPNTray-2.5.7\'
Get-Process | ForEach-Object {
  $p = $_
  try {
    foreach ($m in $p.Modules) {
      if ($m.FileName -and $m.FileName.StartsWith($prefix, 'OrdinalIgnoreCase')) {
        "$($p.Id) $($p.ProcessName) -> $($m.FileName)"
        break
      }
    }
  } catch {}
}
"== 目录属性 =="
(Get-Item 'E:\VPNTray-2.5.7\_internal').Attributes
