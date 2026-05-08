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
recoverable through Vitriol via the symmetric decoder.
"""
from __future__ import annotations
import hashlib
import math
import random
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

# Minimum audio duration floor: 10 seconds. Tiny payloads would otherwise
# produce millisecond-long audio files (a 50-byte payload = 50 stereo
# frames = 1.1 ms of audio). That's a forensic tell — a real music file
# is never that short. The audio Stone-pack synthesizes the same chord
# progression / drum / arpeggio system it normally would, padded to the
# minimum length via the v3 audio envelope's `pad_to_inner` mechanism.
# The decoder reads the inner-plaintext `payload_len` field and discards
# the random tail, so round-trip is byte-perfect regardless of padding.
MUSIC_MIN_FRAMES = SAMPLE_RATE * 10        # 441,000 stereo frames = 10 sec at 44.1 kHz
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


# Drum patterns: per-section beat patterns within a 4-beat measure.
# Each entry is (kick_beats, click_beats), 0-indexed beats in the measure.
# All entries land on a beat — never off-grid — so the on-grid rule holds.
# Pattern variation between sections breaks the "regular transient every N
# seconds" statistical fingerprint that pure kick-on-1 / click-on-3 produces.
DRUM_PATTERNS: List[Tuple[List[int], List[int]]] = [
    ([0],       [2]),         # pop default — kick on 1, click on 3
    ([0, 2],    [1]),         # driving — kick on 1+3, click on 2
    ([0],       [1, 3]),      # half-time — kick on 1, clicks on 2+4
    ([0, 2],    [1, 3]),      # rock — kick on 1+3, clicks on 2+4
    ([0],       [1, 2, 3]),   # sparse kick + busy clicks
    ([0, 2],    [3]),         # off-beat lift — kick 1+3, click on the 4
    ([0],       []),          # kick only, no click — minimal section
    ([],        [0, 2]),      # click-only, no kick — breakdown section
]


# Section transformations: per-section key/mode shifts relative to the file's
# base key. Each is (semitone_offset, flip_mode). Heavily biased toward the
# tonic (offset=0, no flip) so the file feels like one song with occasional
# excursions, not a random key-jumping medley. Modulation targets are all
# closely related keys (IV, V, relative major/minor, ♭VII).
SECTION_TRANSFORMS: List[Tuple[int, bool]] = [
    (0,  False),   # tonic, same mode  ── 5 weighted entries below ↓
    (0,  False),
    (0,  False),
    (0,  False),
    (0,  False),
    (5,  False),   # IV — subdominant, same mode
    (7,  False),   # V — dominant, same mode
    (9,  True),    # relative minor (from major) or relative major (from minor)
    (-2, False),   # ♭VII — common rock/modal modulation
]


# Arpeggio patterns: indices into the chord-tone list, played as 8th-note
# sequence on top of the held pad. Each chord has 3 tones (0=root, 1=third,
# 2=fifth); we use modulo so longer patterns walk through octave-up tones too.
# Adds melodic motion *within* each chord so 1.3-second held triads don't
# feel like a sustained drone. Each section picks one (or `None` for an
# ambient pad-only section, weighted in via the None entry below).
ARP_PATTERNS: List[Tuple] = [
    None,                          # ambient — no arpeggio, pad only
    (0, 1, 2, 3),                  # ascending: root, third, fifth, octave-root
    (3, 2, 1, 0),                  # descending: octave, fifth, third, root
    (0, 2, 1, 2),                  # broken triad (Alberti-bass-style)
    (0, 1, 2, 3, 2, 1, 0, 1),      # up-down (8 notes, two beats per cycle)
    (0, 2, 4, 2),                  # wide intervals (root, fifth, ninth, fifth)
    (0, 0, 2, 2),                  # rhythmic pulse on root + fifth
]


def _seed_params(envelope: bytes) -> Tuple[int, float]:
    """File-wide music parameters seeded from the payload bytes. Tempo is
    snapped so `samples_per_beat = round(SAMPLE_RATE * 60 / tempo_bpm)` is
    exact — this keeps the percussion grid locked for the entire output.

    Per-section parameters (progression, voicing, key offset, drum pattern)
    are picked separately by `_plan_sections`.

      key_index : 0..23  — 0..11 = major C..B, 12..23 = minor c..b
      tempo_bpm : 60..160, snapped to a beat-aligned tempo
    """
    h = hashlib.sha256(envelope).digest()
    key_index = h[0] % 24
    tempo_byte = h[1]
    tempo_raw = 60.0 + (tempo_byte / 255.0) * 100.0    # 60..160 BPM
    samples_per_beat = max(1, round(SAMPLE_RATE * 60.0 / tempo_raw))
    tempo_bpm = SAMPLE_RATE * 60.0 / samples_per_beat
    return key_index, tempo_bpm


# A section descriptor: (start_frame, end_frame, key_offset_semitones,
# mode_flip, progression_idx, voicing, drum_pattern_idx, arp_pattern_idx).
SectionTuple = Tuple[int, int, int, bool, int, int, int, int]


def _plan_sections(envelope: bytes, n_frames: int,
                    samples_per_beat: int) -> List[SectionTuple]:
    """Plan a verse/chorus/bridge-style section schedule for the file.

    Each section is 8–24 measures long (variable, seeded from the envelope
    hash so the section-length pattern itself isn't a regular grid). Each
    section gets its own progression, voicing, drum pattern, and key
    transformation relative to the file's base key. The mix is biased
    toward staying on the tonic so a file feels like one song with
    occasional excursions, not a key-jumping medley.

    Section seeds are derived via HMAC(envelope, section_idx) so we have
    32 fresh bytes per section regardless of how many sections we need —
    a 100 MB payload (~75 minutes of audio) needs ~470 sections, far more
    than a single SHA-256 of the envelope could supply.
    """
    samples_per_measure = samples_per_beat * 4
    if samples_per_measure <= 0 or n_frames <= 0:
        return []
    sections: List[SectionTuple] = []
    cursor = 0
    sec_idx = 0
    while cursor < n_frames:
        h = hashlib.sha256(envelope + b"section:" + sec_idx.to_bytes(4, "big")).digest()
        section_measures = 8 + (h[0] % 17)   # 8..24 inclusive
        section_frames = section_measures * samples_per_measure
        end = min(cursor + section_frames, n_frames)
        key_offset, mode_flip = SECTION_TRANSFORMS[h[1] % len(SECTION_TRANSFORMS)]
        # First section always plays in the file's base key/mode so the
        # listener gets oriented before any modulation.
        if sec_idx == 0:
            key_offset, mode_flip = 0, False
        prog_idx = h[2] % len(PROGRESSIONS)
        voicing = h[3] % 3
        drum_idx = h[4] % len(DRUM_PATTERNS)
        arp_idx = h[5] % len(ARP_PATTERNS)
        sections.append((cursor, end, key_offset, mode_flip,
                         prog_idx, voicing, drum_idx, arp_idx))
        cursor = end
        sec_idx += 1
    return sections


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
    embedding per channel (8 bits per stereo frame). Always at least
    `MUSIC_MIN_FRAMES` so very small payloads still produce a 10-second
    audio file — see the constant's comment for the rationale."""
    return max(MUSIC_MIN_FRAMES, n_bytes)  # 1 byte per stereo frame, floored at 10 sec


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
                             tempo_bpm: float,
                             sections: List[SectionTuple],
                             jitter_rng: random.Random,
                             ) -> Iterator[Tuple[int, int]]:
    """Yield exactly n_frames (left, right) sample tuples in the music
    amplitude range (signed; payload bits will be packed in by the caller).

    Walks through `sections` (verse/chorus/bridge schedule). Each section
    can have its own progression, voicing, key offset, mode flip, and
    drum pattern — giving a long file a song-like structure rather than a
    single-progression drone.

    `jitter_rng` is a pre-seeded `random.Random` used to pick deterministic
    ±3 ms micro-timing offsets on each kick/click hit. Same source bytes
    → same RNG seed → same micro-timing pattern, so encoder is fully
    deterministic. (Decoder doesn't care about jitter — it reads bit
    values, not music.)"""
    file_is_minor = key_index >= 12
    file_tonic_offset = key_index % 12  # 0=C..11=B
    file_base_midi = 60 + file_tonic_offset

    samples_per_beat = max(1, round(SAMPLE_RATE * 60.0 / tempo_bpm))
    samples_per_chord = samples_per_beat * 2
    samples_per_measure = samples_per_beat * 4

    # Pre-compute percussion waveforms (same shape across all sections).
    kick = _synth_kick(samples_per_beat)
    click = _synth_click(samples_per_beat)
    micro_jitter_max = int(SAMPLE_RATE * 0.003)   # ±3 ms

    # Pre-plan every drum hit in the file: scan each section's pattern
    # over its measures, apply ±3 ms micro-timing, accumulate (start_frame,
    # waveform) pairs. For typical files this is a few thousand entries —
    # cheap, and lets the main loop just check a bucket per frame.
    hits_by_start: dict[int, List[List[int]]] = {}
    for sec_start, sec_end, _ko, _mf, _pi, _vo, drum_idx, _ai in sections:
        kick_beats, click_beats = DRUM_PATTERNS[drum_idx]
        # First measure that starts at-or-after sec_start.
        first_m = (sec_start + samples_per_measure - 1) // samples_per_measure
        last_m = (sec_end + samples_per_measure - 1) // samples_per_measure
        for m in range(first_m, last_m):
            measure_start = m * samples_per_measure
            for b in kick_beats:
                base = measure_start + b * samples_per_beat
                jitter = jitter_rng.randint(-micro_jitter_max, micro_jitter_max)
                start = base + jitter
                if 0 <= start < n_frames:
                    hits_by_start.setdefault(start, []).append([0, kick])
            for b in click_beats:
                base = measure_start + b * samples_per_beat
                jitter = jitter_rng.randint(-micro_jitter_max, micro_jitter_max)
                start = base + jitter
                if 0 <= start < n_frames:
                    hits_by_start.setdefault(start, []).append([0, click])

    # Attack/release envelope (50 ms each).
    env_samples = max(1, int(SAMPLE_RATE * 0.05))

    # Vibrato.
    vib_freq = 5.0
    vib_depth = 0.005
    two_pi_over_sr = 2.0 * math.pi / SAMPLE_RATE

    # Active drum hits — each entry is [current_offset_into_wave, wave_list].
    active_hits: List[List] = []

    # Section / chord state — recomputed when section or chord rolls over.
    sec_idx = 0
    cur_section = sections[0] if sections else (0, n_frames, 0, False, 0, 0, 0, 0)
    (sec_start, sec_end, key_offset, mode_flip,
     prog_idx, voicing, _drum_idx, arp_idx) = cur_section
    section_is_minor = (not file_is_minor) if mode_flip else file_is_minor
    section_intervals = MINOR_INTERVALS if section_is_minor else MAJOR_INTERVALS
    section_qualities = MINOR_QUALITY if section_is_minor else MAJOR_QUALITY
    section_base_midi = file_base_midi + key_offset
    progression = PROGRESSIONS[prog_idx]
    arp_pattern = ARP_PATTERNS[arp_idx]

    chord_index = 0
    chord_sample_pos = 0
    cur_chord_freqs: List[float] = []
    cur_arp_freqs: List[float] = []
    phases: List[float] = []
    arp_phase = 0.0
    arp_cur_idx = -1

    # Arpeggio note duration: 8th note (samples_per_beat // 2). Each held
    # chord (samples_per_chord = 2 beats) carries 4 arpeggio notes.
    samples_per_arp_note = max(1, samples_per_beat // 2)

    def _refresh_chord() -> None:
        nonlocal cur_chord_freqs, cur_arp_freqs, phases
        degree = progression[chord_index % len(progression)]
        scale_idx = (degree - 1) % 7
        chord_root_midi = section_base_midi + section_intervals[scale_idx]
        notes = _build_chord(chord_root_midi, section_qualities[scale_idx], voicing)
        cur_chord_freqs = [_midi_to_hz(n) for n in notes]
        # Arpeggio pool: chord tones one octave up, plus same set two octaves
        # up. Lets longer patterns walk into a higher register without
        # clashing with the held pad in the chord-tone register.
        cur_arp_freqs = ([_midi_to_hz(n + 12) for n in notes]
                         + [_midi_to_hz(n + 24) for n in notes])
        phases = [0.0 for _ in cur_chord_freqs]

    _refresh_chord()

    for f in range(n_frames):
        # Section rollover.
        if f >= sec_end and sec_idx + 1 < len(sections):
            sec_idx += 1
            cur_section = sections[sec_idx]
            (sec_start, sec_end, key_offset, mode_flip,
             prog_idx, voicing, _drum_idx, arp_idx) = cur_section
            section_is_minor = (not file_is_minor) if mode_flip else file_is_minor
            section_intervals = MINOR_INTERVALS if section_is_minor else MAJOR_INTERVALS
            section_qualities = MINOR_QUALITY if section_is_minor else MAJOR_QUALITY
            section_base_midi = file_base_midi + key_offset
            progression = PROGRESSIONS[prog_idx]
            arp_pattern = ARP_PATTERNS[arp_idx]
            # Restart the progression on the I chord at every section
            # boundary — sounds like a verse/chorus transition, lands the
            # listener firmly back on the tonic at the new key.
            chord_index = 0
            chord_sample_pos = 0
            arp_phase = 0.0
            arp_cur_idx = -1
            _refresh_chord()

        # Chord rollover within the section.
        if chord_sample_pos >= samples_per_chord:
            chord_index += 1
            chord_sample_pos = 0
            _refresh_chord()

        # Envelope (linear attack, sustained, linear release).
        if chord_sample_pos < env_samples:
            env = chord_sample_pos / env_samples
        elif chord_sample_pos > samples_per_chord - env_samples:
            env = max(0.0, (samples_per_chord - chord_sample_pos) / env_samples)
        else:
            env = 1.0

        vib = 1.0 + vib_depth * math.sin(two_pi_over_sr * vib_freq * f)

        # Held pad: sum chord-tone voices. Per-voice amplitude sized so
        # 3-voice constructive peaks reach ~70% of MUSIC_HEADROOM, leaving
        # ~30% headroom for the arpeggio voice on top before the soft-clip.
        n_voices = max(1, len(cur_chord_freqs))
        pad_amp_per_voice = (MUSIC_HEADROOM * 7 // 10) // n_voices
        sample_value = 0
        for i, base_freq in enumerate(cur_chord_freqs):
            freq = base_freq * vib
            phases[i] += two_pi_over_sr * freq
            sample_value += int(pad_amp_per_voice * env * math.sin(phases[i]))

        # Arpeggio voice: walks chord tones at 8th-note rate, providing
        # melodic motion *within* each held chord. Section-seeded pattern
        # (or `None` for ambient pad-only sections).
        if arp_pattern is not None and cur_arp_freqs:
            arp_pos_in_chord = chord_sample_pos
            pattern_step = (arp_pos_in_chord // samples_per_arp_note) % len(arp_pattern)
            new_arp_idx = arp_pattern[pattern_step] % len(cur_arp_freqs)
            if new_arp_idx != arp_cur_idx:
                arp_phase = 0.0  # crisp re-attack on each new note
                arp_cur_idx = new_arp_idx
            arp_freq = cur_arp_freqs[arp_cur_idx]
            arp_phase += two_pi_over_sr * arp_freq * vib
            # Per-note pluck envelope: instant attack, exponential decay over
            # the duration of one 8th note. Quieter than the pad so it sits
            # ON the chord, not over it.
            note_pos = arp_pos_in_chord % samples_per_arp_note
            arp_env = math.exp(-3.0 * note_pos / samples_per_arp_note)
            arp_amp = MUSIC_HEADROOM // 5   # ~20% of headroom
            sample_value += int(arp_amp * env * arp_env * math.sin(arp_phase))

        # Spawn any drum hits starting at this frame (with ±3 ms jitter
        # already baked in by the section planner above).
        starts_here = hits_by_start.get(f)
        if starts_here:
            active_hits.extend(starts_here)

        # Mix every active drum hit, advance offsets, drop finished hits.
        if active_hits:
            still_active: List[List] = []
            for hit in active_hits:
                off, wave = hit[0], hit[1]
                if off < len(wave):
                    sample_value += wave[off]
                    hit[0] = off + 1
                    still_active.append(hit)
            active_hits = still_active

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


def _build_synthesis_inputs(payload: bytes, n_frames: int):
    """Shared setup for all four encode entrypoints. Returns
    (key_index, tempo_bpm, sections, jitter_rng).

    `payload` is whatever bytes will be bit-packed (uM01 header + zlib
    compressed envelope for legacy, raw v3 envelope for v3). The seeds
    are derived from it so identical payloads produce identical music.
    """
    key_index, tempo_bpm = _seed_params(payload)
    samples_per_beat = max(1, round(SAMPLE_RATE * 60.0 / tempo_bpm))
    sections = _plan_sections(payload, n_frames, samples_per_beat)
    # Seed the jitter RNG from the payload hash. Same source → same
    # micro-timing pattern, deterministic across encodes.
    jitter_seed = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    jitter_rng = random.Random(jitter_seed)
    return key_index, tempo_bpm, sections, jitter_rng


def encode_music_payload(envelope: bytes) -> Tuple[bytes, int]:
    """LEGACY (uM01) audio Stone path. Compresses the envelope with zlib,
    prepends a 12-byte uM01 header, and bit-packs into LE PCM samples.

    Kept for decoder backward-compatibility with audio Stone files
    generated before MAGIC_V3_AUDIO shipped. NEW v3 audio Stone files
    use `encode_music_envelope` instead — no zlib (ciphertext is already
    incompressible) and the v3 envelope carries its own self-describing
    header.
    """
    compressed = zlib.compress(envelope, level=6)
    header = b"uM01" + struct.pack(">II", len(envelope), len(compressed))
    full_payload = header + compressed
    n_frames = _frames_for_bytes(len(full_payload))
    key_index, tempo_bpm, sections, jitter_rng = _build_synthesis_inputs(
        full_payload, n_frames)
    samples = _generate_music_samples(n_frames, key_index, tempo_bpm,
                                       sections, jitter_rng)
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
    n_frames = _frames_for_bytes(len(full_payload))
    key_index, tempo_bpm, sections, jitter_rng = _build_synthesis_inputs(
        full_payload, n_frames)
    samples = _generate_music_samples(n_frames, key_index, tempo_bpm,
                                       sections, jitter_rng)
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
    n_frames = _frames_for_bytes(len(envelope))
    key_index, tempo_bpm, sections, jitter_rng = _build_synthesis_inputs(
        envelope, n_frames)
    samples = _generate_music_samples(n_frames, key_index, tempo_bpm,
                                       sections, jitter_rng)
    embedded = _pack_payload_into_samples(samples, envelope)
    out = bytearray()
    for chunk in _samples_to_pcm_le16(embedded):
        out.extend(chunk)
    return bytes(out), n_frames


def encode_music_envelope_be(envelope: bytes) -> Tuple[bytes, int]:
    """NEW v3 BE-PCM variant for AIFF. See encode_music_envelope."""
    n_frames = _frames_for_bytes(len(envelope))
    key_index, tempo_bpm, sections, jitter_rng = _build_synthesis_inputs(
        envelope, n_frames)
    samples = _generate_music_samples(n_frames, key_index, tempo_bpm,
                                       sections, jitter_rng)
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
