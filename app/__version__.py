"""Single source of truth for Vitriol's version string.

Used by:
  - The Inno Setup installer (`tools/build_installer.py` reads this and
    passes it to `iscc.exe` as a `/D` define).
  - Anywhere in the app that wants to display the version (About dialog,
    log preamble, etc.).

Bump this when you cut a new release.
"""
__version__ = "1.1.1"
