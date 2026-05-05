"""Music encoder for cross-category Stone audio outputs.

When a Stone-mode cross-category conversion outputs to an audio target
(WAV, AIFF, FLAC), this module renders a music-like sample stream whose
low 4 bits per sample carry the source payload bytes.

Output format constants:
  - 44.1 kHz sample rate
  - 16-bit signed PCM
  - Stereo (2 channels)

Bit-packing:
  - Each 16-bit sample reserves the LOW 4 BITS for payload. Music part
    occupies the remaining range and is amplitude-bounded by MUSIC_HEADROOM.
  - Stereo frame holds 8 payload bits = 1 byte. At 44.1 kHz, 1 second of
    music carries 44,100 bytes of payload.

Two encoding paths share the bit-packer:

  encode_music_envelope (v3, NEW):
    Caller supplies a self-describing v3 envelope (MAGIC_V3_AUDIO + length
    + IV + salt + ciphertext). Encoder bit-packs the envelope verbatim
    into PCM samples. No header wrapper, no compression — the envelope
    is already self-describing AND incompressible (it's ciphertext).

  encode_music_payload (uM01, LEGACY):
    Caller supplies a UCMSv1 envelope. Encoder zlib-compresses, prepends
    a 12-byte uM01 header (magic + sizes), bit-packs. Kept for backward
    decoding of pre-v3 audio Stone files.

Determinism: chord progression, key signature, tempo, and voicing are
derived from SHA-256 of the bit-packed input bytes. Same source always
produces the same music.

This is a presentation feature, not steganography. The payload is
recoverable through Transmute via the symmetric decoder.
"""
from __future__ import annotations
import hashlib
import math
import struct
import zlib
from typing import Iterator, List, Tuple

# WAV/AIFF/FLAC output parameters (fixed across this module).
SAMPLE_RATE = 44100
CHANNELS = 2
BITS_PER_SAMPLE = 16
BYTES_PER_SAMPLE = BITS_PER_SAMPLE // 8
PAYLOAD_BITS_PER_SAMPLE = 4              # bottom 4 bits of each sample
PAYLOAD_BITS_PER_FRAME = PAYLOAD_BITS_PER_SAMPLE * CHANNELS  # 8 bits = 1 byte
# Music synthesis amplitude cap. Sized to land peaks at ≈ 50% of int16 full
# scale (±16384), making outputs sound like normal music rather than the
# 18 dB-quieter-than-CD dribble earlier versions produced. The bottom 4 bits
# of the synthesis output get overwritten with payload nibbles regardless,
# so the effective resolution is 12 bits — quantization noise sits at
# ~-72 dBFS, inaudible against the chord backing.
MUSIC_HEADROOM = 16384

