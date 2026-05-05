# Transmute

<p align="center">
  <img src="resources/icons/logo-256.png" alt="Transmute" width="160"/>
</p>

<p align="center">
  <em>A self-contained, offline-first desktop file converter for text, audio, video, image, and 3D model formats.</em>
</p>

---

## What it does

Transmute converts files between formats across five categories:

- **Text** — txt, md, html, json, xml, yaml, ini, log, csv, tsv, xlsx, docx, pdf, epub, rtf, pptx, odt
- **Images** — png, jpg, webp, bmp, tiff, gif, ico, tga, ppm/pgm/pbm, dds, heic, svg
- **Audio** — mp3, wav, flac, ogg, opus, m4a, aac, wma, aiff, alac, ac3, amr, au, mka
- **Video** — mp4, mkv, webm, avi, mov, wmv, flv, mpg, 3gp, ts, vob, ogv
- **3D models** — glb, gltf, obj, stl, fbx, ply, dae, 3ds

Plus a **Philosopher's Stone** mode that hides any file losslessly inside a host container (WAV / PNG / BMP / TXT / MKV) and recovers it byte-exact, with an optional **Verify Round-Trip** check that hashes both directions and only commits the output if they match.

## Install & run

```
python launcher.py
```

That's the entire setup. The launcher is autonomous — on first launch it:

1. Installs the three required Python packages (`PySide6`, `Pillow`, `striprtf`) via pip.
2. Downloads FFmpeg, Assimp, DejaVu Sans, and Cinzel Regular from official upstream sources to `bin/` and `resources/`.
3. Probes FFmpeg for hardware encoders (NVENC / QuickSync / AMF / VideoToolbox) and caches the result.
4. Hands off to `main.py`.

Subsequent launches skip the downloads. End users never need Python installed system-wide once the launcher is packaged with PyInstaller.

## Philosophy

Only **PySide6**, **Pillow**, and **striprtf** as Python dependencies. **FFmpeg** as a subprocess and **Assimp** via `ctypes` as the only native binaries. Everything else — docx / xlsx / pptx / epub / pdf / markdown / charset detection / 3D bindings / dependency management — is recoded from the standard library.

The trade-off is a heavier app that always works offline, rather than a light app that needs internet to install 200 packages.

## Top-bar toggles

**Philosopher's Stone** (off by default; persists across sessions). When on, the per-row target dropdown expands to include byte-passthrough hosts: `.wav`, `.png`, `.bmp`, `.txt`, `.mkv`, `.py`, `.ply`, `.obj`, `.glb`, `.aiff`, `.flac`. Lossy source formats (jpg, mp3, mp4, etc.) are excluded — only lossless data passes through the Stone.

| Host | Container | Notes |
|---|---|---|
| `.wav` | RIFF/WAVE PCM, envelope is the data chunk | Plays as static audio (same-category) or as music (cross-category — see Aesthetic Stone below) |
| `.png` | Pixel-data envelope (UCMSv2) | Solid-color image (same-category) or Mandelbrot fractal (cross-category) |
| `.bmp` | 24-bit pixel-data envelope | Same dual mode as PNG |
| `.txt` | Base64-wrapped envelope, no header | Looks like an ordinary base64 dump |
| `.mkv` | Matroska + rawvideo rgb24, 1024×1024 @ 42 fps | Plays as static video. Requires FFmpeg |
| `.py` | Self-extracting Python script | `python file.py` reconstructs the original |
| `.ply` / `.obj` | ASCII 3D-model header with envelope in `comment` lines | Loads in MeshLab / Blender / Open3D as a degenerate single-vertex mesh |
| `.glb` | Binary glTF with custom `ucMs` chunk after JSON+BIN | Loads in any glTF viewer; the chunk is spec-compliant (readers must ignore unknown chunks) |
| `.aiff` | Apple/IFF audio (FORM/COMM/SSND, big-endian PCM) | Same dual mode as WAV |
| `.flac` | Lossless FLAC via FFmpeg | Cross-category outputs are music-mode WAV re-encoded to FLAC |

### Aesthetic Stone (cross-category outputs)

When Philosopher's Stone is on AND the conversion crosses categories (e.g. `.pdf → .png`, `.txt → .wav`), the output gets aesthetic treatment:

- **Image targets** (`.png`, `.bmp`) render as **deterministic colored Mandelbrot fractals**. The source bytes hide in the bottom bit of each pixel byte (1 bit per channel = 3 bits per pixel). The top 7 bits per channel hold a real fractal at 128 levels per channel — perceptually identical to a "pure" fractal rendering. Output passes statistical "is this a real fractal?" tests: pixel compression ratio drops to ~0.03 (real fractals are 0.05–0.3, random noise is ~1.0). Each source picks one of 65 hand-curated viewports across the Mandelbrot boundary and one of 6 color-cycling palette algorithms (sin-wave, HSV cycle, two-color gradient, three-anchor blend, log ramp, inverted) — same source always produces the same image, different sources produce visibly distinct fractals.
- **Audio targets** (`.wav`, `.aiff`, `.flac`) get **music-like sample data** — generated chord progressions in a key/tempo/progression deterministically chosen from the envelope header — with the source bytes packed into the bottom 4 bits of each 16-bit sample. Output is ~1.5–2× source size for compressible inputs, ~3–4× for already-compressed inputs.

