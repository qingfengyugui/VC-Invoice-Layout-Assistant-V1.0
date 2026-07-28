"""Build and stage the deterministic OFDRW renderer for wheel packaging."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

_MAVEN_ARGUMENTS = ["-q", "-DskipTests", "clean", "package"]


def _maven_command(
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Return an executable Maven command on Unix and Windows.

    Maven is installed as ``mvn.cmd`` on Windows. ``CreateProcess`` does not
    expand ``PATHEXT`` for Python argument-list subprocesses, so passing the
    exact wrapper path is required there.
    """
    executable_name = "mvn.cmd" if platform == "win32" else "mvn"
    executable = which(executable_name) or executable_name
    return [executable, *_MAVEN_ARGUMENTS]


def build_ofd_renderer(
    project_root: Path,
    *,
    runner: Callable[..., object] = subprocess.run,
    force: bool = False,
) -> Path:
    """Stage a packaged renderer, building it only when absent or forced."""
    renderer_dir = project_root / "tools" / "ofd-renderer"
    artifact = renderer_dir / "target" / "ofd-renderer.jar"
    packaged = project_root / "src" / "invoice_layout" / "bin" / "ofd-renderer.jar"

    if not force and packaged.is_file() and packaged.stat().st_size > 0:
        return packaged

    runner(_maven_command(), cwd=renderer_dir, check=True)
    if not artifact.is_file() or artifact.stat().st_size == 0:
        raise RuntimeError("Maven did not produce the OFDRW renderer JAR")
    packaged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(artifact, packaged)
    return packaged