# Chord progressions, encoded by 4-bit index. Each entry is a list of scale
# degrees (1-indexed Roman numerals translated to integers) describing chord
# roots within the scale. Quality (major/minor/dim) is implied by the
# diatonic position (I, IV, V = major; ii, iii, vi = minor; vii = dim) for
# major keys; mirrored for minor keys.
PROGRESSIONS: List[List[int]] = [
    # Original 16 — common pop / rock / jazz / blues progressions
    [1, 5, 6, 4],     # I-V-vi-IV   (most common pop)
    [2, 5, 1, 1],     # ii-V-I      (jazz standard, pad I)
    [1, 6, 4, 5],     # I-vi-IV-V   (50s doo-wop)
    [6, 4, 1, 5],     # vi-IV-I-V
    [1, 4, 5, 5],     # I-IV-V (12-bar blues simplified)
    [1, 1, 4, 5],     # I-I-IV-V
    [1, 5, 4, 1],     # I-V-IV-I
    [6, 5, 4, 5],     # vi-V-IV-V (modal vamp)
    [1, 3, 4, 6],     # I-iii-IV-vi
    [4, 1, 5, 6],     # IV-I-V-vi
    [1, 4, 6, 5],     # I-IV-vi-V
    [2, 4, 5, 1],     # ii-IV-V-I
    [1, 7, 6, 5],     # I-VII-vi-V (descending)
    [6, 7, 1, 1],     # vi-VII-I (rock cadence)
    [1, 5, 6, 3],     # I-V-vi-iii
    [4, 5, 1, 6],     # IV-V-I-vi
    # Added 16 — modal interchange, jazz turnarounds, longer cycles
    [1, 5, 4, 5],     # I-V-IV-V (sustained tonic)
    [6, 2, 5, 1],     # vi-ii-V-I (jazz turnaround)
    [1, 6, 2, 5],     # I-vi-ii-V (rhythm changes)
    [4, 4, 1, 1],     # IV-IV-I-I (plagal feel)
    [3, 6, 2, 5],     # iii-vi-ii-V (descending circle)
    [1, 4, 7, 3],     # I-IV-VII-iii (modal)
    [6, 3, 4, 1],     # vi-iii-IV-I
    [2, 1, 4, 5],     # ii-I-IV-V
    [1, 3, 6, 4],     # I-iii-vi-IV
    [5, 4, 1, 6],     # V-IV-I-vi
    [1, 6, 7, 5],     # I-vi-VII-V
    [4, 6, 1, 5],     # IV-vi-I-V
    [3, 4, 5, 6],     # iii-IV-V-vi
    [1, 5, 6, 7],     # I-V-vi-VII
    [6, 4, 5, 1],     # vi-IV-V-I (epic cadence)
    [2, 5, 6, 4],     # ii-V-vi-IV
]

# Major-scale intervals in semitones from the tonic.
MAJOR_INTERVALS = [0, 2, 4, 5, 7, 9, 11]
MINOR_INTERVALS = [0, 2, 3, 5, 7, 8, 10]

# Chord quality per degree (T=major, m=minor, d=dim).
MAJOR_QUALITY = ["T", "m", "m", "T", "T", "m", "d"]
MINOR_QUALITY = ["m", "d", "T", "m", "m", "T", "T"]

# Note names indexed by semitone offset from C.
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _seed_params(envelope: bytes) -> Tuple[int, float, int, int]:
    """Hash the envelope bytes (or any deterministic seed material) into
    music parameters. The tempo is then snapped so that
    `samples_per_beat = round(SAMPLE_RATE * 60 / tempo_bpm)` is exact —
    this keeps the percussion grid locked to the chord grid for the
    entire output, no drift over time.

      key_index   : 0..23  — 0..11 = major C..B, 12..23 = minor c..b
      tempo_bpm   : 60..160, snapped to a beat-aligned tempo
      progression : 0..len(PROGRESSIONS)-1
      voicing     : 0=root, 1=first inversion, 2=second inversion
    """
    h = hashlib.sha256(envelope).digest()
    key_index = h[0] % 24
    tempo_byte = h[1]
    tempo_raw = 60.0 + (tempo_byte / 255.0) * 100.0    # 60..160 BPM
    samples_per_beat = max(1, round(SAMPLE_RATE * 60.0 / tempo_raw))
    tempo_bpm = SAMPLE_RATE * 60.0 / samples_per_beat
    progression = h[2] % len(PROGRESSIONS)
    voicing = h[3] % 3
    return key_index, tempo_bpm, progression, voicing


def _midi_to_hz(midi_note: int) -> float:
    """A4 = 440 Hz = MIDI 69."""
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


def _build_chord(root_midi: int, quality: str, voicing: int = 0) -> List[int]:
    """Return MIDI notes for a triad rooted at root_midi.
    quality: 'T' major (0,4,7), 'm' minor (0,3,7), 'd' diminished (0,3,6).
    voicing: 0=root position, 1=first inversion, 2=second inversion."""
    intervals = {"T": (0, 4, 7), "m": (0, 3, 7), "d": (0, 3, 6)}[quality]
    notes = [root_midi + i for i in intervals]
    if voicing == 1:
        return [notes[1], notes[2], notes[0] + 12]
    if voicing == 2:
        return [notes[2], notes[0] + 12, notes[1] + 12]
    return notes


