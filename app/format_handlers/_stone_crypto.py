"""Stone v3 envelope crypto: deterministic AES-256-CTR.

Why AES-CTR (not AES-GCM): the user spec forbids any wrong-password
oracle. AEAD modes provide an authentication tag whose check tells the
decoder "this password is wrong" with certainty, which is exactly the
oracle we cannot expose. CTR is the underlying stream cipher AEAD modes
are built on; using it raw means wrong password = garbage bytes, no
detection, no error.

Why deterministic IV (HMAC of the plaintext): we want
  same source + same password  ⇒  byte-identical PNG output
so the Mandelbrot fractal stays a unique-per-file piece of art. SIV-style
nonce derivation gives us this: the IV is a function of (key, plaintext),
so identical inputs reproduce identical ciphertexts. Different plaintexts
get different IVs even under the same key, so we don't reuse a CTR
keystream against multiple plaintexts (which would be a two-time-pad
catastrophe).

Why PBKDF2-200k: makes brute-forcing a weak password measurably
expensive (~50 ms per attempt). Empty password ("default key") still
passes through PBKDF2 — the resulting key is reproducible by anyone
with the source, which is fine because default-key files are the
"public" tier (anyone with Vitriol can decode, same as today).
"""
from __future__ import annotations
import hashlib
import hmac
from typing import Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend


_PBKDF2_ITER = 200_000
_PBKDF2_SALT = b"transmute-stone-v3"
_KEY_LEN = 32           # AES-256
_IV_LEN = 16            # AES block size
_SALT_FIELD_LEN = 4     # placeholder field in the v3 envelope header

# Module-level cache so we only PBKDF2 each unique password once per session.
# Keyed by SHA-256 of the password so we don't keep raw passwords in memory.
_KEY_CACHE: dict[bytes, bytes] = {}


def derive_key(password: bytes) -> bytes:
    """Derive a 32-byte AES-256 key from the password via PBKDF2-HMAC-SHA256.

    Empty password is allowed and produces a deterministic "default key"
    that is identical across all installs of the same Vitriol version.
    """
    if not isinstance(password, (bytes, bytearray)):
        raise TypeError("password must be bytes")
    fingerprint = hashlib.sha256(password).digest()
    cached = _KEY_CACHE.get(fingerprint)
    if cached is not None:
        return cached
    key = hashlib.pbkdf2_hmac(
        "sha256", bytes(password), _PBKDF2_SALT, _PBKDF2_ITER, dklen=_KEY_LEN)
    _KEY_CACHE[fingerprint] = key
    return key


def derive_iv(key: bytes, plaintext: bytes) -> bytes:
    """SIV-style deterministic IV. Same (key, plaintext) → same IV.
    Different plaintexts under the same key get different IVs."""
    return hmac.new(key, plaintext, "sha256").digest()[:_IV_LEN]


def encrypt(plaintext: bytes, password: bytes) -> Tuple[bytes, bytes]:
    """Encrypt plaintext with a password. Returns (iv, ciphertext).

    Both fields go into the envelope plaintext (iv must be reconstructible
    by the decoder). The plaintext itself is never exposed.
    """
    key = derive_key(password)
    iv = derive_iv(key, plaintext)
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend())
    enc = cipher.encryptor()
    return iv, enc.update(plaintext) + enc.finalize()


def decrypt(iv: bytes, ciphertext: bytes, password: bytes) -> bytes:
    """Decrypt with the password. NO authentication check — wrong password
    silently produces garbage of the same length as the ciphertext.

    There is no way for the caller to detect "wrong password" from this
    function's output. That is by design. Higher-level code must treat
    the returned bytes as authoritative regardless of correctness.
    """
    key = derive_key(password)
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend())
    dec = cipher.decryptor()
    return dec.update(ciphertext) + dec.finalize()


def clear_key_cache() -> None:
    """Forget all cached derived keys. Call this when the user toggles
    Stone off or otherwise wants in-memory passwords gone immediately."""
    _KEY_CACHE.clear()


# ---------------------------------------------------------------------------
# Streaming AES-CTR API
# ---------------------------------------------------------------------------
# AES-CTR is naturally streamable — each output block is independent of
# input position. The `cryptography` library exposes this via
# `encryptor.update(chunk)` / `decryptor.update(chunk)`. The module-level
# `encrypt()` / `decrypt()` above wrap this for one-shot use; the
# `StreamingEncryptor` / `StreamingDecryptor` below expose the chunked
# API for callers that handle multi-GB plaintexts and can't afford to
# hold the whole thing in RAM.
#
# IV derivation for streaming:
#   The one-shot path computes IV = HMAC(key, plaintext)[:16]. For
#   streaming we don't want to read the source twice solely for IV
#   computation, so callers should accumulate the same byte sequence
#   the one-shot would have hashed by feeding chunks to a `hmac.HMAC`
#   incrementally before constructing the encryptor. See
#   `derive_iv_streaming` for the helper.

class StreamingEncryptor:
    """Chunked AES-CTR encrypt. Caller feeds plaintext via `update(chunk)`
    and receives ciphertext chunks of the same length. Wraps the native
    `cryptography` chunked API. Single-threaded (cipher state is
    sequential)."""
    __slots__ = ("_enc",)

    def __init__(self, key: bytes, iv: bytes) -> None:
        cipher = Cipher(algorithms.AES(key), modes.CTR(iv),
                         backend=default_backend())
        self._enc = cipher.encryptor()

    def update(self, data: bytes) -> bytes:
        return self._enc.update(data)

    def finalize(self) -> bytes:
        return self._enc.finalize()


class StreamingDecryptor:
    """Chunked AES-CTR decrypt. Same API as `StreamingEncryptor`. NO
    authentication check (matches the no-oracle invariant of `decrypt`)."""
    __slots__ = ("_dec",)

    def __init__(self, key: bytes, iv: bytes) -> None:
        cipher = Cipher(algorithms.AES(key), modes.CTR(iv),
                         backend=default_backend())
        self._dec = cipher.decryptor()

    def update(self, data: bytes) -> bytes:
        return self._dec.update(data)

    def finalize(self) -> bytes:
        return self._dec.finalize()


class StreamingIVHasher:
    """Incremental HMAC-SHA256 helper that produces the SAME IV
    `derive_iv(key, plaintext)` would, given the same byte stream fed
    via `update()`. Caller is responsible for feeding the inner-plaintext
    bytes in the same order the one-shot path would produce them.

    Usage:
        h = StreamingIVHasher(key)
        h.update(inner_header)        # ext_len + ext + payload_len
        for chunk in source_chunks:
            h.update(chunk)
        iv = h.iv()
    """
    __slots__ = ("_h",)

    def __init__(self, key: bytes) -> None:
        self._h = hmac.new(key, b"", "sha256")

    def update(self, data: bytes) -> None:
        self._h.update(data)

    def iv(self) -> bytes:
        return self._h.digest()[:_IV_LEN]
