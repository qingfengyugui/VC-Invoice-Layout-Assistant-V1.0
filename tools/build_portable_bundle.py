"""Assemble a self-contained platform runtime around a frozen executable."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

PLATFORMS = ("codex", "claude-code", "openclaw", "workbuddy", "qoder", "qclaw")
JAVA_MODULES = "java.base,java.desktop,java.management,java.naming,java.sql"


def build_portable_bundle(
    project_root: Path,
    *,
    executable: Path,
    java_home: Path,
    seven_zip: Path,
    output: Path,
    seven_zip_license: Path | None = None,
    runner: Callable[..., object] = subprocess.run,
) -> Path:
    """Build one complete runtime bundle without touching system configuration."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"bundle destination already exists: {output}")
    if not executable.is_file():
        raise FileNotFoundError(f"standalone executable is missing: {executable}")
    if not seven_zip.is_file():
        raise FileNotFoundError(f"7-Zip executable is missing: {seven_zip}")
    jlink = java_home / "bin" / "jlink"
    if not jlink.is_file():
        jlink = jlink.with_suffix(".exe")
    if not jlink.is_file():
        raise FileNotFoundError(f"jlink is missing under: {java_home}")

    output.mkdir(parents=True)
    shutil.copy2(executable, output / executable.name)
    native_bin = output / "native" / "bin"
    native_bin.mkdir(parents=True)
    shutil.copy2(seven_zip, native_bin / seven_zip.name)
    for sibling_name in ("7z.dll", "7zz.dll"):
        sibling = seven_zip.with_name(sibling_name)
        if sibling.is_file():
            shutil.copy2(sibling, native_bin / sibling.name)
    if seven_zip_license is not None:
        if not seven_zip_license.is_file():
            raise FileNotFoundError(
                f"7-Zip license notice is missing: {seven_zip_license}"
            )
        license_dir = output / "licenses" / "7zip"
        license_dir.mkdir(parents=True)
        shutil.copy2(seven_zip_license, license_dir / seven_zip_license.name)

    java_output = output / "native" / "java"
    runner(
        [
            str(jlink),
            "--add-modules",
            JAVA_MODULES,
            "--strip-debug",
            "--no-header-files",
            "--no-man-pages",
            "--output",
            str(java_output),
        ],
        check=True,
    )

    for platform in PLATFORMS:
        source = project_root / "platforms" / platform / "invoice-layout-agent"
        shutil.copytree(
            source,
            output / "platforms" / platform / "invoice-layout-agent",
        )
    shutil.copy2(project_root / "platforms" / "compatibility.json", output / "platforms")
    shutil.copy2(project_root / "LICENSE", output / "LICENSE")
    shutil.copy2(
        project_root / "THIRD_PARTY_NOTICES.md",
        output / "THIRD_PARTY_NOTICES.md",
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--seven-zip", type=Path, required=True)
    parser.add_argument("--seven-zip-license", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_portable_bundle(
        args.project_root.resolve(),
        executable=args.executable.resolve(),
        java_home=args.java_home.resolve(),
        seven_zip=args.seven_zip.resolve(),
        seven_zip_license=(
            args.seven_zip_license.resolve() if args.seven_zip_license else None
        ),
        output=args.output.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
