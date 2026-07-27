"""Locate complete-bundle native tools without requiring user configuration."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def native_root() -> Path | None:
    """Return the explicit, frozen-bundle, or packaged native tool root."""
    configured = os.getenv("INVOICE_LAYOUT_NATIVE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "native"
    packaged = Path(__file__).resolve().parent / "native"
    return packaged if packaged.is_dir() else None


def configure_native_environment() -> None:
    """Prepend bundled command directories for libraries that probe PATH."""
    root = native_root()
    if root is None:
        return
    candidates = (root / "bin", root / "java" / "bin")
    existing = os.environ.get("PATH", "")
    prefixes = [str(path) for path in candidates if path.is_dir()]
    if prefixes:
        os.environ["PATH"] = os.pathsep.join([*prefixes, existing])


def java_executable() -> Path | None:
    """Find the configured, bundled, or system Java executable."""
    configured = os.getenv("INVOICE_LAYOUT_JAVA")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        return candidate if candidate.is_file() else None
    root = native_root()
    name = "java.exe" if os.name == "nt" else "java"
    if root is not None:
        candidate = root / "java" / "bin" / name
        if candidate.is_file():
            return candidate
    system = shutil.which("java")
    return Path(system) if system else None


def rar_extractor() -> Path | None:
    """Find a bundled or system extractor supported by rarfile."""
    names = (
        ("7zz.exe", "7z.exe", "unar.exe", "unrar.exe", "bsdtar.exe")
        if os.name == "nt"
        else ("7zz", "7z", "unar", "unrar", "bsdtar")
    )
    root = native_root()
    if root is not None:
        for name in names:
            candidate = root / "bin" / name
            if candidate.is_file():
                return candidate
    for name in names:
        system = shutil.which(name)
        if system:
            return Path(system)
    return None
