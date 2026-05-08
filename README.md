# Vitriol


<p align="center">
<em>Visita Interiora Terrae Rectificando Invenies Occultum Lapidem
</p>

<p align="center">
  <img src="resources/icons/logo-256.png" alt="Vitriol" width="160"/>
</p>

<p align="center">
<em>Visit the interior of the earth; by rectification you will find the hidden stone.
</p>

---

<p align="center">
  <em>A self-contained, offline-first desktop file converter for text, audio, video, image, and 3D model formats.</em>
</p>

---

## What it does

Vitriol converts files between formats across five categories — **text, images, audio, video, and 3D models** — about 60 input formats and 50 output formats covered.

It also includes a feature called **Philosopher's Stone**: drop any lossless file in, get back an output that looks like an ordinary image, audio file, video, archive, or self-extracting script — but contains the original bytes inside, recoverable byte-exact through Vitriol. Optional password protection (AES-256). Optional Verify Round-Trip safety check that refuses to commit an output unless the reverse conversion produces the original.

### Format support

- **Text** — txt, md, html, json, xml, yaml, ini, log, csv, tsv, xlsx, docx, pdf, epub, rtf, pptx, odt
- **Images** — png, jpg, webp, bmp, tiff, gif, ico, tga, ppm/pgm/pbm, dds, heic, svg
- **Audio** — mp3, wav, flac, ogg, opus, m4a, aac, wma, aiff, alac, ac3, amr, au, mka
- **Video** — mp4, mkv, webm, avi, mov, wmv, flv, mpg, 3gp, ts, vob, ogv
- **3D models** — glb, gltf, obj, stl, fbx, ply, dae, 3ds

## Install & run

```
python launcher.py
```

That's the entire setup. On first launch the launcher installs four Python packages (`PySide6`, `Pillow`, `striprtf`, `cryptography`), downloads FFmpeg + Assimp + bundled fonts to local folders, probes for hardware video encoders, and hands off to the main app. Subsequent launches are instant.

When packaged as the installer (`VitriolSetup-x.y.z.exe` from `tools/build_installer.py`), end users don't need Python at all — everything is bundled.

## Philosophy

Vitriol is **offline-first**. Everything runs locally — no telemetry, no network calls during conversions, no third-party APIs. The launcher fetches FFmpeg and Assimp from their official sites once, then never touches the network again.

Minimal dependencies: four Python packages, two native binaries (FFmpeg, Assimp). Everything else — docx, xlsx, pptx, epub, pdf, markdown, charset detection — is implemented from the standard library. The tradeoff is a heavier app that always works offline rather than a small app that needs internet to install dependencies.

## Philosopher's Stone

Stone mode hides a source file inside one of several host formats. The output is a real, working file of its host type — opens in the appropriate viewer, plays in the appropriate player — and Vitriol can recover the original bytes from it byte-for-byte.

| Host format | What you get |
|---|---|
| `.png`, `.bmp` | An image. Same-category sources produce a passthrough image; cross-category sources produce a deterministic colored fractal. |
| `.wav`, `.aiff`, `.flac`, `.m4a` | An audio file. Same-category sources are normal audio; cross-category sources sound like generated music. |
| `.mkv` | A video file. Plays in any video player. Requires FFmpeg. |
| `.txt` | A text file with base64-style content. |
| `.py` | A self-extracting Python script. Run `python file.py` and it reconstructs the original. |
| `.exe` | A self-extracting Windows executable. End users don't need Python. |
| `.zip` | A standard ZIP archive containing the original file. Opens in any unzip tool. |
| `.ply`, `.obj`, `.glb` | A 3D model file. Opens in Blender, MeshLab, or any glTF viewer. |

Lossy formats (jpg, mp3, mp4, etc.) cannot be Stone sources — only lossless data round-trips meaningfully. `.zip` and `.exe` sources auto-engage Stone (they have no other purpose in Vitriol). Vitriol refuses `.py` ↔ `.py`, `.py` ↔ `.exe`, and `.exe` ↔ `.exe` conversions to prevent the tool from being used as a malware wrapper.

