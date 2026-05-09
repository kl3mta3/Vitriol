"""Structured-config conversions: json, yaml, toml, ini, jsonl, env.

Real semantic round-trips (not byte copies). Each format is parsed to a
Python value, kept on the TextDoc's metadata as `_config_data`, and emitted
in the target format. Writers in this module use that metadata when set;
falling back to a JSON-string round-trip otherwise.

Cross-category bridging: TextDoc.blocks is also populated with the
pretty-printed source text, so a `.json` → `.md` conversion through other
text writers gives a readable text dump instead of an empty document.

INI / ENV / JSONL flatten naturally:
  - INI       — top-level dict of section → dict of key → string
  - ENV       — top-level dict of key → string
  - JSONL     — top-level list of objects (one JSON value per line)

Loads alphabetically before `text_plain.py`, so it claims these extensions
in the registry's setdefault path.
"""
from __future__ import annotations
import configparser
import io
import json
from pathlib import Path
from typing import Any

from ..core.intermediate import Paragraph, Run, TextDoc
from ..utils.cancellation import CancellationToken
from . import charset

SUPPORTED_READ = {".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".env"}
SUPPORTED_WRITE = {".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".env"}
DOC_KIND = "text"  # bridges to other text writers via the textdoc fallback


def read(path: Path, ext: str, cancel: CancellationToken) -> TextDoc:
    raw = path.read_bytes()
    cancel.check()
    text, _ = charset.decode_with_encoding(raw)
    data = _parse(text, ext)
    return _wrap(data, text)


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if isinstance(doc, (bytes, bytearray)):
        # Binary fallback — write through.
        path.write_bytes(bytes(doc))
        return
    if isinstance(doc, TextDoc):
        data = doc.metadata.get("_config_data") if isinstance(doc.metadata, dict) else None
        if data is None:
            # Fallback: convert the textdoc's plain text to the target.
            from ..core.intermediate import textdoc_to_plain
            text = textdoc_to_plain(doc)
            data = _coerce_text_to_data(text)
    else:
        data = doc

    out = _emit(data, ext)
    path.write_text(out, encoding="utf-8")


# ---- parse ---------------------------------------------------------------

def _parse(text: str, ext: str) -> Any:
    if ext == ".json":
        return json.loads(text) if text.strip() else {}
    if ext == ".jsonl":
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
        return items
    if ext in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text) or {}
    if ext == ".toml":
        import tomllib
        return tomllib.loads(text)
    if ext == ".ini":
        cp = configparser.ConfigParser()
        cp.read_string(text)
        out: dict = {}
        if cp.defaults():
            out["DEFAULT"] = dict(cp.defaults())
        for section in cp.sections():
            out[section] = dict(cp.items(section))
        return out
    if ext == ".env":
        return _parse_env(text)
    raise RuntimeError(f"Unsupported config source: {ext}")


def _parse_env(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        # Unwrap optional surrounding quotes.
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


# ---- emit ----------------------------------------------------------------

def _emit(data: Any, ext: str) -> str:
    if ext == ".json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if ext == ".jsonl":
        items = data if isinstance(data, list) else _flatten_to_list(data)
        return "\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n"
    if ext in (".yaml", ".yml"):
        import yaml
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if ext == ".toml":
        import tomli_w
        coerced = _coerce_for_toml(data)
        return tomli_w.dumps(coerced)
    if ext == ".ini":
        return _emit_ini(data)
    if ext == ".env":
        return _emit_env(data)
    raise RuntimeError(f"Unsupported config target: {ext}")


def _emit_ini(data: Any) -> str:
    cp = configparser.ConfigParser()
    if not isinstance(data, dict):
        # Wrap scalar/list in a single section.
        cp["data"] = {"value": json.dumps(data)}
    else:
        # Scalars at the top level go under [DEFAULT].
        defaults = {}
        for k, v in data.items():
            if isinstance(v, dict):
                cp[str(k)] = {sk: _ini_scalar(sv) for sk, sv in v.items()}
            else:
                defaults[str(k)] = _ini_scalar(v)
        if defaults:
            cp["DEFAULT"] = defaults
    buf = io.StringIO()
    cp.write(buf)
    return buf.getvalue()


def _ini_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, str)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def _emit_env(data: Any) -> str:
    if not isinstance(data, dict):
        # Flatten lists into INDEXED_keys.
        if isinstance(data, list):
            data = {f"ITEM_{i}": v for i, v in enumerate(data)}
        else:
            data = {"VALUE": data}
    lines = []
    for k, v in data.items():
        key = str(k).upper().replace(".", "_").replace("-", "_")
        if isinstance(v, dict):
            # Flatten one level of nesting: section.key
            for sk, sv in v.items():
                full = f"{key}_{str(sk).upper().replace('.', '_').replace('-', '_')}"
                lines.append(f"{full}={_env_value(sv)}")
        else:
            lines.append(f"{key}={_env_value(v)}")
    return "\n".join(lines) + "\n"


def _env_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v) if not isinstance(v, str) else v
    needs_quote = any(c in s for c in (" ", "\t", "#", '"'))
    if needs_quote:
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'
    return s


def _coerce_for_toml(data: Any) -> dict:
    """TOML requires a top-level table. Wrap scalars / lists if needed and
    drop None values (TOML has no null type)."""
    if isinstance(data, dict):
        return _strip_none(data)
    if isinstance(data, list):
        return {"items": data}
    return {"value": data}


def _strip_none(d):
    if isinstance(d, dict):
        return {k: _strip_none(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_strip_none(x) for x in d if x is not None]
    return d


def _flatten_to_list(data) -> list:
    if isinstance(data, dict):
        return [{k: v} for k, v in data.items()]
    if isinstance(data, list):
        return data
    return [data]


def _coerce_text_to_data(text: str) -> Any:
    """When a non-config writer is used as the source for a config target,
    we get plain text. Try JSON first; fall back to a single string value."""
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"text": text}


def _wrap(data: Any, src_text: str) -> TextDoc:
    """Pack parsed data into a TextDoc — both as `_config_data` metadata for
    same-kind config writers, and as readable JSON paragraphs for cross-kind
    text writers (json → md/html/etc.)."""
    pretty = json.dumps(data, indent=2, ensure_ascii=False)
    blocks = [Paragraph(runs=[Run(text=pretty, code=True)])]
    doc = TextDoc(blocks=blocks)
    doc.metadata["_config_data"] = data
    return doc
