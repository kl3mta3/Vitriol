# Transmute

A Windows-first desktop file converter for text, audio, video, image, and 3D model formats. Built around a self-contained, offline-first philosophy: only PySide6, Pillow, and striprtf as Python dependencies, plus FFmpeg (subprocess) and Assimp (ctypes) as native binaries. Every other format handler is recoded from stdlib.

## Architecture

The end user runs **`launcher.exe`** — a small PyInstaller bundle (~30 MB) with its own embedded Python. The launcher is **fully autonomous**: it does not prompt the user about anything. On every launch it ensures the following are present and silently downloads anything missing:

1. **Python packages** `main.py` needs (PySide6, Pillow, striprtf). Installed from `./wheels/` first, falling back to PyPI.
2. **FFmpeg** — `bin/ffmpeg.exe` + `bin/ffprobe.exe`, fetched from gyan.dev's official Windows builds.
3. **Assimp** — `bin/assimp-vc143-mt.dll`, fetched from the Assimp GitHub release.
4. **DejaVuSans.ttf** — `resources/DejaVuSans.ttf`, fetched from the DejaVu GitHub release. Required for full Unicode coverage in PDF output (Latin Extended + Cyrillic + Greek).
5. **Hardware encoder probe** — runs `ffmpeg -encoders`, caches results to `bin/hw_encoders.json`. The audio/video handler uses NVENC/QSV/AMF/VideoToolbox silently when the source/target codec combination has a hardware path; falls back to software otherwise. No UI toggle.

After everything is in place the launcher hands off to `main.py` via subprocess using its embedded interpreter — so the user never needs Python installed system-wide. `main.py` and `app/` live as plain `.py` files alongside the launcher and can be edited freely without rebuilding the launcher executable.

**This separation means**: editing handlers / UI does NOT require rebuilding `launcher.exe`. Only changes to the launch-time dependency check do.

**Trust model**: downloads come from official upstream HTTPS endpoints. SHA-256 verification is supported (set `FFMPEG_SHA` / `ASSIMP_SHA` / `DEJAVU_SHA` constants in `launcher.py` to lock a release) but is OFF by default so the launcher is never blocked when upstream re-releases. If you ship a zip with `bin/` and `resources/` pre-populated, the launcher skips the downloads and uses what's already there.

## Supported formats

| Category | Read | Write |
|---|---|---|
| **Plain text** | txt, md, html, json, xml, yaml, ini, log | txt, md, html, json, xml, yaml, ini, log |
| **Tabular** | csv, tsv, xlsx | csv, tsv, xlsx |
| **Documents** | docx, pdf, epub, rtf, pptx, odt | docx, pdf, epub |
| **Images** | png, jpg, webp, bmp, tiff, gif, ico, tga, ppm/pgm/pbm, dds, heic*, svg | png, jpg, webp, bmp, tiff, gif, ico, tga, ppm/pgm/pbm, dds, heic* |
| **Audio** | mp3, wav, flac, ogg, opus, m4a, aac, wma, aiff, alac, ac3, amr, au, mka | (same) |
| **Video** | mp4, mkv, webm, avi, mov, wmv, flv, mpg, 3gp, ts, vob, ogv | (same) |
| **3D models** | glb, gltf, obj, stl, fbx, ply, dae, 3ds | (same) |

\* HEIC requires the optional `pillow-heif` package. SVG is read-only.

## Top-bar toggles

**Philosopher's Stone** (off by default; persists across sessions). When on, the per-row target dropdown expands to include byte-passthrough hosts: `.wav`, `.png`, `.bmp`, `.txt`, `.mkv`. Embedding hides any source file losslessly inside the host's data section behind a self-defining envelope (`UCMSv1` magic + original-extension + payload bytes). The reverse direction recovers the original file byte-exact.

Lossy source formats (`.jpg`, `.webp`, `.heic`, `.mp3`, `.ogg`, `.opus`, `.m4a`, `.aac`, `.wma`, `.ac3`, `.amr`, `.mp4`, `.webm`, `.mov`, `.wmv`, `.flv`, `.mpg`, `.3gp`, `.ts`, `.vob`, `.ogv`, `.avi`) are excluded from Stone routing. Their bytes can technically round-trip through a host container, but the original media data inside them is already a lossy compression — treating them as "preserved" is conceptually wrong, and the dropdown asymmetry (jpg→txt allowed, txt→jpg not) confused users. Only lossless data goes through the Stone.

