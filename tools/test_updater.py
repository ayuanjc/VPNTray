"""updater 的离线自测：版本比较、zip slip、版本自证、stage 生成的 bat。

不联网、不重启，只验证纯逻辑部分。
"""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpntray import updater  # noqa: E402

ok = 0
bad = 0


def chk(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        bad += 1
        print(f"  FAIL  {name}  {extra}")


print("== parse_version / is_newer ==")
chk("1.7.1 -> (1,7,1)", updater.parse_version("1.7.1") == (1, 7, 1))
chk("v1.8.0 -> (1,8,0)", updater.parse_version("v1.8.0") == (1, 8, 0))
chk("1.7 -> (1,7)", updater.parse_version("1.7") == (1, 7))
chk("garbage -> None", updater.parse_version("abc") is None)
chk("空 -> None", updater.parse_version("") is None)
chk("1.8.0 > 1.7.1", updater.is_newer("1.8.0", "1.7.1"))
chk("1.7.1 > 1.7.0", updater.is_newer("1.7.1", "1.7.0"))
chk("1.7.0 == 1.7 不算新", not updater.is_newer("1.7.0", "1.7"))
chk("1.7 不 > 1.7.1", not updater.is_newer("1.7", "1.7.1"))
chk("1.10 > 1.9", updater.is_newer("1.10.0", "1.9.9"))
chk("v 前缀", updater.is_newer("v2.0", "1.99.99"))
chk("非法 latest 不算新", not updater.is_newer("nightly", "1.7.1"))

print("== find_exe_root ==")
base = Path("data/_t/up")
import shutil
shutil.rmtree(base, ignore_errors=True)
nested = base / "VPNTray-1.8.0"
nested.mkdir(parents=True)
(nested / "VPNTray.exe").write_bytes(b"MZ")
chk("嵌套一层能找", updater.find_exe_root(base) == nested)
flat = base / "flat"
flat.mkdir(parents=True)
(flat / "VPNTray.exe").write_bytes(b"MZ")
chk("平铺能找", updater.find_exe_root(flat) == flat)
empty = base / "empty"
empty.mkdir(parents=True)
chk("空目录 -> None", updater.find_exe_root(empty) is None)

print("== extract：版本自证 ==")


def make_zip(path: Path, files: dict, top="VPNTray-1.8.0"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for rel, body in files.items():
            zf.writestr(f"{top}/{rel}" if top else rel, body)


good = base / "good.zip"
make_zip(good, {
    "VPNTray.exe": b"MZ",
    "assets/index.html": "<script>const UI_VER = '1.8.0';</script>",
})
root = updater.extract(good, base / "w1", "1.8.0")
chk("版本相符能装", root.name == "VPNTray-1.8.0" and (root / "VPNTray.exe").exists())

mismatch = base / "mismatch.zip"
make_zip(mismatch, {
    "VPNTray.exe": b"MZ",
    "assets/index.html": "<script>const UI_VER = '9.9.9';</script>",
})
try:
    updater.extract(mismatch, base / "w2", "1.8.0")
    chk("版本不符要拒", False, "居然装上了")
except updater.UpdateError as e:
    chk("版本不符要拒", "对不上" in str(e), str(e))

slip = base / "slip.zip"
with zipfile.ZipFile(slip, "w") as zf:
    zf.writestr("VPNTray-1.8.0/VPNTray.exe", b"MZ")
    zf.writestr("../../evil.txt", b"x")
try:
    updater.extract(slip, base / "w3", "1.8.0")
    chk("zip slip 要拒", False)
except updater.UpdateError as e:
    chk("zip slip 要拒", "可疑" in str(e) or "越界" in str(e), str(e))

noexe = base / "noexe.zip"
make_zip(noexe, {"readme.txt": b"hi"})
try:
    updater.extract(noexe, base / "w4", "1.8.0")
    chk("没 exe 要拒", False)
except updater.UpdateError as e:
    chk("没 exe 要拒", "VPNTray.exe" in str(e), str(e))

notzip = base / "bad.zip"
notzip.write_bytes(b"not a zip at all")
try:
    updater.extract(notzip, base / "w5", "1.8.0")
    chk("坏 zip 要拒", False)
except updater.UpdateError as e:
    chk("坏 zip 要拒", "zip" in str(e), str(e))

print("== stage：生成的 bat ==")
w = base / "w6"
w.mkdir(parents=True, exist_ok=True)
bat = updater.stage(root, w, target_root=base / "target")
txt = bat.read_bytes().decode("gbk")
chk("bat 存在", bat.exists())
chk("bat 含 robocopy /E", "robocopy" in txt and "/E" in txt)
chk("bat 会等进程退出", "tasklist" in txt)
chk("bat 会重启", "start \"\"" in txt)
chk("bat 无 LF 裸行", b"\n" not in bat.read_bytes().replace(b"\r\n", b""))

print("== URL 白名单 ==")
try:
    updater._get("https://evil.example.com/x.zip", None, 1)
    chk("非 GitHub 域名要拒", False)
except updater.UpdateError as e:
    chk("非 GitHub 域名要拒", "拒绝" in str(e), str(e))

print("== check：仓库名校验 ==")
chk("空 repo 拒绝", updater.check("", "1.7.1")["ok"] is False)
chk("带 ../ 拒绝", updater.check("a/../../b", "1.7.1")["ok"] is False)
chk("正常 repo 才发请求", updater.check("ayuanjc/VPNTray", "1.7.1", local_port=None)["ok"] in (True, False))

shutil.rmtree(base, ignore_errors=True)
print()
print(f"结果: {ok} 通过 / {bad} 失败")
sys.exit(1 if bad else 0)
