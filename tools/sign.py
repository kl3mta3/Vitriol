"""Azure Trusted Signing helper for Vitriol build scripts.

Signs a Windows PE file (Vitriol.exe, VitriolSetup-*.exe) via the
Microsoft `signtool` toolchain plus the `Azure.CodeSigning.Dlib`
plugin. Both build_vitriol_dist.py and build_installer.py call
`sign_file(path)` at the right point, so the same configuration drives
both stages.

Configuration via environment variables — set them once in your shell
and both build scripts pick them up:

  VITRIOL_AZTS_ENDPOINT    e.g. https://eus.codesigning.azure.net/
  VITRIOL_AZTS_ACCOUNT     your Trusted Signing account name
  VITRIOL_AZTS_PROFILE     certificate profile name within the account

Optional:

  VITRIOL_AZTS_DLIB        full path to Azure.CodeSigning.Dlib.dll
                           (defaults to common install locations + PATH)
  VITRIOL_AZTS_TS_URL      RFC 3161 timestamp URL
                           (default: http://timestamp.acs.microsoft.com)
  VITRIOL_SIGNTOOL         full path to signtool.exe
                           (defaults to common Windows SDK locations)
  VITRIOL_SIGN_DESCRIPTION /d description embedded in the signature
                           (default: "Vitriol — file converter")
  VITRIOL_SIGN_URL         /du info URL embedded in the signature
                           (default: https://github.com/<you>/Vitriol)

Authentication: signtool inherits Azure auth from one of:
  - `az login` interactive session (common for local dev),
  - service principal env vars (AZURE_CLIENT_ID / AZURE_TENANT_ID /
    AZURE_CLIENT_SECRET — common in CI),
  - managed identity (when running on an Azure VM / GitHub Actions
    runner with AzTS federation set up).

If the AzTS env vars are missing, `sign_file()` prints a clear warning
and returns False without raising, so dev / first-time builders who
don't have AzTS access still get an unsigned artifact rather than a
failed build.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_TIMESTAMP_URL = "http://timestamp.acs.microsoft.com"
DEFAULT_DESCRIPTION = "Vitriol — file converter"
DEFAULT_INFO_URL = "https://github.com/kl3mta3/Vitriol"


# Common signtool.exe locations across Windows SDK versions. The
# build host might have any subset; we try them in order and fall
# back to PATH lookup.
_SIGNTOOL_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe"),
    Path(r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.22000.0\x64\signtool.exe"),
    Path(r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.20348.0\x64\signtool.exe"),
    Path(r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.19041.0\x64\signtool.exe"),
    Path(r"C:\Program Files (x86)\Windows Kits\10\bin\x64\signtool.exe"),
    Path(r"C:\Program Files\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe"),
    Path(r"C:\Program Files\Windows Kits\10\bin\10.0.22000.0\x64\signtool.exe"),
]

# Common Azure.CodeSigning.Dlib.dll locations. The dlib ships as a
# NuGet package — users typically extract it to a tools folder under
# their user profile. Microsoft's own samples drop it under
# %USERPROFILE%\.nuget\packages\ once installed via `nuget install`.
_DLIB_CANDIDATES = [
    Path.home() / "AppData" / "Local" / "Microsoft" / "MicrosoftCodeSigning" / "Azure.CodeSigning.Dlib" / "bin" / "x64" / "Azure.CodeSigning.Dlib.dll",
    Path.home() / ".nuget" / "packages" / "azure.codesigning.dlib" / "bin" / "x64" / "Azure.CodeSigning.Dlib.dll",
    Path(r"C:\Program Files\Microsoft\Azure.CodeSigning.Dlib\bin\x64\Azure.CodeSigning.Dlib.dll"),
]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _find_signtool() -> Optional[Path]:
    """Locate signtool.exe. Honors VITRIOL_SIGNTOOL env override."""
    env = os.environ.get("VITRIOL_SIGNTOOL")
    if env:
        p = Path(env)
        return p if p.exists() else None
    for cand in _SIGNTOOL_CANDIDATES:
        if cand.exists():
            return cand
    on_path = shutil.which("signtool")
    return Path(on_path) if on_path else None


def _find_dlib() -> Optional[Path]:
    """Locate Azure.CodeSigning.Dlib.dll. Honors VITRIOL_AZTS_DLIB env override.

    The dlib is a wildcard-version-named NuGet package, so the
    `.nuget/packages/azure.codesigning.dlib/` folder usually has a
    versioned subdir between the package name and `bin/`. We glob for it
    so we don't have to update this file every time the user installs a
    new dlib version.
    """
    env = os.environ.get("VITRIOL_AZTS_DLIB")
    if env:
        p = Path(env)
        return p if p.exists() else None

    # Direct hits.
    for cand in _DLIB_CANDIDATES:
        if cand.exists():
            return cand

    # NuGet versioned-folder glob fallback.
    nuget = Path.home() / ".nuget" / "packages" / "azure.codesigning.dlib"
    if nuget.exists():
        # Sort descending so the newest version wins.
        for ver in sorted(nuget.iterdir(), reverse=True):
            dll = ver / "bin" / "x64" / "Azure.CodeSigning.Dlib.dll"
            if dll.exists():
                return dll
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    """True iff the three required AzTS env vars are set.

    Doesn't check whether signtool / dlib are findable — those are
    surfaced as errors at sign time. The point of this is to let
    build scripts skip signing entirely when the user clearly hasn't
    set up AzTS yet (rather than failing partway through)."""
    return bool(
        os.environ.get("VITRIOL_AZTS_ENDPOINT")
        and os.environ.get("VITRIOL_AZTS_ACCOUNT")
        and os.environ.get("VITRIOL_AZTS_PROFILE")
    )


def sign_file(path: Path) -> bool:
    """Sign `path` in place using Azure Trusted Signing.

    Returns True on success, False on any failure with a clear message
    printed to stderr. Never raises — a missing AzTS configuration or
    a signtool failure should NOT abort the build, just produce an
    unsigned artifact (which the build script then warns about).

    The signed file's bytes change (Authenticode block is appended),
    so any SHA-256 you computed before signing is now stale.
    """
    path = Path(path)
    if not path.exists():
        print(f"sign: {path} does not exist", file=sys.stderr)
        return False

    if not is_configured():
        print()
        print("=" * 64)
        print("Skipping code signing: AzTS not configured.")
        print()
        print("To enable signing, set these environment variables:")
        print("  VITRIOL_AZTS_ENDPOINT   e.g. https://eus.codesigning.azure.net/")
        print("  VITRIOL_AZTS_ACCOUNT    your Trusted Signing account name")
        print("  VITRIOL_AZTS_PROFILE    certificate profile name")
        print()
        print("Build will continue without a signature.")
        print("=" * 64)
        return False

    signtool = _find_signtool()
    if signtool is None:
        print()
        print("=" * 64)
        print("ERROR: signtool.exe not found.", file=sys.stderr)
        print()
        print("Install the Windows SDK (Signing Tools component) from:",
              file=sys.stderr)
        print("  https://developer.microsoft.com/windows/downloads/windows-sdk/",
              file=sys.stderr)
        print()
        print("Or set VITRIOL_SIGNTOOL to a full path.", file=sys.stderr)
        print("=" * 64)
        return False

    dlib = _find_dlib()
    if dlib is None:
        print()
        print("=" * 64)
        print("ERROR: Azure.CodeSigning.Dlib.dll not found.", file=sys.stderr)
        print()
        print("Install the dlib via NuGet:", file=sys.stderr)
        print("  nuget install Azure.CodeSigning.Dlib -Version <latest> -x \\",
              file=sys.stderr)
        print("    -OutputDirectory %USERPROFILE%\\.nuget\\packages",
              file=sys.stderr)
        print()
        print("Or set VITRIOL_AZTS_DLIB to the full path of the .dll.",
              file=sys.stderr)
        print("=" * 64)
        return False

    endpoint = os.environ["VITRIOL_AZTS_ENDPOINT"]
    account = os.environ["VITRIOL_AZTS_ACCOUNT"]
    profile = os.environ["VITRIOL_AZTS_PROFILE"]
    ts_url = os.environ.get("VITRIOL_AZTS_TS_URL", DEFAULT_TIMESTAMP_URL)
    description = os.environ.get("VITRIOL_SIGN_DESCRIPTION", DEFAULT_DESCRIPTION)
    info_url = os.environ.get("VITRIOL_SIGN_URL", DEFAULT_INFO_URL)

    # The dlib expects its config in a JSON file referenced by /dmdf.
    # Generate it on the fly in a temp dir so the env vars stay the
    # single source of truth — no metadata.json sitting on disk that
    # could drift out of sync.
    with tempfile.TemporaryDirectory() as td:
        meta = Path(td) / "metadata.json"
        meta.write_text(
            json.dumps({
                "Endpoint": endpoint,
                "CodeSigningAccountName": account,
                "CertificateProfileName": profile,
            }, indent=2),
            encoding="utf-8",
        )

        cmd = [
            str(signtool), "sign",
            "/v",                              # verbose: surface auth errors
            "/fd", "SHA256",                   # file digest algorithm
            "/tr", ts_url,                     # RFC 3161 timestamp server
            "/td", "SHA256",                   # timestamp digest algorithm
            "/dlib", str(dlib),                # AzTS plugin
            "/dmdf", str(meta),                # plugin's metadata.json
            "/d", description,                 # signature description
            "/du", info_url,                   # info URL
            str(path),
        ]
        print(f"\nSigning {path.name} via Azure Trusted Signing...")
        print(f"  endpoint: {endpoint}")
        print(f"  account:  {account}")
        print(f"  profile:  {profile}")
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"signtool failed with exit code {rc}", file=sys.stderr)
            print("Build continues with the unsigned artifact.", file=sys.stderr)
            return False

    # Verify the signature attached cleanly. /pa = use the default
    # authentication policy (any trusted root). Catches the case where
    # signtool returned 0 but produced a signature that won't validate
    # at runtime on a standard Windows install.
    verify_cmd = [str(signtool), "verify", "/pa", "/v", str(path)]
    print(f"\nVerifying signature on {path.name}...")
    rc = subprocess.call(verify_cmd)
    if rc != 0:
        print(f"signature verify failed with exit code {rc}", file=sys.stderr)
        return False

    print(f"\nSigned: {path}")
    return True
