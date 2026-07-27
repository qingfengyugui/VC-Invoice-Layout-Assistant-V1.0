from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.install_skill import install_skill

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("codex", "claude-code", "openclaw", "workbuddy", "qoder", "qclaw")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_installer_copies_only_selected_skill(platform: str, tmp_path: Path) -> None:
    installed = install_skill(platform, tmp_path)

    assert installed == tmp_path / "invoice-layout-agent"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "agents" / "openai.yaml").is_file()
    assert (installed / "RUNTIME.md").read_text("utf-8").endswith(
        "`invoice-layout`\n"
    )
    assert not (installed / "src").exists()


def test_installer_rejects_unknown_platform(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        install_skill("unknown", tmp_path)


def test_installer_refuses_collision_unless_force_is_explicit(tmp_path: Path) -> None:
    installed = install_skill("codex", tmp_path)
    marker = installed / "local.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        install_skill("codex", tmp_path)
    assert marker.is_file()

    replaced = install_skill("codex", tmp_path, force=True)
    assert replaced == installed
    assert not marker.exists()


def test_cli_prints_follow_up_without_installing_dependencies(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/install_skill.py",
            "--platform",
            "codex",
            "--destination",
            str(tmp_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "codex mcp add invoice-layout -- invoice-layout mcp" in result.stdout
    assert "pip install" not in result.stdout.casefold()