### Password protection

Each row in the playlist has an optional password (🔓 / 🔒 lock icon next to the save path). When set, the embedded payload is encrypted with **AES-256-CTR** under a key derived from your password via **PBKDF2-HMAC-SHA256**. Without the right password, the file does not decode.

Wrong-password behavior depends on the host type. Two distinct designs for two threat models:

- **Image, audio, video, 3D, text, zip outputs** — wrong password produces silent garbage. There is no error message; the file gives no signal that encryption was used at all. Same-format outputs look identical whether they're password-protected or not. This preserves a *no-oracle* property: someone probing files cannot tell "right password produced wrong file" from "wrong password produced garbage."
- **Self-extracting `.py` and `.exe`** — these are interactive, so they print `Wrong password. X/5 attempts used.` After 5 wrong attempts the file invalidates itself and self-deletes.

**Forgetting a password means the file is unrecoverable.** Vitriol never stores, logs, or persists passwords. They live in the playlist row's memory and are gone the moment you remove the row or close the app.

### Verify Round-Trip

Optional safety toggle in the top bar. With it on, every Stone conversion immediately runs in reverse into a temp folder, and the output is only committed if the reverse produces bytes matching the original input. If they don't match, the output is discarded and the row is marked failed. Catches end-to-end integrity issues before they reach disk.

### Samples

The [`samples/`](samples) folder ships with three source files and the Stone outputs Vitriol produces from them, so you can see/hear/run the round-trip without installing anything. Drop any output back into Vitriol to recover the original.

<p align="center">
  <a href="samples/Sample%20Music%20Outputs/Sample%20Music%20Zip.png"><img src="samples/Sample%20Music%20Outputs/Sample%20Music%20Zip.png" alt="Stone PNG hiding a zip of audio" width="180"/></a>
  &nbsp;&nbsp;
  <a href="samples/The%20Raven%20Outputs/The%20Raven%20By%20Edgar%20Allan%20Poe%20.png"><img src="samples/The%20Raven%20Outputs/The%20Raven%20By%20Edgar%20Allan%20Poe%20.png" alt="Stone PNG hiding The Raven PDF" width="180"/></a>
  &nbsp;&nbsp;
  <a href="samples/Chopin%20-%20Nocturne%20Outputs/Chopin%20-%20Nocturne%20Op.%209%20password%20free.png"><img src="samples/Chopin%20-%20Nocturne%20Outputs/Chopin%20-%20Nocturne%20Op.%209%20password%20free.png" alt="Stone PNG hiding a Chopin nocturne (no password)" width="180"/></a>
  &nbsp;&nbsp;
  <a href="samples/Chopin%20-%20Nocturne%20Outputs/Chopin%20-%20Nocturne%20Op.%209%20%28Password%20is%20Chopin%29.png"><img src="samples/Chopin%20-%20Nocturne%20Outputs/Chopin%20-%20Nocturne%20Op.%209%20%28Password%20is%20Chopin%29.png" alt="Stone PNG hiding a Chopin nocturne (password-protected)" width="180"/></a>
</p>
<p align="center">
  <sub><em>Four Stone-mode PNGs — a zip of audio, a PDF, and the same Chopin nocturne hidden twice (the third image is unencrypted, the fourth is encrypted with the password <code>Chopin</code>). Same source, different password, completely different image: the encryption is baked into the carrier itself. Click any image to view full size, then drop it into Vitriol to recover the original file.</em></sub>
</p>