| Host | Container | Notes |
|---|---|---|
| `.wav` | RIFF/WAVE PCM, envelope is the data chunk | Plays as static audio |
| `.png` | 1×1 RGBA + private `ucMs` ancillary chunk | Displays as 1px transparent |
| `.bmp` | 1×1 24-bit + payload appended after pixel array | Displays as 1px |
| `.txt` | Base64-wrapped envelope with `#` header | Always opens cleanly in any text editor |
| `.mkv` | Matroska + rawvideo rgb24, 1024×1024 @ **42 fps** | Plays as static video. Requires FFmpeg. **42 fps is intentional** — it fingerprints Stone output: combined with the `UCMSv1` magic in frame-zero pixels, anyone can identify "this is a Transmute Stone file" by reading the container header alone. Minimum 42 frames so the clip is always at least 1.0 second |

For MKV, frame data carries the envelope (hybrid approach) AND the MKV metadata tags duplicate the byte count, real frame count, and padding frame count for inspection in mkvinfo. Round-trip reads the envelope from frame pixels — surviving any tool that preserves the rawvideo stream. The metadata tags are advisory only.

**Verify Round-Trip** (greyed unless Philosopher's Stone is on). After each conversion, immediately runs the reverse direction into a temp folder, hashes both files, and only commits the output if `sha256(reverse(forward(src))) == sha256(src)`. The temp folder is cleaned up on success, on verification failure, and on app exit. Distinguishes "verification could not complete" (I/O error during compare — retry safe) from "verification failed — bytes differ" (genuine round-trip failure — the host doesn't preserve this content).

## Bundle output for Markdown

When converting a rich format with images (docx → md, pdf → md, epub → md, pptx → md), the markdown writer detects embedded images and switches to a folder-structured output:

```
desired_output_location/
   my_document/
       my_document.md
       images/
           image1.png
           image2.jpg
```

The `.md` references images via `![alt](images/image1.png)` (relative paths). Pure-text conversions still produce a flat `.md`. The reverse direction (md → docx) consumes this structure if present, embedding images back into the docx.

## Run from source

```
python launcher.py
```

That's the whole setup. The launcher:

- Installs PySide6, Pillow, and striprtf into the current Python via pip if they're missing.
- Downloads FFmpeg (~80 MB), Assimp (~10 MB), and DejaVuSans.ttf to `./bin/` and `./resources/` if they're missing.
- Probes hardware encoders.
- Spawns `main.py`.

No manual steps. The first launch fetches everything from official upstream sources over HTTPS; subsequent launches skip the downloads.

For development you can run `python main.py` directly to skip the launcher's checks (deps must already be installed).

## Project layout

```
launcher.py                   PyInstaller-bundled wrapper (deps + hw probe + spawn main.py)
main.py                       Entry point invoked by the launcher
theme.qss                     Dark theme
app/ui/                       PySide6 widgets
app/core/                     Conversion queue, router, IRs, file detector, downloader
app/format_handlers/          One module per format family (incl. masquerade)
app/format_handlers/_pdf_ttf.py  Minimal TTF parser for Type0 PDF font embedding
app/utils/                    Paths, logger, cancellation, settings
bin/                          FFmpeg + Assimp DLL + hw_encoders.json (gitignored)
wheels/                       Optional offline pip wheels (gitignored)
resources/                    Bundled fonts (DejaVuSans.ttf for PDF Unicode)
output/                       Default output directories per category
```

## v1 scope cuts

- **PPTX, ODT**: read-only.
- **RTF**: read via `striprtf` (preferred) or a from-scratch fallback. Write deferred.
- **SVG**: read-only via QtSvg's SVG Tiny 1.2 implementation.
- **PDF write**: Type0 with Identity-H + DejaVu Sans gives full Latin Extended + Cyrillic + Greek when the font is bundled. Without DejaVu, falls back to standard 14 fonts (Helvetica family, WinAnsi only). CJK is intentionally out of v1 — bundling Noto CJK adds ~20 MB and a separate font-fallback layer.
- **PDF read**: handles uncompressed and FlateDecode streams. Encrypted PDFs and image-only PDFs (OCR) are not supported. When extraction yields under 50 chars from a PDF over 100 KB, surfaces a status-bar warning explaining likely causes.
- **3D animations**: dropped on conversion (geometry only).
- **Linux/macOS, light mode, codec/quality presets, CLI mode, batch rename**: deferred.

## Why no third-party format libraries?

Dependency rule: if a library can be reasonably reimplemented in a few hundred to a few thousand lines, recode it. Only depend on libraries where reimplementation would take days or weeks (FFmpeg, Assimp). The trade-off is a heavier app that always works offline rather than a light app that needs internet to install 200 packages.
