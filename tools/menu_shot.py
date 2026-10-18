"""验证原生托盘菜单真的能弹出：给托盘窗口发右键消息 → 截图 → 关闭菜单。

用法: python tools/menu_shot.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import win32con  # noqa: E402
import win32gui  # noqa: E402
from PIL import ImageGrab  # noqa: E402

WM_TRAY = win32con.WM_APP + 1
CLASS_NAME = "VPNTrayNativeWnd"


def find_window() -> int:
    found = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLASS_NAME:
                found.append(h)
        except Exception:  # noqa: BLE001
            pass

    win32gui.EnumWindows(cb, None)
    return found[0] if found else 0


def main() -> int:
    hwnd = find_window()
    if not hwnd:
        print("没找到托盘窗口，程序没在运行？")
        return 1
    print("托盘窗口 hwnd =", hwnd)

    # 把鼠标挪到屏幕中间，菜单会弹在光标处
    try:
        ctypes.windll.user32.SetCursorPos(700, 400)
    except Exception:  # noqa: BLE001
        pass

    win32gui.PostMessage(hwnd, WM_TRAY, 0, win32con.WM_RBUTTONUP)
    time.sleep(1.0)

    tag = ""
    if "--sub" in sys.argv:
        # 下移到「切换节点」并展开子菜单
        for _ in range(1):
            _key(0x28)  # VK_DOWN
        time.sleep(0.25)
        _key(0x27)      # VK_RIGHT
        time.sleep(0.6)
        tag = "_sub"
    elif "--region" in sys.argv:
        _key(0x28); time.sleep(0.2)
        _key(0x27); time.sleep(0.4)   # 进入「切换节点」
        _key(0x27); time.sleep(0.6)   # 展开第一个地区
        tag = "_region"

    out = ROOT / "data" / f"menu_shot{tag}.png"
    img = ImageGrab.grab(all_screens=True)
    img.save(out)
    print("截图:", out, img.size, "bytes:", out.stat().st_size)

    _key(0x1B)  # ESC
    time.sleep(0.4)
    return 0


def _key(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.04)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


if __name__ == "__main__":
    raise SystemExit(main())
