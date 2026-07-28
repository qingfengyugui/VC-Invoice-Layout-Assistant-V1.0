from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_installers_download_verified_complete_runtime_only() -> None:
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")

    for script in (powershell, shell):
        assert "SHA256SUMS" in script
        assert "invoice-layout-agent-" in script
        assert "RUNTIME.md" in script
        lowered = script.casefold()
        assert "__repository__" not in lowered
        assert "qingfengyugui/vc-invoice-layout-assistant-v1.0" in lowered
        for forbidden in (
            "pip install",
            "winget install",
            "choco install",
            "apt-get install",
            "brew install",
            "docker build",
        ):
            assert forbidden not in lowered
    assert "'invoice-layout-agent'" in powershell
    assert ".invoice-layout-agent" in shell


def test_readme_has_publishable_install_urls_without_owner_placeholder() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "<owner>" not in readme
    assert "qingfengyugui/VC-Invoice-Layout-Assistant-V1.0" in readme


def test_bootstrap_installers_cover_all_supported_agent_platforms() -> None:
    combined = (ROOT / "install.ps1").read_text(encoding="utf-8") + (
        ROOT / "install.sh"
    ).read_text(encoding="utf-8")

    for platform in (
        "codex",
        "claude-code",
        "openclaw",
        "workbuddy",
        "qoder",
        "qclaw",
    ):
        assert platform in combined


def test_linux_release_locates_ubuntu_noble_7zip_binary() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert 'echo "SEVEN_ZIP=$(command -v 7z)"' in workflow
