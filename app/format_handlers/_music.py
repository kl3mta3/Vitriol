"""Music encoder for cross-category Stone audio outputs.

When a Stone-mode cross-category conversion outputs to an audio target
(WAV, AIFF, FLAC), this module renders a music-like sample stream whose
low 4 bits per sample carry the source payload (zlib-compressed envelope).

Output format constants:
  - 44.1 kHz sample rate
  - 16-bit signed PCM
  - Stereo (2 channels)

Bit-packing:
  - Each 16-bit sample reserves the LOW 4 BITS for payload, leaving 12 bits
    of music headroom (~72 dB dynamic range — audibly indistinguishable
    from CD audio for synthesized chord tones).
  - Stereo frame holds 8 payload bits = 1 byte. So 1 byte of payload =
    1 stereo frame. At 44.1 kHz, 1 second of music = 44,100 bytes payload.

Input transformation:
  - Source bytes are zlib-compressed before bit-packing. Most non-audio
    sources (text, code, PDFs) compress 30-70%; already-compressed sources
    (PNGs, JPEGs, MP3s) gain little. Output size is roughly 1.5-2x source
    for compressible inputs, 3-4x for incompressible ones.

Determinism: chord progression, key signature, and tempo are derived
deterministically from the envelope header bytes (SHA-256 → bit fields).
Same source always produces the same music.

This is a presentation feature, not steganography. The payload is
recoverable through Transmute via the symmetric extract function.
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
MUSIC_BITS_PER_SAMPLE = BITS_PER_SAMPLE - PAYLOAD_BITS_PER_SAMPLE  # 12
MUSIC_HEADROOM = 1 << (MUSIC_BITS_PER_SAMPLE - 1)  # signed-12-bit max = 2048

# Chord progressions, encoded by 4-bit index. Each entry is a list of scale
# degrees (1-indexed Roman numerals translated to integers) describing chord
# roots within the scale. Quality (major/minor/dim) is implied by the
# diatonic position (I, IV, V = major; ii, iii, vi = minor; vii = dim) for
# major keys; mirrored for minor keys.
PROGRESSIONS: List[List[int]] = [
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
]

# Major-scale intervals in semitones from the tonic.
MAJOR_INTERVALS = [0, 2, 4, 5, 7, 9, 11]
MINOR_INTERVALS = [0, 2, 3, 5, 7, 8, 10]

# Chord quality per degree (T=major, m=minor, d=dim).
MAJOR_QUALITY = ["T", "m", "m", "T", "T", "m", "d"]
MINOR_QUALITY = ["m", "d", "T", "m", "m", "T", "T"]

# Note names indexed by semitone offset from C.
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _seed_params(envelope: bytes) -> Tuple[int, float, int]:
    """Hash the envelope bytes (or any deterministic seed material) into
    music parameters.
      key_index   : 0..23  — 0..11 = major C..B, 12..23 = minor c..b
      tempo_bpm   : 60..120
      progression : 0..15  — index into PROGRESSIONS
    """
    h = hashlib.sha256(envelope).digest()
    key_index = h[0] % 24
    tempo_byte = h[1]
    tempo_bpm = 60.0 + (tempo_byte / 255.0) * 60.0
    progression = h[2] % len(PROGRESSIONS)
    return key_index, tempo_bpm, progression


def _midi_to_hz(midi_note: int) -> float:
    """A4 = 440 Hz = MIDI 69."""
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


def _build_chord(root_midi: int, quality: str) -> List[int]:
    """Return MIDI notes for a triad rooted at root_midi.
    quality: 'T' major (0,4,7), 'm' minor (0,3,7), 'd' diminished (0,3,6)."""
    intervals = {"T": (0, 4, 7), "m": (0, 3, 7), "d": (0, 3, 6)}[quality]
    return [root_midi + i for i in intervals]


def _frames_for_bytes(n_bytes: int) -> int:
    """Number of stereo frames needed to carry n_bytes of payload at 4-bit
    embedding per channel (8 bits per stereo frame)."""
    return n_bytes  # 1 byte per stereo frame


def _generate_music_samples(n_frames: int, key_index: int,
                             tempo_bpm: float, progression_idx: int
                             ) -> Iterator[Tuple[int, int]]:
    """Yield exactly n_frames (left, right) sample tuples in the 12-bit
    music range (signed; payload bits will be packed in by the caller)."""
    is_minor = key_index >= 12
    tonic_offset = key_index % 12  # 0=C, 1=C#, ..., 11=B
    intervals = MINOR_INTERVALS if is_minor else MAJOR_INTERVALS
    qualities = MINOR_QUALITY if is_minor else MAJOR_QUALITY
    progression = PROGRESSIONS[progression_idx]

    # Tonic at MIDI 60 (C4) shifted by key offset; minor uses same root.
    base_midi = 60 + tonic_offset

    # Beats per second; one chord per 2 beats by default.
    beats_per_sec = tempo_bpm / 60.0
    samples_per_beat = SAMPLE_RATE / beats_per_sec
    samples_per_chord = int(samples_per_beat * 2.0)

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
        notes = _build_chord(chord_root_midi, qualities[scale_idx])
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

        # Sum voices, scaled to fit the 12-bit headroom with margin.
        # Divide by len(voices)*2 to leave room for envelope/vibrato peaks.
        n_voices = len(cur_chord_freqs)
        amp_per_voice = MUSIC_HEADROOM // (n_voices * 2)
        sample_value = 0
        for i, base_freq in enumerate(cur_chord_freqs):
            freq = base_freq * vib
            phases[i] += two_pi_over_sr * freq
            sample_value += int(amp_per_voice * env * math.sin(phases[i]))

        # Soft saturation guard so payload bits aren't clipped.
        if sample_value > MUSIC_HEADROOM - 1:
            sample_value = MUSIC_HEADROOM - 1
        elif sample_value < -MUSIC_HEADROOM:
            sample_value = -MUSIC_HEADROOM

        # Stereo: light per-channel detune for spaciousness.
        # Low channel = sample_value, high channel = slightly delayed (frame-1).
        # For simplicity we just return identical channels here; spatializer
        # is out of scope.
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
    """Compress the envelope, generate music samples, embed payload bits,
    and return (raw_pcm_le16_bytes, n_frames). The PCM is ready to be
    wrapped in WAV/AIFF/FLAC containers.

    Returns a single bytes object — for typical Stone payloads (under
    ~50 MB after compression) this fits easily in memory. Streaming output
    can be added later if Stone starts being used for huge sources.
    """
    compressed = zlib.compress(envelope, level=6)
    # Header bytes to identify the music payload on extract. Embed:
    #   4 bytes magic "uM01"
    #   4 bytes uncompressed envelope size (BE uint32)
    #   4 bytes compressed payload size (BE uint32)
    #   payload bytes (zlib-compressed envelope)
    header = b"uM01" + struct.pack(">II", len(envelope), len(compressed))
    full_payload = header + compressed

    # Seed for music params is derived from the FULL payload bytes so the
    # music is deterministic from the input.
    key_index, tempo_bpm, progression = _seed_params(full_payload)

    n_frames = _frames_for_bytes(len(full_payload))

    samples = _generate_music_samples(n_frames, key_index, tempo_bpm, progression)
    embedded = _pack_payload_into_samples(samples, full_payload)
    out = bytearray()
    for chunk in _samples_to_pcm_le16(embedded):
        out.extend(chunk)
    return bytes(out), n_frames


def encode_music_payload_be(envelope: bytes) -> Tuple[bytes, int]:
    """Same as encode_music_payload but emits big-endian PCM (for AIFF)."""
    compressed = zlib.compress(envelope, level=6)
    header = b"uM01" + struct.pack(">II", len(envelope), len(compressed))
    full_payload = header + compressed
    key_index, tempo_bpm, progression = _seed_params(full_payload)
    n_frames = _frames_for_bytes(len(full_payload))
    samples = _generate_music_samples(n_frames, key_index, tempo_bpm, progression)
    embedded = _pack_payload_into_samples(samples, full_payload)
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
