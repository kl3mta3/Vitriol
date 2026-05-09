"""3D model conversion via Assimp.

Recoded ctypes bindings — no pyassimp, no trimesh. We use Assimp's C API:
  aiImportFile          — read a model into an aiScene*
  aiGetExportFormatCount + aiGetExportFormatDescription — list writers
  aiExportScene         — write a scene to a target format
  aiReleaseImport       — free the imported scene

Scope:
  - Read/write: glb, gltf, obj, stl, fbx, ply, dae, 3ds.
  - Geometry always preserved. Animations preserved on a per-conversion
    basis when the user opts in via the pre-flight dialog AND the target
    format can carry them (.fbx / .dae / .glb / .gltf). On opt-out (or
    formats that physically can't carry animations like .obj / .stl /
    .ply / .3ds), animations are stripped before export so the output
    is deterministically static-only.
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

# Target formats whose specs CAN carry rigs/animations. Other targets
# (.obj, .stl, .ply, .3ds) are geometry-only by spec — no point asking
# the user about animation preservation since the file format can't
# carry the data either way.
ANIMATION_CAPABLE_TARGETS = {".fbx", ".dae", ".glb", ".gltf"}

# Assimp post-processing flag bits we want.
_aiProcess_Triangulate = 0x8
_aiProcess_GenNormals = 0x20
_aiProcess_JoinIdenticalVertices = 0x2
# `aiProcess_PopulateArmatureData` (0x4000) populates `aiNode.mArmature`
# pointers on import. Some Assimp exporters use these to emit proper
# bind-pose data; the FBX exporter unfortunately does NOT (see comment
# in `convert()` about the GLB → FBX skinning limitation). We pass it
# anyway because it's harmless and helps the future-version case
# where the FBX exporter starts honoring it, plus other targets like
# .dae use the armature data correctly today.
_aiProcess_PopulateArmatureData = 0x4000

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


# Partial mirror of Assimp's aiScene struct — only the leading fields up
# through mAnimations. ctypes computes natural alignment, which matches
# Assimp's C layout on x86_64 Windows. We use this to:
#   - Read mNumAnimations (cheap detection of "does this file have rigs?")
#   - Zero mNumAnimations to tell the exporter to skip animation tracks
#     (used for the "drop animations" path).
# We do NOT touch fields past mAnimations — Assimp expects the struct to
# remain valid for aiReleaseImport, and we never reallocate it.
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
    """True if the target file format spec supports embedded rigs/animations."""
    return dst_ext.lower() in ANIMATION_CAPABLE_TARGETS


def has_animations(path: Path) -> bool:
    """Cheap detection: does this 3D file contain any animation tracks?

    Opens the file via Assimp with no post-processing (fastest path),
    reads aiScene.mNumAnimations, and releases. Returns False if the
    file isn't a 3D model, Assimp isn't loaded, or the import failed.
    Cost: usually 50-500 ms depending on file size.
    """
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
    """Set aiScene.mNumAnimations = 0 so the exporter skips animation
    tracks. The animation array itself stays allocated (Assimp owns it
    and frees on aiReleaseImport); we just tell the exporter there are
    zero entries. Bone references inside individual meshes are left
    untouched — exporters that respect mNumAnimations=0 typically also
    drop or static-pose the bone references on their own."""
    scene = ctypes.cast(scene_ptr, POINTER(_aiScene)).contents
    scene.mNumAnimations = 0


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
    *,
    preserve_animations: bool = False,
) -> None:
    """Convert a 3D model via Assimp.

    `preserve_animations` (default False): when True, the loaded scene's
    animation data is left intact, so Assimp's exporter has the
    opportunity to embed it in the output (best-effort — actual
    preservation depends on the format pair). When False, animations
    are explicitly stripped before export so the output is
    deterministically static-only.

    The caller (router → main_window pre-flight) decides which mode to
    pass based on a one-time user prompt when the source has animations
    AND the target format can carry them. Targets that physically can't
    carry animations (.obj, .stl, .ply, .3ds) ignore this flag — strip
    happens regardless because the format can't represent the data.
    """
    dll = _load()
    fmt_id = _FORMAT_IDS.get(dst_ext)
    if fmt_id is None:
        raise RuntimeError(f"No Assimp exporter for {dst_ext}.")

    progress(0.05)
    # Standard geometry-cleanup flags. PopulateArmatureData is included
    # for skinned-mesh round-trip support — it's mandatory for some
    # exporters (.dae) and harmless for others. Notably it does NOT
    # fully fix GLB → FBX skinning round-trips: Assimp's FBX exporter
    # has a long-standing limitation where it doesn't emit `BindPose`
    # or `PoseNode` chunks when the source came from glTF. The exported
    # FBX is structurally valid and includes Skin / Cluster nodes for
    # bone-to-vertex bindings, but tools that load it find no rest-pose
    # data and end up with the mesh stuck in T-pose while the skeleton
    # animates separately — the "hopping in T-pose" symptom. There is
    # no Assimp flag that fixes this; a real fix would require either
    # patching Assimp's FBX exporter or post-processing the output FBX
    # to inject BindPose nodes, both out of scope for this round.
    # FBX → GLB direction works correctly; only the reverse direction
    # is affected.
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
        # Strip animations unless: user opted in AND the target format can
        # actually carry them. Belt-and-suspenders gating — even if the
        # caller forgets to consider can_carry_animations, we'll still do
        # the right thing for geometry-only target formats.
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