Both features preserve byte-perfect round-trip. Same-category Stone (e.g. `.png → .png`, `.mp3 → .wav`) is unchanged.

### Stone encryption (per-row password)

Every Stone PNG/BMP is **encrypted with AES-256-CTR**. With no user password, files use a built-in app-wide key — anyone with Transmute can decode them (same exposure level as the older non-encrypted Stone). With a user password set on a row (via the 🔓/🔒 lock icon next to the save-path field), only the same password decodes the file.

The round-trip is purely mechanical: `WAV1 + "paper" → PNG1`, then `PNG1 + "paper" → WAV2` produces `WAV2 == WAV1` byte-for-byte. **Wrong password produces garbage output silently** — there is no "incorrect password" message, and Transmute does not detect that encryption was used. The format itself is the only key. Every Stone file looks structurally identical to every other Stone file; the bytes don't reveal which use a password and which don't.

**Forgetting a password means the file is unrecoverable.** Transmute does not store, log, or persist passwords. They live in widget memory only and are forgotten when the row is removed or Stone mode is toggled off.

**Verify Round-Trip** (only available with Philosopher's Stone on). After each conversion, immediately runs the reverse direction into a temp folder, hashes both files, and only commits the output if `sha256(reverse(forward(src))) == sha256(src)`. The temp folder is cleaned up on success, on verification failure, and on app exit.

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

## Project layout

```
launcher.py                    # PyInstaller-bundled wrapper (deps + hw probe + spawn main.py)
main.py                        # Entry point invoked by the launcher
theme.qss                      # Dark theme
tools/
    generate_icons.py          # Render logo.svg to PNG sizes + multi-resolution .ico
app/
    ui/                        # PySide6 widgets (main window, playlist, drop zone,
                               #     border frame, vignette overlay, status glyph)
    core/                      # Conversion queue, router, IRs, file detector, downloader
    format_handlers/           # One module per format family (incl. masquerade engine)
    utils/                     # Paths, logger, cancellation, settings
resources/
    logo.svg                   # App logo (transmutation circle)
    gem.svg                    # Toggle gem icon
    hex-empty.svg              # Topbar toggle indicator (off)
    hex-filled-red.svg         # Stone toggle indicator (on)
    hex-filled-purple.svg      # Verify toggle indicator (on)
    icons/                     # Generated PNG sizes + logo.ico (run tools/generate_icons.py)
    fonts/                     # Auto-fetched Cinzel-Regular.ttf
    DejaVuSans.ttf             # Auto-fetched (PDF Unicode)
bin/                           # FFmpeg + Assimp DLL + hw_encoders.json (gitignored)
wheels/                        # Optional offline pip wheels (gitignored)
output/                        # Default output directories per category
```

## Customization

- **Theme** — edit `theme.qss`. The `{RES}` placeholder is substituted with the absolute path to `resources/` at load time, so QSS rules can reference SVG assets via `image: url({RES}/...)`.
- **Hardware encoders** — automatic. The result of `ffmpeg -encoders` is cached at `bin/hw_encoders.json` and refreshed whenever the FFmpeg binary's mtime changes. No UI toggle.
- **Settings** — persisted at `%LOCALAPPDATA%/Transmute/settings.json` on Windows. Currently tracks the two top-bar toggles.
- **Logo** — edit `resources/logo.svg`, then re-run `python tools/generate_icons.py` to regenerate the PNG sizes and `.ico`.

## Scope notes

- PPTX and ODT are read-only.
- RTF reads via [striprtf](https://github.com/joshy/striprtf); RTF write is not supported.
- SVG is read-only via QtSvg's SVG Tiny 1.2 implementation.
- PDF write supports headings, paragraphs, lists, basic tables, and image placeholders. With DejaVu Sans bundled, the writer emits CID-keyed Type0 fonts with full Latin Extended + Cyrillic + Greek coverage.
- PDF read parses uncompressed and FlateDecode streams. Encrypted PDFs and image-only PDFs (which would need OCR) are not supported.
- 3D conversions preserve geometry only — animations are dropped.

## License

MIT — see [LICENSE](LICENSE).

### Third-party components

- [PySide6](https://doc.qt.io/qtforpython-6/) — LGPL v3
- [Pillow](https://pillow.readthedocs.io/) — MIT-CMU
- [striprtf](https://github.com/joshy/striprtf) — BSD 3-Clause
- [FFmpeg](https://www.ffmpeg.org/) (auto-fetched) — LGPL v2.1+ for the gyan.dev essentials build; see the FFmpeg README for codec licenses
- [Assimp](https://www.assimp.org/) (auto-fetched) — BSD 3-Clause
- [DejaVu Sans](https://dejavu-fonts.github.io/) (auto-fetched) — Bitstream Vera + DejaVu Public Domain
- [Cinzel](https://fonts.google.com/specimen/Cinzel) (auto-fetched) — SIL Open Font License 1.1

The launcher fetches these from their official upstream sources over HTTPS on first run. SHA-256 verification is supported per release; see the constants at the top of `launcher.py` to lock specific versions before shipping.
