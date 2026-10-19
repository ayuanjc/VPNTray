"""验证 MOTW 清理 + spawn 验活。

2.0.0 的两个坑：
  1) 带「来自 Internet」标记的程序集 .NET 拒绝加载 -> 窗口秒退
  2) spawn() 只看 Popen 成不成功 -> 秒退也当成打开成功，于是既不回退浏览器也不报错
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from vpntray import ui_native as un  # noqa: E402

print("=== 1. MOTW 检测与清理 ===")
d = pathlib.Path(tempfile.mkdtemp())
f = d / "Python.Runtime.dll"
f.write_bytes(b"MZ fake")
try:
    with open(f"{f}:Zone.Identifier", "w", encoding="utf-8") as fh:
        fh.write("[ZoneTransfer]\r\nZoneId=3\r\n")
except OSError as e:
    print("  写 Zone.Identifier 失败（当前文件系统不支持 ADS）:", e)
else:
    print("  造好标记后 has_motw =", un.has_motw(f))
    n = un.unblock_tree(d)
    print("  unblock_tree 清了", n, "个 | 之后 has_motw =", un.has_motw(f))

print("\n=== 2. 程序目录现有 MOTW ===")
root = un._app_root()
cnt = 0
for p in root.rglob("*"):
    if p.is_file() and un.has_motw(p):
        cnt += 1
print(f"  {root} 下还有 {cnt} 个带标记的文件")

print("\n=== 3. spawn 验活（正常路径）===")
good, why = un.spawn(7892, wait=un.SPAWN_WAIT)
print(f"  spawn -> {good} {why}")
print(f"  窗口 hwnd = {un._find_console_window()}")
if good:
    print("  focus() ->", un.focus())
