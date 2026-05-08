"""Diagnose Vitriol signing setup without actually signing anything.

Run this AFTER dot-sourcing your .vitriol-sign-env.ps1 (or whatever
shell setup you use to populate the AzTS env vars). It reports:

  - Which env vars are set (the client secret is masked).
  - Whether signtool.exe was located.
  - Whether the Azure.CodeSigning.Dlib was located.

It does NOT make any network calls or attempt a real sign — that's
deliberate, so this can be run as a fast pre-flight check before
spending 5 minutes on a full PyInstaller build.

Exit code:
  0 = ready to sign (every check passed)
  1 = configuration incomplete (the printed reasons explain what)
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

# Make tools/ importable so we can reuse sign.py's discovery helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sign  # noqa: E402


def _mask(value: str) -> str:
    """Show a long secret as `fqx8…ya1r` so the user can confirm it's
    set without leaking the body. Short / unset values pass through
    unchanged so missing-env-var diagnostics stay readable."""
    if not value:
        return "(unset)"
    if len(value) < 12:
        return value
    return f"{value[:4]}…{value[-4:]} (len {len(value)})"


def main() -> int:
    print("Vitriol signing pre-flight check")
    print("=" * 64)

    problems: list[str] = []

    # --- Auth env vars ---
    print()
    print("App Registration auth:")
    auth = {
        "AZURE_CLIENT_ID":     os.environ.get("AZURE_CLIENT_ID", ""),
        "AZURE_TENANT_ID":     os.environ.get("AZURE_TENANT_ID", ""),
        "AZURE_CLIENT_SECRET": os.environ.get("AZURE_CLIENT_SECRET", ""),
    }
    for k, v in auth.items():
        if k == "AZURE_CLIENT_SECRET":
            shown = _mask(v)
        else:
            shown = v if v else "(unset)"
        marker = "  ok" if v else "  MISSING"
        print(f"  {k:24s} {shown}    [{marker.strip()}]")
        if not v:
            problems.append(f"{k} is unset")

    # --- AzTS resource env vars ---
    print()
    print("Trusted Signing resource:")
    resource = {
        "VITRIOL_AZTS_ENDPOINT": os.environ.get("VITRIOL_AZTS_ENDPOINT", ""),
        "VITRIOL_AZTS_ACCOUNT":  os.environ.get("VITRIOL_AZTS_ACCOUNT", ""),
        "VITRIOL_AZTS_PROFILE":  os.environ.get("VITRIOL_AZTS_PROFILE", ""),
    }
    for k, v in resource.items():
        shown = v if v else "(unset)"
        marker = "ok" if v else "MISSING"
        print(f"  {k:24s} {shown}    [{marker}]")
        if not v:
            problems.append(f"{k} is unset")

    # Sanity-check the endpoint shape — common typo is missing trailing
    # slash, or pasting the AzTS account-resource URL instead of the
    # signing endpoint.
    endpoint = resource["VITRIOL_AZTS_ENDPOINT"]
    if endpoint:
        if "<" in endpoint or "PASTE" in endpoint.upper():
            problems.append("VITRIOL_AZTS_ENDPOINT still contains a placeholder")
        elif "codesigning.azure.net" not in endpoint:
            problems.append(
                "VITRIOL_AZTS_ENDPOINT doesn't look like an AzTS endpoint "
                "(expected something containing codesigning.azure.net)"
            )

    # --- Tool discovery ---
    print()
    print("Tooling:")
    signtool = sign._find_signtool()
    if signtool:
        print(f"  signtool.exe          {signtool}    [ok]")
    else:
        print(f"  signtool.exe          (not found)    [MISSING]")
        problems.append("signtool.exe not found — install Windows SDK Signing Tools")

    dlib = sign._find_dlib()
    if dlib:
        print(f"  Azure.CodeSigning.Dlib  {dlib}    [ok]")
    else:
        print(f"  Azure.CodeSigning.Dlib  (not found)    [MISSING]")
        problems.append(
            "Azure.CodeSigning.Dlib.dll not found — install via "
            "`nuget install Azure.CodeSigning.Dlib -OutputDirectory "
            "$env:USERPROFILE\\.nuget\\packages`"
        )

    # --- Summary ---
    print()
    print("=" * 64)
    if problems:
        print(f"NOT READY — {len(problems)} issue(s):")
        for p in problems:
            print(f"  - {p}")
        print()
        print("Fix the items above, then re-run this script.")
        return 1

    print("READY — all env vars set, signtool + dlib located.")
    print()
    print("Next: run `python tools\\build_vitriol_dist.py` to build and")
    print("sign Vitriol.exe, then `python tools\\build_installer.py` to")
    print("build and sign the installer.")
    print()
    print("The first sign call will hit Azure to mint a fresh cert and")
    print("apply the signature. If auth is wrong, you'll see a clear")
    print("error from signtool — no half-signed file will result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
