from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


PROJECT_DIR = Path(__file__).resolve().parent
ASSET_DIR = PROJECT_DIR / "app" / "assets"


def build_icon() -> None:
    svg_path = ASSET_DIR / "log_review_icon.svg"
    png_path = ASSET_DIR / "canqt_icon_256.png"
    ico_path = ASSET_DIR / "canqt.ico"
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise RuntimeError(f"无法读取图标：{svg_path}")
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, 256, 256))
    painter.end()
    if not image.save(str(png_path), "PNG"):
        raise RuntimeError("无法生成 PNG 图标。")
    with Image.open(png_path) as source:
        source.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main() -> int:
    build_icon()
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(PROJECT_DIR / "CANQT.spec"),
    ]
    return subprocess.call(command, cwd=PROJECT_DIR)


if __name__ == "__main__":
    raise SystemExit(main())
