"""Shared bundle output helper.

When a TextDoc carries Image blocks with bytes, plain-text-ish writers
(markdown, txt, html) emit a folder-structured output instead of a flat
file. The folder layout is:

    <dst.parent>/<dst.stem>/
        <dst.stem>.<ext>
        images/
            image1.<ext>
            image2.<ext>
            ...

The flat .<ext> file references images via a relative path
("images/imageN.png"), and the writer mutates each Image block's `href`
attribute so the renderer emits the correct relative path.

If the doc has NO images, the writer's flat path is used unchanged
(no folder created — output is just a single file).
"""
from __future__ import annotations
from pathlib import Path
from typing import Callable, List

from .intermediate import (
    Block, Blockquote, Image, List_, Paragraph, Table, TextDoc,
)


_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
}


def _ext_for_mime(mime: str) -> str:
    return _MIME_TO_EXT.get((mime or "").lower(), ".png")


def _collect_images(doc: TextDoc) -> List[Image]:
    """Walk the doc tree and return every Image block that has actual bytes."""
    out: List[Image] = []

    def visit(blocks):
        for b in blocks:
            if isinstance(b, Image) and b.data:
                out.append(b)
            elif isinstance(b, List_):
                for item in b.items:
                    visit(item)
            elif isinstance(b, Blockquote):
                visit(b.blocks)
            elif isinstance(b, Table):
                for row in b.rows:
                    for cell in row:
                        visit(cell)
    visit(doc.blocks)
    return out


def _unique_bundle_dir(target: Path) -> Path:
    """Like unique_path but for directories — append ' (1)', ' (2)', ... if
    the target dir already exists."""
    if not target.exists():
        return target
    parent = target.parent
    stem = target.name
    i = 1
    while True:
        candidate = parent / f"{stem} ({i})"
        if not candidate.exists():
            return candidate
        i += 1


def write_with_bundle(
    doc: TextDoc,
    dst: Path,
    flat_render: Callable[[TextDoc, Path], None],
    image_href_format: str = "images/{name}",
) -> None:
    """Decide flat vs bundle layout based on whether `doc` carries images.

    Args:
        doc: TextDoc to write. Image blocks' `href` attributes will be set
            in-place to point at the bundled image files.
        dst: The "logical" output path the user picked (e.g.
            output/Text/photo.md). For bundle output, the actual file lands
            at <dst.parent>/<dst.stem>/<dst.name>.
        flat_render: Callback `(doc, path) -> None` that writes the doc to
            `path` in the target format. Receives the bundled path when
            bundling, the original `dst` when not.
        image_href_format: Template for the href written into each Image
            block. Default is "images/{name}" — a path relative to the .md
            (or whatever) file inside the bundle.
    """
    images = _collect_images(doc)
    if not images:
        flat_render(doc, dst)
        return

    bundle_dir = _unique_bundle_dir(dst.parent / dst.stem)
    img_dir = bundle_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    for n, img in enumerate(images, 1):
        fname = f"image{n}{_ext_for_mime(img.mime)}"
        (img_dir / fname).write_bytes(img.data)
        # Mutate the href so the renderer writes the relative reference.
        img.href = image_href_format.format(name=fname)

    flat_render(doc, bundle_dir / dst.name)