def _frames_for_bytes(n_bytes: int) -> int:
    """Number of stereo frames needed to carry n_bytes of payload at 4-bit
    embedding per channel (8 bits per stereo frame)."""
    return n_bytes  # 1 byte per stereo frame


def _synth_kick(samples_per_beat: int) -> List[int]:
    """Bass-drum-like waveform. Sine sweep 150 Hz → 50 Hz over an
    exponentially-decaying envelope (~80 ms). Caps at half MUSIC_HEADROOM
    so it sums into the chord backing without clipping. Length is bounded
    by samples_per_beat so a single hit can't bleed into the next beat."""
    duration = min(int(SAMPLE_RATE * 0.08), max(1, samples_per_beat - 1))
    if duration < 2:
        return []
    f0 = 150.0
    f1 = 50.0
    amp = MUSIC_HEADROOM // 3
    out = [0] * duration
    phase = 0.0
    for i in range(duration):
        # Exponential frequency sweep — characteristic kick "thump".
        freq = f0 * (f1 / f0) ** (i / duration)
        phase += 2.0 * math.pi * freq / SAMPLE_RATE
        env = math.exp(-i / (duration * 0.3))
        out[i] = int(amp * env * math.sin(phase))
    return out


def _synth_click(samples_per_beat: int) -> List[int]:
    """High click / hi-hat tick. Brief 3.5 kHz tone burst with fast
    exponential decay (~15 ms). Quieter than the kick so the rhythm
    section doesn't dominate the chord backing."""
    duration = min(int(SAMPLE_RATE * 0.015), max(1, samples_per_beat - 1))
    if duration < 2:
        return []
    freq = 3500.0
    amp = MUSIC_HEADROOM // 6
    out = [0] * duration
    for i in range(duration):
        env = math.exp(-i / (duration * 0.25))
        out[i] = int(amp * env * math.sin(2.0 * math.pi * freq * i / SAMPLE_RATE))
    return out


