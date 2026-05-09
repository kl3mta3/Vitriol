"""X.509 cert / private-key conversions: PEM ↔ CRT (DER cert) ↔ KEY (DER key).

Supported pairs:
  pem ↔ crt      (X.509 certificates: PEM ↔ DER)
  pem ↔ key      (private keys: PEM ↔ DER, PKCS#8)
  pem ↔ pem      (passthrough — re-serializes through the parser, normalizing)
  crt → pem      (DER → PEM cert)
  key → pem      (DER → PEM key, PKCS#8)

A `.pem` source is sniffed for its first BEGIN line to choose between cert
and key paths. A bundle (cert + key in the same .pem) writes the cert when
exporting to .crt, the key when exporting to .key.

Public-key-only files are detected and routed similarly. Encrypted private
keys are not supported (no password prompt UX in v1) — they will surface a
clear error from cryptography.

DOC_KIND="crypto" keeps these in their own group.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..utils.cancellation import CancellationToken

SUPPORTED_READ = {".pem", ".crt", ".cer", ".key", ".der"}
SUPPORTED_WRITE = {".pem", ".crt", ".cer", ".key", ".der"}
DOC_KIND = "crypto"


@dataclass
class CryptoDoc:
    """Parsed crypto material: zero or more certs + zero or one private key.

    Stored as DER bytes — re-serializing to PEM is trivial; going the other
    way (PEM input) is what the parser does at read time.
    """
    certs_der: List[bytes] = field(default_factory=list)
    key_der: Optional[bytes] = None  # PKCS#8 DER for private keys
    public_key_der: Optional[bytes] = None  # SPKI DER for standalone pubkeys


def read(path: Path, ext: str, cancel: CancellationToken) -> CryptoDoc:
    raw = path.read_bytes()
    cancel.check()
    # PEM (text-armored) sources can be sniffed by content; .crt/.cer/.key
    # may be either PEM or DER. We try PEM first then DER.
    if _looks_like_pem(raw):
        return _parse_pem(raw)
    return _parse_der(raw, ext)


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, CryptoDoc):
        raise RuntimeError("Crypto writer requires a CryptoDoc.")
    if ext == ".pem":
        path.write_bytes(_emit_pem(doc))
        return
    if ext in (".crt", ".cer"):
        if not doc.certs_der:
            raise RuntimeError(
                "Cannot write .crt/.cer: source contains no X.509 certificate."
            )
        # DER cert files hold ONE cert. Write the first.
        path.write_bytes(doc.certs_der[0])
        return
    if ext == ".key":
        if doc.key_der is None:
            raise RuntimeError(
                "Cannot write .key: source contains no private key."
            )
        # Convention: bare .key files are also DER unless the user expects PEM.
        # We write DER PKCS#8 here. For PEM keys, the user picks .pem.
        path.write_bytes(doc.key_der)
        return
    if ext == ".der":
        # Pick the most useful single object: cert > key > pubkey.
        if doc.certs_der:
            path.write_bytes(doc.certs_der[0])
            return
        if doc.key_der is not None:
            path.write_bytes(doc.key_der)
            return
        if doc.public_key_der is not None:
            path.write_bytes(doc.public_key_der)
            return
        raise RuntimeError("Empty CryptoDoc — nothing to write as .der.")
    raise RuntimeError(f"Unsupported crypto target: {ext}")


# ---- PEM -----------------------------------------------------------------

_PEM_BLOCK_RE = re.compile(
    rb"-----BEGIN ([A-Z0-9 ]+)-----\s*([A-Za-z0-9+/=\s]+?)-----END \1-----",
    re.DOTALL,
)


def _looks_like_pem(raw: bytes) -> bool:
    return b"-----BEGIN " in raw[:2048]


def _parse_pem(raw: bytes) -> CryptoDoc:
    from cryptography.hazmat.primitives import serialization
    from cryptography import x509

    out = CryptoDoc()
    for m in _PEM_BLOCK_RE.finditer(raw):
        label = m.group(1).decode("ascii").strip()
        full_block = m.group(0)
        if label.endswith("CERTIFICATE") and "REQUEST" not in label:
            cert = x509.load_pem_x509_certificate(full_block)
            out.certs_der.append(
                cert.public_bytes(serialization.Encoding.DER)
            )
        elif "PRIVATE KEY" in label:
            key = serialization.load_pem_private_key(full_block, password=None)
            out.key_der = key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        elif "PUBLIC KEY" in label:
            pub = serialization.load_pem_public_key(full_block)
            out.public_key_der = pub.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        # Other labels (DH PARAMETERS, EC PARAMETERS, etc.) are silently
        # dropped — we don't claim full PEM coverage here.
    if not out.certs_der and out.key_der is None and out.public_key_der is None:
        raise RuntimeError(
            "PEM source contained no recognized cert / private key / public key blocks."
        )
    return out


def _emit_pem(doc: CryptoDoc) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography import x509

    parts: List[bytes] = []
    for der in doc.certs_der:
        cert = x509.load_der_x509_certificate(der)
        parts.append(cert.public_bytes(serialization.Encoding.PEM))
    if doc.key_der is not None:
        key = serialization.load_der_private_key(doc.key_der, password=None)
        parts.append(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    if doc.public_key_der is not None:
        pub = serialization.load_der_public_key(doc.public_key_der)
        parts.append(pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
    if not parts:
        raise RuntimeError("Empty CryptoDoc — nothing to emit as PEM.")
    return b"".join(parts)


# ---- DER -----------------------------------------------------------------

def _parse_der(raw: bytes, ext: str) -> CryptoDoc:
    from cryptography.hazmat.primitives import serialization
    from cryptography import x509

    out = CryptoDoc()
    # Try the most likely format first based on extension, then fall back.
    if ext in (".crt", ".cer", ".der"):
        try:
            cert = x509.load_der_x509_certificate(raw)
            out.certs_der.append(
                cert.public_bytes(serialization.Encoding.DER)
            )
            return out
        except Exception:
            pass
    if ext in (".key", ".der"):
        try:
            key = serialization.load_der_private_key(raw, password=None)
            out.key_der = key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            return out
        except Exception:
            pass
    # Last-ditch: try public key.
    try:
        pub = serialization.load_der_public_key(raw)
        out.public_key_der = pub.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return out
    except Exception:
        pass
    # Try cert / key fallbacks not gated on the extension.
    try:
        cert = x509.load_der_x509_certificate(raw)
        out.certs_der.append(cert.public_bytes(serialization.Encoding.DER))
        return out
    except Exception:
        pass
    try:
        key = serialization.load_der_private_key(raw, password=None)
        out.key_der = key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return out
    except Exception as e:
        raise RuntimeError(
            f"Could not parse {ext} as DER cert / private key / public key. "
            f"If this is encrypted, decrypt it first. ({e})"
        ) from e
