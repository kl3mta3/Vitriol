# Vitriol

<p align="center">
  <img src="resources/icons/logo-256.png" alt="Vitriol" width="160"/>
</p>

<p align="center">
  <em>A self-contained, offline-first desktop file converter for text, audio, video, image, and 3D model formats.</em>
</p>

---

## What it does

Vitriol converts files between formats across five categories:

- **Text** — txt, md, html, json, xml, yaml, ini, log, csv, tsv, xlsx, docx, pdf, epub, rtf, pptx, odt
- **Images** — png, jpg, webp, bmp, tiff, gif, ico, tga, ppm/pgm/pbm, dds, heic, svg
- **Audio** — mp3, wav, flac, ogg, opus, m4a, aac, wma, aiff, alac, ac3, amr, au, mka
- **Video** — mp4, mkv, webm, avi, mov, wmv, flv, mpg, 3gp, ts, vob, ogv
- **3D models** — glb, gltf, obj, stl, fbx, ply, dae, 3ds

Plus a **Philosopher's Stone** mode that hides any file losslessly inside a host container — image (PNG, BMP), audio (WAV, AIFF, FLAC, M4A), video (MKV), text (TXT), 3D model (PLY, OBJ, GLB), archive (ZIP), self-extracting Python script (.py), or self-extracting Windows executable (.exe) — and recovers it byte-exact. Includes an optional **Verify Round-Trip** check that hashes both directions and only commits the output if they match, and an optional per-row password that AES-256-CTR-encrypts the embedded payload.

## Install & run

```
python launcher.py
```

That's the entire setup. The launcher is autonomous — on first launch it:

1. Installs the four required Python packages (`PySide6`, `Pillow`, `striprtf`, `cryptography`) via pip.
2. Downloads FFmpeg, Assimp, DejaVu Sans, and Cinzel Regular from official upstream sources to `bin/` and `resources/`.
3. Probes FFmpeg for hardware encoders (NVENC / QuickSync / AMF / VideoToolbox) and caches the result.
4. Hands off to `main.py`.

Subsequent launches skip the downloads. End users never need Python installed system-wide once the launcher is packaged with PyInstaller.

## Philosophy

Only **PySide6**, **Pillow**, **striprtf**, and **cryptography** as Python dependencies. **FFmpeg** as a subprocess and **Assimp** via `ctypes` as the only native binaries. Everything else — docx / xlsx / pptx / epub / pdf / markdown / charset detection / 3D bindings / dependency management — is recoded from the standard library.

The trade-off is a heavier app that always works offline, rather than a light app that needs internet to install 200 packages.

## Top-bar toggles

**Philosopher's Stone** (off by default; persists across sessions). When on, the per-row target dropdown expands to include byte-passthrough hosts: `.wav`, `.png`, `.bmp`, `.txt`, `.mkv`, `.py`, `.exe`, `.zip`, `.ply`, `.obj`, `.glb`, `.aiff`, `.flac`, `.m4a`. Lossy source formats (jpg, mp3, mp4, etc.) are excluded — only lossless data passes through the Stone. `.zip` and `.exe` sources auto-engage Stone (they have no non-Stone meaning in Vitriol). `.py` ↔ `.py`, `.py` ↔ `.exe`, and `.exe` ↔ `.exe` are blocked by the malware-pipeline guard — Vitriol refuses to wrap auto-execute formats in other auto-execute formats.