def _generate_music_samples(n_frames: int, key_index: int,
                             tempo_bpm: float, progression_idx: int,
                             voicing: int = 0,
                             ) -> Iterator[Tuple[int, int]]:
    """Yield exactly n_frames (left, right) sample tuples in the music
    amplitude range (signed; payload bits will be packed in by the caller).

    The tempo is assumed pre-snapped by `_seed_params` so that
    `samples_per_beat` is an exact integer — this keeps percussion locked
    to the chord grid for the duration of the output."""
    is_minor = key_index >= 12
    tonic_offset = key_index % 12  # 0=C, 1=C#, ..., 11=B
    intervals = MINOR_INTERVALS if is_minor else MAJOR_INTERVALS
    qualities = MINOR_QUALITY if is_minor else MAJOR_QUALITY
    progression = PROGRESSIONS[progression_idx]

    # Tonic at MIDI 60 (C4) shifted by key offset; minor uses same root.
    base_midi = 60 + tonic_offset

    # Beats per second; one chord per 2 beats by default.
    samples_per_beat = max(1, round(SAMPLE_RATE * 60.0 / tempo_bpm))
    samples_per_chord = samples_per_beat * 2

    # Pre-compute percussion: kick on beat 1, click on beat 3 of every
    # 4-beat measure. Grid is sample-exact because samples_per_beat is
    # integer (see _seed_params snap).
    kick = _synth_kick(samples_per_beat)
    click = _synth_click(samples_per_beat)
    measure_samples = samples_per_beat * 4
    click_offset = samples_per_beat * 2  # beat 3 (0-indexed)

    # Attack/release envelope (50 ms each).
    env_samples = int(SAMPLE_RATE * 0.05)
    if env_samples < 1:
        env_samples = 1

    # Vibrato parameters.
    vib_freq = 5.0  # Hz
    vib_depth = 0.005  # 0.5% pitch variation

    chord_index = 0
    chord_sample_pos = 0
    cur_chord_freqs: List[float] = []

    def _refresh_chord():
        nonlocal cur_chord_freqs
        degree = progression[chord_index % len(progression)]
        scale_idx = (degree - 1) % 7
        chord_root_midi = base_midi + intervals[scale_idx]
        notes = _build_chord(chord_root_midi, qualities[scale_idx], voicing)
        cur_chord_freqs = [_midi_to_hz(n) for n in notes]

    _refresh_chord()
    # Per-voice phase accumulators (radians)
    phases = [0.0 for _ in cur_chord_freqs]

    two_pi_over_sr = 2.0 * math.pi / SAMPLE_RATE
    for f in range(n_frames):
        # Roll over chord boundary.
        if chord_sample_pos >= samples_per_chord:
            chord_index += 1
            chord_sample_pos = 0
            _refresh_chord()
            phases = [0.0 for _ in cur_chord_freqs]

        # Envelope (linear attack, sustained, linear release).
        if chord_sample_pos < env_samples:
            env = chord_sample_pos / env_samples
        elif chord_sample_pos > samples_per_chord - env_samples:
            env = max(0.0, (samples_per_chord - chord_sample_pos) / env_samples)
        else:
            env = 1.0

        # Vibrato modulator.
        vib = 1.0 + vib_depth * math.sin(two_pi_over_sr * vib_freq * f)

        # Sum voices. Per-voice amplitude is sized so an in-phase 3-voice
        # constructive peak just reaches MUSIC_HEADROOM — the post-summation
        # clamp catches the rare overshoot. Real chord material rarely
        # phase-aligns, so typical RMS sits well below the cap.
        n_voices = len(cur_chord_freqs)
        amp_per_voice = MUSIC_HEADROOM // max(1, n_voices)
        sample_value = 0
        for i, base_freq in enumerate(cur_chord_freqs):
            freq = base_freq * vib
            phases[i] += two_pi_over_sr * freq
            sample_value += int(amp_per_voice * env * math.sin(phases[i]))

        # Percussion overlay (mono, summed into both channels). Locked to
        # the beat grid: kick on beat 1, click on beat 3 of every measure.
        f_in_measure = f % measure_samples
        if f_in_measure < len(kick):
            sample_value += kick[f_in_measure]
        delta = f_in_measure - click_offset
        if 0 <= delta < len(click):
            sample_value += click[delta]

        # Soft saturation guard so payload bits aren't clipped at the top
        # of the int16 range. Hits ±MUSIC_HEADROOM only on rare summed peaks.
        if sample_value > MUSIC_HEADROOM - 1:
            sample_value = MUSIC_HEADROOM - 1
        elif sample_value < -MUSIC_HEADROOM:
            sample_value = -MUSIC_HEADROOM

        yield sample_value, sample_value
        chord_sample_pos += 1


def _pack_payload_into_samples(music_samples: Iterator[Tuple[int, int]],
                                payload: bytes,
                                ) -> Iterator[Tuple[int, int]]:
    """Yield modified (left, right) samples with the bottom 4 bits of each
    channel set from successive nibbles of payload.

    Frame F carries payload byte F (low nibble in left channel, high nibble
    in right channel). Each music sample is shifted left by 4 to clear the
    bottom 4 bits, then OR'd with the payload nibble.

    After the payload is exhausted, music samples pass through unmodified
    (with bottom 4 bits zeroed for consistency on extract — extract trusts
    the encoded payload length to know where to stop)."""
    payload_len = len(payload)
    mask_clear = ~((1 << PAYLOAD_BITS_PER_SAMPLE) - 1) & 0xFFFF
    # `mask_clear` for signed 16-bit needs care: we operate on the unsigned
    # 16-bit representation when bit-fiddling.

    def _embed(sample_signed: int, nibble: int) -> int:
        # Convert signed-16 to unsigned-16, clear low 4 bits, OR nibble,
        # convert back to signed-16.
        u = sample_signed & 0xFFFF
        u = (u & mask_clear) | (nibble & 0x0F)
        if u & 0x8000:
            return u - 0x10000
        return u

    def _zero_low(sample_signed: int) -> int:
        return _embed(sample_signed, 0)

    for f, (left, right) in enumerate(music_samples):
        if f < payload_len:
            byte = payload[f]
            low_nibble = byte & 0x0F
            high_nibble = (byte >> 4) & 0x0F
            yield _embed(left, low_nibble), _embed(right, high_nibble)
        else:
            yield _zero_low(left), _zero_low(right)


