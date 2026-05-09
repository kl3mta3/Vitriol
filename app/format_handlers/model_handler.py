"""3D model conversion via Assimp's C API (ctypes bindings).

Read/write: glb, gltf, obj, stl, fbx, ply, dae, 3ds.

Animations: preserved when the user opts in via the pre-flight dialog AND
the target format supports them (.fbx, .dae, .glb, .gltf). Otherwise
stripped before export.

DLL lookup: app.utils.paths.bin_dir() → ASSIMP_DLL env var → Windows
search path.
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

# Target formats that can carry rigs/animations.
ANIMATION_CAPABLE_TARGETS = {".fbx", ".dae", ".glb", ".gltf"}

# Assimp post-processing flag bits.
_aiProcess_Triangulate = 0x8
_aiProcess_GenNormals = 0x20
_aiProcess_JoinIdenticalVertices = 0x2
_aiProcess_PopulateArmatureData = 0x4000

# Output ext → Assimp exporter format-id (from aiGetExportFormatDescription).
_FORMAT_IDS = {
    ".glb": "glb2",
    ".gltf": "gltf2",
    ".obj": "obj",
    ".stl": "stlb",
    ".fbx": "fbx",
    ".ply": "plyb",
    ".dae": "collada",
    ".3ds": "3ds",
}


class _aiString(Structure):
    _fields_ = [("length", c_uint), ("data", ctypes.c_char * 1024)]


# Partial mirror of Assimp's aiScene struct — leading fields through
# mAnimations. Used to read mNumAnimations and to zero it (strip path).
# Fields past mAnimations are not touched.
class _aiScene(Structure):
    _fields_ = [
        ("mFlags", c_uint),
        ("mRootNode", c_void_p),
        ("mNumMeshes", c_uint),
        ("mMeshes", c_void_p),
        ("mNumMaterials", c_uint),
        ("mMaterials", c_void_p),
        ("mNumAnimations", c_uint),
        ("mAnimations", c_void_p),
    ]


def can_carry_animations(dst_ext: str) -> bool:
    """True if the target format supports embedded rigs/animations."""
    return dst_ext.lower() in ANIMATION_CAPABLE_TARGETS


def has_animations(path: Path) -> bool:
    """Return True if the file has any animation tracks. Returns False
    on non-models, missing Assimp, or import error."""
    if path.suffix.lower() not in SUPPORTED:
        return False
    try:
        dll = _load()
    except RuntimeError:
        return False
    scene_ptr = dll.aiImportFile(str(path).encode("utf-8"), 0)
    if not scene_ptr:
        return False
    try:
        scene = ctypes.cast(scene_ptr, POINTER(_aiScene)).contents
        return scene.mNumAnimations > 0
    finally:
        dll.aiReleaseImport(scene_ptr)


def _strip_animations(scene_ptr) -> None:
    """Set aiScene.mNumAnimations = 0 so the exporter emits no animation
    tracks. Assimp still owns + frees the animation array."""
    scene = ctypes.cast(scene_ptr, POINTER(_aiScene)).contents
    scene.mNumAnimations = 0


_dll: Optional[ctypes.CDLL] = None


def _find_dll_path() -> Optional[Path]:
    """Delegate to paths.find_assimp()."""
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
    *,
    preserve_animations: bool = False,
) -> None:
    """Convert a 3D model via Assimp.

    `preserve_animations`: when True and the target format supports it,
    animation data is left intact for the exporter. Otherwise stripped
    before export.

    Known limitation: GLB/DAE → FBX loses BindPose/PoseNode chunks (mesh
    stays in T-pose, skeleton animates separately). FBX → GLB/DAE works.
    """
    dll = _load()
    fmt_id = _FORMAT_IDS.get(dst_ext)
    if fmt_id is None:
        raise RuntimeError(f"No Assimp exporter for {dst_ext}.")

    progress(0.05)
    flags = (_aiProcess_Triangulate
             | _aiProcess_GenNormals
             | _aiProcess_JoinIdenticalVertices
             | _aiProcess_PopulateArmatureData)
    scene = dll.aiImportFile(str(src).encode("utf-8"), flags)
    if not scene:
        err = dll.aiGetErrorString() or b""
        raise RuntimeError(f"Assimp import failed: {err.decode('utf-8', errors='replace') or 'unknown error'}")

    cancel.check()
    progress(0.55)
    try:
        # Strip animations unless caller opted in AND target supports them.
        if not (preserve_animations and can_carry_animations(dst_ext)):
            _strip_animations(scene)

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