| Source | Stone outputs |
|---|---|
| [`Sample Music Zip.zip`](samples/Sample%20Music%20Zip.zip) — a zip archive of audio files | [`.png`](samples/Sample%20Music%20Outputs/Sample%20Music%20Zip.png) · [`.wav`](samples/Sample%20Music%20Outputs/Sample%20Music%20Zip.wav) · [`.py`](samples/Sample%20Music%20Outputs/Sample%20Music%20Zip.py) |
| [`The Raven By Edgar Allan Poe.pdf`](samples/The%20Raven%20By%20Edgar%20Allan%20Poe%20.pdf) | [`.png`](samples/The%20Raven%20Outputs/The%20Raven%20By%20Edgar%20Allan%20Poe%20.png) · [`.wav`](samples/The%20Raven%20Outputs/The%20Raven%20By%20Edgar%20Allan%20Poe%20.wav) · [`.mkv`](samples/The%20Raven%20Outputs/The%20Raven%20By%20Edgar%20Allan%20Poe%20.mkv) · [`.py`](samples/The%20Raven%20Outputs/The%20Raven%20By%20Edgar%20Allan%20Poe%20.py) |
| [`Chopin - Nocturne Op. 9, No. 2.m4a`](samples/Chopin%20-%20Nocturne%20Op.%209%2C%20No.%202%20in%20E-flat%20major.m4a) — [🔊 Listen to source](samples/Chopin%20-%20Nocturne%20Op.%209%2C%20No.%202%20in%20E-flat%20major.m4a) | [`.png` (no password)](samples/Chopin%20-%20Nocturne%20Outputs/Chopin%20-%20Nocturne%20Op.%209%20password%20free.png) · [`.png` (password)](samples/Chopin%20-%20Nocturne%20Outputs/Chopin%20-%20Nocturne%20Op.%209%20%28Password%20is%20Chopin%29.png) · [`.py`](samples/Chopin%20-%20Nocturne%20Outputs/Chopin%20-%20Nocturne%20Op.%209%20%28Password%20is%20Chopin%29.py) · [`.exe`](samples/Chopin%20-%20Nocturne%20Outputs/Chopin%20-%20Nocturne%20Op.%209%20%28Password%20is%20Chopin%29.exe) — password: `Chopin` |

Each output is a real working file of its host type — the `.png` opens in any image viewer, the `.wav` plays in any audio player, the `.mkv` plays in any video player, the `.py` runs with `python file.py`, and the `.exe` runs by double-click on Windows.

### Layering

Stone conversions can be chained for additional depth. Save a file as a `.png`, drop the `.png` back in and save it as a `.wav`, then the `.wav` as an `.mkv`, and so on. Each layer can carry its own password — recovering the original means unwrapping every layer in reverse order with the right password at each step. Forgetting any one password breaks the chain. Used sparingly this is a heavy increase in protection at the cost of file size and recovery effort.

## Markdown bundles (rich-format conversions)

When converting docx / pdf / epub / pptx → md, the markdown writer detects embedded images and produces a folder-structured output:

```
desired_output_location/
   my_document/
       my_document.md
       images/
           image1.png
           image2.jpg
```

The `.md` references images by relative paths (`![alt](images/image1.png)`). Pure-text conversions still produce a flat `.md`. The reverse direction (md → docx) reads this structure if present and re-embeds the images.

## Scope notes / known limits

- **Read-only formats:** PPTX, ODT, SVG, RTF write (RTF read works via striprtf).
- **PDF write** supports headings, paragraphs, lists, basic tables, and image placeholders. Bundled DejaVu Sans gives Latin Extended + Cyrillic + Greek coverage.
- **PDF read** handles uncompressed and FlateDecode streams. Encrypted PDFs and image-only (scanned, non-OCR) PDFs are not supported.
- **3D conversions** preserve geometry by default. When the source has animation/rig data AND the target format can carry it (`.fbx`, `.dae`, `.glb`, `.gltf`), Vitriol asks once — Yes attempts to preserve animations through the export (best-effort; outcome depends on the format pair), No strips them cleanly for predictable static-geometry output. Targets that can't carry animations by spec (`.obj`, `.stl`, `.ply`, `.3ds`) skip the prompt and always export geometry only.

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

The launcher fetches these from their official upstream sources over HTTPS on first run, if missing. SHA-256 verification is supported per release; see the constants at the top of `launcher.py` to lock specific versions before shipping.
