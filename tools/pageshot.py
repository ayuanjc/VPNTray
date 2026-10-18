"""用真实 Edge 窗口打开页面并截图（headless 的虚拟时钟会让轮询数据来不及渲染）。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import ImageGrab

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python tools/pageshot.py URL OUT.png [等待秒数]")
        return 2
    url = sys.argv[1]
    out = Path(sys.argv[2])
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 9.0

    prof = tempfile.mkdtemp(prefix="edge_shot_")
    p = subprocess.Popen(
        [EDGE, f"--app={url}", "--window-size=1500,1160", "--window-position=40,20",
         f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
         "--disable-features=Translate,msEdgeSplitScreen"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(wait)
    img = ImageGrab.grab(all_screens=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print("saved", out, img.size)
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
