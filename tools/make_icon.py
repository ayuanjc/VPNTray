"""生成多尺寸 .ico（绿/灰/黄/红四态），供 exe / 托盘 / 快捷方式使用。

用法: python tools/make_icon.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from vpntray import icons  # noqa: E402

SIZES = [256, 128, 64, 48, 32, 16]
FILES = {"on": "app.ico", "off": "app_off.ico", "busy": "app_busy.ico", "error": "app_err.ico"}


def main() -> None:
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    for state, fname in FILES.items():
        imgs = [icons.make_icon(state, s).resize((s, s), Image.LANCZOS) for s in SIZES]
        path = out / fname
        imgs[0].save(path, format="ICO", sizes=[(s, s) for s in SIZES])
        print(f"生成: {path.name}  {path.stat().st_size} bytes")

    icons.make_icon("on", 256).save(out / "app.png")
    print("生成: app.png")


if __name__ == "__main__":
    main()
