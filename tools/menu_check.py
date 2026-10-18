"""托盘菜单结构自检：不弹窗，直接把动态菜单展开一遍，检查文本/勾选状态是否正常。

用法: python tools/menu_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from vpntray import logger  # noqa: E402
from vpntray.app import App  # noqa: E402
from vpntray.tray import Tray  # noqa: E402


def resolve(item, icon) -> str:
    t = item.text
    return t(icon) if callable(t) else str(t)


def walk(menu, icon, depth: int = 0, seen: set | None = None) -> int:
    seen = seen or set()
    n = 0
    pad = "  " * depth
    for item in menu:
        if item is None:
            continue
        if str(item) == "<Menu.SEPARATOR>":
            print(f"{pad}───")
            continue
        try:
            text = resolve(item, icon)
        except Exception as e:  # noqa: BLE001
            print(f"{pad}! 文本求值失败: {e}")
            continue
        checked = ""
        if callable(item.checked) or item.checked:
            try:
                checked = " [x]" if (item.checked(icon) if callable(item.checked) else item.checked) else " [ ]"
            except Exception:  # noqa: BLE001
                checked = " [?]"
        enabled = "" if (item.enabled(icon) if callable(item.enabled) else item.enabled) else " (disabled)"
        print(f"{pad}{text}{checked}{enabled}")
        n += 1
        if item.submenu is not None and depth < 3 and id(item.submenu) not in seen:
            seen.add(id(item.submenu))
            n += walk(item.submenu, icon, depth + 1, seen)
            seen.discard(id(item.submenu))
    return n


def main() -> int:
    logger.setup()
    app = App()
    tray = Tray(app)
    print("── 顶层菜单 ──")
    total = walk(tray._build_menu(), tray.icon)
    print(f"\n共 {total} 项")

    print("\n── 节点子菜单（展开前 2 个地区）──")
    count = 0
    for item in tray._node_items():
        try:
            print(" ", resolve(item, tray.icon))
        except Exception as e:  # noqa: BLE001
            print("  !", e)
        count += 1
        if count >= 6:
            break
    print(f"节点菜单顶层 {count} 项（已截断）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
