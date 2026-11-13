#!/usr/bin/env bash
set -u
export MSYS2_ARG_CONV_EXCL='*'
SRC="E:/Desktop/AI/intert/vpntray/release/VPNTray-3.0.1"
DST="E:/VPNTray-2.5.7"
cd "$DST" || exit 1

curl -s -m 4 -X POST http://127.0.0.1:7892/api/quit >/dev/null 2>&1
sleep 3
"/c/Program Files/PowerShell/7/pwsh.exe" -NoProfile -Command "Get-Process VPNTray,mihomo -ErrorAction SilentlyContinue | Stop-Process -Force" >/dev/null 2>&1
sleep 2
echo "剩余进程: $(tasklist | grep -ci vpntray || true)"

[ -f VPNTray.exe ] && cp -f VPNTray.exe VPNTray.exe.bak-3.0.0
rm -rf "$DST/_internal"; echo "rm exit=$?"
cp -r "$SRC/_internal" "$DST/_internal" || exit 1
cp -f "$SRC/VPNTray.exe" "$DST/VPNTray.exe"
cp -f "$SRC/README.md" "$DST/README.md" 2>/dev/null
cp -f "$SRC/首次使用.txt" "$DST/首次使用.txt" 2>/dev/null
[ -d "$SRC/bin" ] && cp -rf "$SRC/bin/." "$DST/bin/"
echo "src=$(ls "$SRC" | wc -l) dst=$(ls "$DST" | wc -l) internal_src=$(ls "$SRC/_internal" | wc -l) internal_dst=$(ls "$DST/_internal" | wc -l)"
ls -l "$DST/VPNTray.exe" "$DST/_internal/assets/index.html"