def _samples_to_pcm_le16(samples: Iterator[Tuple[int, int]]) -> Iterator[bytes]:
    """Pack (left, right) tuples as little-endian 16-bit signed PCM."""
    for left, right in samples:
        yield struct.pack("<hh", left, right)


def _samples_to_pcm_be16(samples: Iterator[Tuple[int, int]]) -> Iterator[bytes]:
    """Pack (left, right) tuples as big-endian 16-bit signed PCM (for AIFF)."""
    for left, right in samples:
        yield struct.pack(">hh", left, right)


def encode_music_payload(envelope: bytes) -> Tuple[bytes, int]:
    """LEGACY (uM01) audio Stone path. Compresses the envelope with zlib,
    prepends a 12-byte uM01 header, and bit-packs into LE PCM samples.

    Kept for two reasons:
      1. Same-category audio targets that flow through the music encoder
         (currently none — same-category WAV/AIFF use _wav_embed not
         _wav_embed_music — but harmless to keep available).
      2. Decoder backward-compatibility with audio Stone files generated
         before MAGIC_V3_AUDIO shipped (decode_music_payload_le still
         expects the uM01 header).

    NEW v3 audio Stone files use `encode_music_envelope` instead — no
    zlib (ciphertext is already incompressible) and the v3 envelope
    carries its own self-describing header.
    """
    compressed = zlib.compress(envelope, level=6)
    header = b"uM01" + struct.pack(">II", len(envelope), len(compressed))
    full_payload = header + compressed
    key_index, tempo_bpm, progression, voicing = _seed_params(full_payload)
    n_frames = _frames_for_bytes(len(full_payload))
    samples = _generate_music_samples(n_frames, key_index, tempo_bpm,
                                       progression, voicing)
    embedded = _pack_payload_into_samples(samples, full_payload)
    out = bytearray()
    for chunk in _samples_to_pcm_le16(embedded):
        out.extend(chunk)
    return bytes(out), n_frames


def encode_music_payload_be(envelope: bytes) -> Tuple[bytes, int]:
    """LEGACY (uM01) BE-PCM variant for AIFF. See encode_music_payload."""
    compressed = zlib.compress(envelope, level=6)
    header = b"uM01" + struct.pack(">II", len(envelope), len(compressed))
    full_payload = header + compressed
    key_index, tempo_bpm, progression, voicing = _seed_params(full_payload)
    n_frames = _frames_for_bytes(len(full_payload))
    samples = _generate_music_samples(n_frames, key_index, tempo_bpm,
                                       progression, voicing)
    embedded = _pack_payload_into_samples(samples, full_payload)
    out = bytearray()
    for chunk in _samples_to_pcm_be16(embedded):
        out.extend(chunk)
    return bytes(out), n_frames


def encode_music_envelope(envelope: bytes) -> Tuple[bytes, int]:
    """NEW v3 audio Stone path. The envelope is a self-describing v3
    envelope built by `masquerade._v3_audio_envelope` — magic + length +
    IV + salt + ciphertext. Bit-packs verbatim into LE PCM samples (no
    zlib wrapper, no extra header)."""
    key_index, tempo_bpm, progression, voicing = _seed_params(envelope)
    n_frames = _frames_for_bytes(len(envelope))
    samples = _generate_music_samples(n_frames, key_index, tempo_bpm,
                                       progression, voicing)
    embedded = _pack_payload_into_samples(samples, envelope)
    out = bytearray()
    for chunk in _samples_to_pcm_le16(embedded):
        out.extend(chunk)
    return bytes(out), n_frames


def encode_music_envelope_be(envelope: bytes) -> Tuple[bytes, int]:
    """NEW v3 BE-PCM variant for AIFF. See encode_music_envelope."""
    key_index, tempo_bpm, progression, voicing = _seed_params(envelope)
    n_frames = _frames_for_bytes(len(envelope))
    samples = _generate_music_samples(n_frames, key_index, tempo_bpm,
                                       progression, voicing)
    embedded = _pack_payload_into_samples(samples, envelope)
    out = bytearray()
    for chunk in _samples_to_pcm_be16(embedded):
        out.extend(chunk)
    return bytes(out), n_frames


