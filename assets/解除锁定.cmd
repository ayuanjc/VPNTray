@echo off
chcp 936 >nul
title 解除 VPNTray 的「来自 Internet」标记
echo.
echo 这个脚本只做一件事：把本文件夹里所有文件的「来自 Internet」标记去掉。
echo 不去掉的话，Windows 每次运行都会弹「Windows 已保护你的电脑」。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File | Unblock-File -ErrorAction SilentlyContinue; Write-Host '完成'"
echo.
echo 现在可以双击 VPNTray.exe 了。
echo.
pause
