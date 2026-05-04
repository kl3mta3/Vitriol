"""Render resources/logo.svg into PNG sizes + a multi-resolution Windows .ico.

Run once whenever logo.svg changes:
    python tools/generate_icons.py

Outputs:
    resources/icons/logo-16.png
    resources/icons/logo-32.png
    resources/icons/logo-48.png
    resources/icons/logo-64.png
    resources/icons/logo-128.png
    resources/icons/logo-256.png
    resources/icons/logo-512.png
    resources/icons/logo.ico    (embeds 16/32/48/64/128/256 frames)

Uses QtSvg to rasterize (handles gradients and SVG features Pillow can't read
on its own) and Pillow to assemble the .ico container.
"""
from __future__ import annotations
import sys
from io import BytesIO
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication
from PIL import Image


PNG_SIZES = [16, 32, 48, 64, 128, 256, 512]
ICO_SIZES = [16, 32, 48, 64, 128, 256]


def render_png(svg_path: Path, size: int) -> Image.Image:
    """Rasterize the SVG to a PIL Image at `size`×`size`. Pillow doesn't read
    SVG natively, so we go QtSvg → QImage → bytes → PIL."""
    renderer = QSvgRenderer(str(svg_path))
    qimg = QImage(size, size, QImage.Format.Format_RGBA8888)
    qimg.fill(Qt.GlobalColor.transparent)
    p = QPainter(qimg)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    # QImage to PIL Image
    ptr = qimg.constBits().tobytes()
    return Image.frombuffer("RGBA", (size, size), ptr, "raw", "RGBA", 0, 1)


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    svg = repo / "resources" / "logo.svg"
    if not svg.exists():
        print(f"ERROR: {svg} not found", file=sys.stderr)
        return 1

    out_dir = repo / "resources" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)

    # QApplication needed for QtSvg rendering even in offscreen mode
    app = QApplication.instance() or QApplication(sys.argv)

    rendered: dict[int, Image.Image] = {}
    for size in sorted(set(PNG_SIZES + ICO_SIZES)):
        img = render_png(svg, size)
        rendered[size] = img
        if size in PNG_SIZES:
            png_path = out_dir / f"logo-{size}.png"
            img.save(png_path, format="PNG", optimize=True)
            print(f"  wrote {png_path.name} ({png_path.stat().st_size} bytes)")

    # Multi-frame ICO
    base = rendered[max(ICO_SIZES)]
    extra = [rendered[s] for s in ICO_SIZES if s != max(ICO_SIZES)]
    ico_path = out_dir / "logo.ico"
    base.save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=extra,
    )
    print(f"  wrote {ico_path.name} ({ico_path.stat().st_size} bytes, {len(ICO_SIZES)} frames)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