| Host | Container | Notes |
|---|---|---|
| `.wav` | RIFF/WAVE PCM, envelope is the data chunk | Plays as static audio (same-category) or as music (cross-category — see Aesthetic Stone below) |
| `.png` | Pixel-data envelope (UCMSv2) | Solid-color image (same-category) or Mandelbrot fractal (cross-category) |
| `.bmp` | 24-bit pixel-data envelope | Same dual mode as PNG |
| `.txt` | Base64-wrapped envelope, no header | Looks like an ordinary base64 dump |
| `.mkv` | Matroska + rawvideo rgb24, 1024×1024 @ 42 fps | Plays as static video. Requires FFmpeg |
| `.py` | Self-extracting Python script | `python file.py` reconstructs the original. With a password set, the script prompts for it at run time, AES-CTR-decrypts, and self-deletes after either success or 5 wrong-password attempts. The actual rebuild logic + counter math is hidden inside an encrypted inner runtime; the visible loader is just bootstrap |
| `.exe` | Self-extracting Windows executable | Pre-compiled Python stub binary (~12 MB) with the same `.py` runtime appended after a `TMUTSTUB-PAYLOAD\x00` magic marker. End users don't need Python installed. Same password / counter / self-delete behavior as `.py`. Frozen-exe self-delete uses a deferred batch file to bypass Windows's exclusive-lock-on-running-exe |
| `.zip` | Transparent ZIP archive containing a single member named `original.<ext>` | Opens in any unzip tool; member preserves the original filename and bytes. Cheap detection: ZIP namelist scan |
| `.ply` / `.obj` | ASCII 3D-model header with envelope in `comment` lines | Loads in MeshLab / Blender / Open3D as a degenerate single-vertex mesh |
| `.glb` | Binary glTF with custom `ucMs` chunk after JSON+BIN | Loads in any glTF viewer; the chunk is spec-compliant (readers must ignore unknown chunks) |
| `.aiff` | Apple/IFF audio (FORM/COMM/SSND, big-endian PCM) | Same dual mode as WAV |
| `.flac` | Lossless FLAC via FFmpeg | Cross-category outputs are music-mode WAV re-encoded to FLAC |
| `.m4a` | ALAC (Apple Lossless) via FFmpeg | Cross-category only — same-category audio→m4a routes through the regular media pipeline |

### Aesthetic Stone (cross-category outputs)

When Philosopher's Stone is on AND the conversion crosses categories (e.g. `.pdf → .png`, `.txt → .wav`), the output gets aesthetic treatment:

- **Image targets** (`.png`, `.bmp`) render as **deterministic colored Mandelbrot fractals**. The source bytes hide in the bottom bit of each pixel byte (1 bit per channel = 3 bits per pixel). The top 7 bits per channel hold a real fractal at 128 levels per channel — perceptually identical to a "pure" fractal rendering. Output passes statistical "is this a real fractal?" tests: pixel compression ratio drops to ~0.03 (real fractals are 0.05–0.3, random noise is ~1.0). Each source picks one of 65 hand-curated viewports across the Mandelbrot boundary and one of 6 color-cycling palette algorithms (sin-wave, HSV cycle, two-color gradient, three-anchor blend, log ramp, inverted) — same source always produces the same image, different sources produce visibly distinct fractals.
- **Audio targets** (`.wav`, `.aiff`, `.flac`) get **music-like sample data** — generated chord progressions in a key/tempo/progression deterministically chosen from the envelope header — with the source bytes packed into the bottom 4 bits of each 16-bit sample. Output is ~1.5–2× source size for compressible inputs, ~3–4× for already-compressed inputs.

Both features preserve byte-perfect round-trip. Same-category Stone (e.g. `.png → .png`, `.mp3 → .wav`) is unchanged.

### Stone encryption (per-row password)

Every Stone v3 envelope is **encrypted with AES-256-CTR** under a key derived from PBKDF2-HMAC-SHA256 (200,000 iterations). The same envelope encrypts payloads inside PNG, BMP, MKV, music-mode WAV / AIFF / FLAC / M4A, the inner runtime of self-extracting `.py` and `.exe`, and 3D model hosts (PLY, OBJ, GLB) when used cross-category. With no user password, the key derives from an empty password — anyone with Vitriol can decode (same exposure level as the older non-encrypted Stone). With a user password set on a row (via the 🔓/🔒 lock icon next to the save-path field), only the same password decodes the file.

The round-trip is purely mechanical: `WAV1 + "paper" → PNG1`, then `PNG1 + "paper" → WAV2` produces `WAV2 == WAV1` byte-for-byte.

**Wrong-password behavior depends on host type.** This is intentional — different threat models for different hosts:

- **PNG / BMP / WAV / AIFF / FLAC / M4A / MKV / PLY / OBJ / GLB** (passive media envelopes) — wrong password produces **garbage output silently**. There is no "incorrect password" message, and Vitriol does not detect that encryption was used. The format itself is the only key. Every Stone file of these types looks structurally identical to every other Stone file; the bytes don't reveal which use a password and which don't. This preserves the **no-oracle invariant** — an attacker probing files cannot distinguish "right password produced wrong file" from "wrong password produced garbage."
- **`.py` / `.exe`** (self-extracting runtimes) — wrong password prints `Wrong password. X/5 attempts used.` and increments a per-file counter stored in `HKCU\Software\_872676883` (Windows) or `~/.872676883` (POSIX). The counter values themselves are HMAC-derived 32-bit magics, not raw 0–5, so a regedit user can't tell how many attempts have been used by reading the DWORD. After 5 wrong attempts, the file self-deletes and is invalidated. This trades the no-oracle property for a hard rate-limit appropriate to interactive runtimes.

**Forgetting a password means the file is unrecoverable.** Vitriol does not store, log, or persist passwords. They live in widget memory only and are forgotten when the row is removed or Stone mode is toggled off.

**Verify Round-Trip** (only available with Philosopher's Stone on). After each conversion, immediately runs the reverse direction into a temp folder, hashes both files, and only commits the output if `sha256(reverse(forward(src))) == sha256(src)`. The temp folder is cleaned up on success, on verification failure, and on app exit.

**Tamper detection on `.py` / `.exe`.** The encrypted self-extracting runtime checks its own integrity twice before writing the recovered file: a SHA-256 of the file's stable bytes (everything except the encrypted runtime block itself) is baked into the inner runtime at embed time and re-verified at extraction; and the decrypted payload is re-hashed and compared to the embedded payload hash before being written to disk. Any modification to the file fails one or both checks, and the recovered file is never written.

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
- **Settings** — persisted at `%LOCALAPPDATA%/Vitriol/settings.json` on Windows. Currently tracks the two top-bar toggles.
- **Logo** — edit `resources/logo.svg`, then re-run `python tools/generate_icons.py` to regenerate the PNG sizes and `.ico`.

## Scope notes

- PPTX and ODT are read-only.
- RTF reads via [striprtf](https://github.com/joshy/striprtf); RTF write is not supported.
- SVG is read-only via QtSvg's SVG Tiny 1.2 implementation.
- PDF write supports headings, paragraphs, lists, basic tables, and image placeholders. With DejaVu Sans bundled, the writer emits CID-keyed Type0 fonts with full Latin Extended + Cyrillic + Greek coverage.
- PDF read parses uncompressed and FlateDecode streams. Encrypted PDFs and image-only PDFs (which would need OCR) are not supported.
- 3D conversions preserve geometry only — animations are dropped.

## License

**Elastic License** — see [LICENSE](LICENSE).

The Elastic License is **source-available**, not OSI-approved open source. You may freely use, copy, distribute, and modify the source. You may **not**:

- Provide Vitriol to third parties as a hosted or managed service that exposes substantially the same features.
- Remove or obscure the licensor's licensing, copyright, or notice text.
- Move, change, disable, or circumvent any license-key functionality (when added in future versions).

Modifications must be marked as such. See LICENSE for the full text and definitions.

### Third-party components

- [PySide6](https://doc.qt.io/qtforpython-6/) — LGPL v3
- [Pillow](https://pillow.readthedocs.io/) — MIT-CMU
- [striprtf](https://github.com/joshy/striprtf) — BSD 3-Clause
- [cryptography](https://cryptography.io/) — Apache 2.0 / BSD 3-Clause
- [FFmpeg](https://www.ffmpeg.org/) (auto-fetched) — LGPL v2.1+ for the gyan.dev essentials build; see the FFmpeg README for codec licenses
- [Assimp](https://www.assimp.org/) (auto-fetched) — BSD 3-Clause
- [DejaVu Sans](https://dejavu-fonts.github.io/) (auto-fetched) — Bitstream Vera + DejaVu Public Domain
- [Cinzel](https://fonts.google.com/specimen/Cinzel) (auto-fetched) — SIL Open Font License 1.1

The launcher fetches these from their official upstream sources over HTTPS on first run. SHA-256 verification is supported per release; see the constants at the top of `launcher.py` to lock specific versions before shipping.
