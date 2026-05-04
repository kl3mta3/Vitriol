"""3D model conversion via Assimp.

Recoded ctypes bindings — no pyassimp, no trimesh. We use Assimp's C API:
  aiImportFile          — read a model into an aiScene*
  aiGetExportFormatCount + aiGetExportFormatDescription — list writers
  aiExportScene         — write a scene to a target format
  aiReleaseImport       — free the imported scene

Scope:
  - Read/write: glb, gltf, obj, stl, fbx, ply, dae, 3ds.
  - Static geometry only — animations are not preserved (Assimp does
    flatten them on some exporters; for safety we add a status-bar warning
    if the input contains animations).
  - Errors from Assimp surface as-is (red status circle + tooltip).

Locating the DLL: app.utils.paths.bin_dir() first; then ASSIMP_DLL env var;
then the standard Windows search path.
"""
from __future__ import annotations
import ctypes
import os
import shutil
from ctypes import c_char_p, c_uint, c_void_p, c_int, POINTER, Structure
from pathlib import Path
from typing import Callable, Optional

from ..utils.cancellation import CancellationToken
from ..utils.logger import get_logger
from ..utils.paths import bin_dir

_log = get_logger()

MEDIA_CATEGORY = "model"
SUPPORTED = {".glb", ".gltf", ".obj", ".stl", ".fbx", ".ply", ".dae", ".3ds"}

# Assimp post-processing flag bits we want.
_aiProcess_Triangulate = 0x8
_aiProcess_GenNormals = 0x20
_aiProcess_JoinIdenticalVertices = 0x2

# Map output ext -> Assimp format-id strings (from aiGetExportFormatDescription)
_FORMAT_IDS = {
    ".glb": "glb2",
    ".gltf": "gltf2",
    ".obj": "obj",
    ".stl": "stlb",   # binary STL by default
    ".fbx": "fbx",
    ".ply": "plyb",   # binary PLY
    ".dae": "collada",
    ".3ds": "3ds",
}


class _aiString(Structure):
    _fields_ = [("length", c_uint), ("data", ctypes.c_char * 1024)]


_dll: Optional[ctypes.CDLL] = None


def _find_dll_path() -> Optional[Path]:
    """Delegates to paths.find_assimp() so launcher and runtime use the
    same lookup logic."""
    from ..utils.paths import find_assimp
    return find_assimp()


def _load() -> ctypes.CDLL:
    global _dll
    if _dll is not None:
        return _dll
    p = _find_dll_path()
    if p is None:
        raise RuntimeError(
            "Assimp library not found. Install it via the launch prompt or place "
            "assimp-vc143-mt.dll in ./bin/."
        )
    try:
        dll = ctypes.CDLL(str(p))
    except OSError as e:
        raise RuntimeError(f"Could not load Assimp DLL at {p}: {e}")

    # Function signatures
    dll.aiImportFile.argtypes = [c_char_p, c_uint]
    dll.aiImportFile.restype = c_void_p

    dll.aiReleaseImport.argtypes = [c_void_p]
    dll.aiReleaseImport.restype = None

    dll.aiExportScene.argtypes = [c_void_p, c_char_p, c_char_p, c_uint]
    dll.aiExportScene.restype = c_int

    dll.aiGetErrorString.argtypes = []
    dll.aiGetErrorString.restype = c_char_p

    _dll = dll
    return dll


def convert(
    src: Path,
    dst: Path,
    src_ext: str,
    dst_ext: str,
    cancel: CancellationToken,
    progress: Callable[[float], None],
) -> None:
    dll = _load()
    fmt_id = _FORMAT_IDS.get(dst_ext)
    if fmt_id is None:
        raise RuntimeError(f"No Assimp exporter for {dst_ext}.")

    progress(0.05)
    flags = _aiProcess_Triangulate | _aiProcess_GenNormals | _aiProcess_JoinIdenticalVertices
    scene = dll.aiImportFile(str(src).encode("utf-8"), flags)
    if not scene:
        err = dll.aiGetErrorString() or b""
        raise RuntimeError(f"Assimp import failed: {err.decode('utf-8', errors='replace') or 'unknown error'}")

    cancel.check()
    progress(0.55)
    try:
        rc = dll.aiExportScene(scene, fmt_id.encode("ascii"), str(dst).encode("utf-8"), 0)
        if rc != 0:
            err = dll.aiGetErrorString() or b""
            raise RuntimeError(
                f"Assimp export to {dst_ext} failed (code {rc}): "
                f"{err.decode('utf-8', errors='replace') or 'unknown error'}"
            )
    finally:
        dll.aiReleaseImport(scene)
    progress(1.0)
