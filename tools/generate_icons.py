"""Render the Vitriol logo SVGs into PNG sizes + multi-resolution .ico.

Run once whenever any of the source SVGs changes:
    python tools/generate_icons.py

Inputs:
    resources/logo.svg          (default — discs use #0a0a0f fill)
    resources/logo-bg.svg       (background-baked variant)
    resources/logo-outline.svg  (transparent / stroke-only variant)

Outputs (in resources/icons/):
    logo-{16,32,48,64,128,256,512}.png        + logo.ico
    logo-bg-{16,32,48,64,128,256,512}.png     + logo-bg.ico
    logo-outline-{16,32,48,64,128,256,512}.png

Use logo-bg.* for the Windows window icon and any taskbar surface (it sits
on its own dark background regardless of where it's composited). Use
logo-outline.* when you want to drop the logo on top of a custom background
of your own choice — its discs are transparent.

Uses QtSvg to rasterize (handles gradients and SVG features Pillow can't
read on its own) and Pillow to assemble the .ico container.
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


def _render_one_variant(svg: Path, slug: str, out_dir: Path,
                         emit_ico: bool) -> None:
    rendered: dict[int, Image.Image] = {}
    for size in sorted(set(PNG_SIZES + ICO_SIZES)):
        img = render_png(svg, size)
        rendered[size] = img
        if size in PNG_SIZES:
            png_path = out_dir / f"{slug}-{size}.png"
            img.save(png_path, format="PNG", optimize=True)
            print(f"  wrote {png_path.name} ({png_path.stat().st_size} bytes)")
    if emit_ico:
        base = rendered[max(ICO_SIZES)]
        extra = [rendered[s] for s in ICO_SIZES if s != max(ICO_SIZES)]
        ico_path = out_dir / f"{slug}.ico"
        base.save(
            ico_path,
            format="ICO",
            sizes=[(s, s) for s in ICO_SIZES],
            append_images=extra,
        )
        print(f"  wrote {ico_path.name} ({ico_path.stat().st_size} bytes, {len(ICO_SIZES)} frames)")


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    out_dir = repo / "resources" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)

    # (svg filename, output slug, emit .ico). All three emit .ico now;
    # transparent backgrounds render correctly in Windows ICOs (the OS honors
    # the alpha channel for taskbar/explorer composites).
    variants = [
        ("logo.svg",         "logo",         True),
        ("logo-bg.svg",      "logo-bg",      True),
        ("logo-outline.svg", "logo-outline", True),
    ]

    for svg_name, slug, emit_ico in variants:
        svg = repo / "resources" / svg_name
        if not svg.exists():
            print(f"  skipping {svg_name} (not found)")
            continue
        print(f"--- {svg_name} -> {slug}-* ---")
        _render_one_variant(svg, slug, out_dir, emit_ico)

    return 0


if __name__ == "__main__":
    sys.exit(main())
