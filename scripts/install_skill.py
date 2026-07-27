"""Install one portable Skill adapter into an explicit platform skill root."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLATFORMS_ROOT = REPOSITORY_ROOT / "platforms"
SKILL_NAME = "invoice-layout-agent"


def _compatibility() -> dict[str, Any]:
    metadata = json.loads(
        (PLATFORMS_ROOT / "compatibility.json").read_text(encoding="utf-8")
    )
    if not isinstance(metadata, dict):
        raise TypeError("platform compatibility metadata must be an object")
    return metadata


def install_skill(
    platform: str,
    destination_root: Path,
    *,
    force: bool = False,
    runtime_command: str = "invoice-layout",
) -> Path:
    """Copy the selected adapter without installing dependencies or editing MCP config."""
    compatibility = _compatibility()
    if platform not in compatibility:
        supported = ", ".join(sorted(compatibility))
        raise ValueError(f"unsupported platform {platform!r}; choose one of: {supported}")

    source = PLATFORMS_ROOT / platform / SKILL_NAME
    destination_root = destination_root.expanduser().resolve()
    destination = destination_root / SKILL_NAME
    if destination.exists() or destination.is_symlink():
        if not force:
            raise FileExistsError(f"destination already exists: {destination}")
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()

    destination_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    (destination / "RUNTIME.md").write_text(
        f"Use this runtime executable for every command:\n\n`{runtime_command}`\n",
        encoding="utf-8",
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=sorted(_compatibility()))
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--runtime", default="invoice-layout")
    args = parser.parse_args()

    installed = install_skill(
        args.platform,
        args.destination,
        force=args.force,
        runtime_command=args.runtime,
    )
    command = _compatibility()[args.platform]["invocation"]["command"]
    print(f"Installed Skill: {installed}")
    print(f"Optional follow-up command: {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
