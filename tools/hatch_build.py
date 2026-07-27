"""Hatch hook that stages the OFDRW renderer before building a wheel."""

# mypy: disable-error-code=import-not-found

from __future__ import annotations

import importlib.util
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import (
    BuildHookInterface,  # type: ignore[import-not-found]
)


class CustomBuildHook(BuildHookInterface):
    """Build the Java renderer only for the wheel that ships it."""

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if self.target_name != "wheel":
            return
        project_root = Path(self.root)
        specification = importlib.util.spec_from_file_location(
            "invoice_layout_build_ofd_renderer",
            project_root / "tools" / "build_ofd_renderer.py",
        )
        if specification is None or specification.loader is None:
            raise RuntimeError("unable to load OFDRW build helper")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        module.build_ofd_renderer(project_root)