def decode_music_payload(pcm_le16_bytes: bytes) -> bytes:
    """Reverse of encode_music_payload. Reads the music header, extracts
    the bottom-4-bit payload stream, zlib-decompresses, returns the
    original envelope bytes.

    Raises ValueError on malformed input."""
    return _decode_music_payload(pcm_le16_bytes, ">")  # actually reads as LE


def decode_music_payload_le(pcm_le16_bytes: bytes) -> bytes:
    return _decode_music_payload_endian(pcm_le16_bytes, "<")


def decode_music_payload_be(pcm_be16_bytes: bytes) -> bytes:
    return _decode_music_payload_endian(pcm_be16_bytes, ">")


def _decode_music_payload(pcm_bytes: bytes, _byte_order: str) -> bytes:
    return _decode_music_payload_endian(pcm_bytes, "<")


def decode_music_bytes_le(pcm_bytes: bytes, n_bytes: int) -> bytes:
    """Bit-unpack the first n_bytes of payload from a LE-PCM stream. No
    header parsing, no zlib — used by the v3 audio decoder which has its
    own self-describing envelope sitting at byte 0 of the payload stream."""
    return _decode_music_bytes_endian(pcm_bytes, "<", n_bytes)


def decode_music_bytes_be(pcm_bytes: bytes, n_bytes: int) -> bytes:
    """BE-PCM variant for AIFF. See decode_music_bytes_le."""
    return _decode_music_bytes_endian(pcm_bytes, ">", n_bytes)


def _decode_music_bytes_endian(pcm_bytes: bytes, byte_order: str,
                                n_bytes: int) -> bytes:
    frame_size = BYTES_PER_SAMPLE * CHANNELS
    n_frames_avail = len(pcm_bytes) // frame_size
    n = max(0, min(n_bytes, n_frames_avail))
    if n == 0:
        return b""
    out = bytearray(n)
    fmt = byte_order + "hh"
    for i in range(n):
        off = i * frame_size
        left, right = struct.unpack(fmt, pcm_bytes[off:off + frame_size])
        out[i] = ((right & 0x0F) << 4) | (left & 0x0F)
    return bytes(out)


def _decode_music_payload_endian(pcm_bytes: bytes, byte_order: str) -> bytes:
    """Extract the bottom-4-bit payload stream from a music PCM blob.
    byte_order: '<' for WAV/FLAC LE, '>' for AIFF BE."""
    frame_size = BYTES_PER_SAMPLE * CHANNELS  # 4 bytes per stereo frame
    n_frames = len(pcm_bytes) // frame_size
    if n_frames < 12:
        raise ValueError("music payload: file too short to contain header")

    def _read_byte(frame_idx: int) -> int:
        off = frame_idx * frame_size
        left, right = struct.unpack(byte_order + "hh",
                                     pcm_bytes[off:off + frame_size])
        low = left & 0x0F
        high = right & 0x0F
        return (high << 4) | low

    # Read first 12 bytes for our music header (uM01 + sizes).
    header = bytearray(_read_byte(i) for i in range(12))
    if bytes(header[:4]) != b"uM01":
        raise ValueError("music payload: header magic not found")
    env_size, comp_size = struct.unpack(">II", bytes(header[4:12]))

    needed_frames = 12 + comp_size
    if needed_frames > n_frames:
        raise ValueError(
            f"music payload: header claims {comp_size} compressed bytes; "
            f"only {n_frames - 12} frames available")

    compressed = bytearray(_read_byte(i) for i in range(12, 12 + comp_size))
    try:
        envelope = zlib.decompress(bytes(compressed))
    except zlib.error as e:
        raise ValueError(f"music payload: zlib decompression failed: {e}")
    if len(envelope) != env_size:
        raise ValueError(
            f"music payload: decoded size mismatch ({len(envelope)} != {env_size})")
    return envelope
