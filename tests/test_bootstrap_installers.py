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


def test_ci_pins_java17_compiler_and_avoids_mutable_font_download() -> None:
    pom = (ROOT / "tools" / "ofd-renderer" / "pom.xml").read_text(
        encoding="utf-8"
    )
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "<artifactId>maven-compiler-plugin</artifactId>" in pom
    assert "<version>3.13.0</version>" in pom
    assert "<release>17</release>" in pom
    assert "NotoSansCJKsc-Regular.otf" not in ci
    assert "raw.githubusercontent.com/notofonts" not in ci


def test_windows_ci_exports_chocolatey_maven_and_uses_cmd_wrapper() -> None:
    from tools.build_ofd_renderer import _maven_command

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    resolved = r"C:\ProgramData\chocolatey\lib\maven\apache-maven-3.9.16\bin\mvn.cmd"

    assert "-Filter 'mvn.cmd'" in ci
    assert "$env:GITHUB_PATH" in ci
    assert _maven_command(platform="win32", which=lambda _name: resolved) == [
        resolved,
        "-q",
        "-DskipTests",
        "clean",
        "package",
    ]


def test_windows_release_clears_accepted_warning_exit_code() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    windows_smoke = release.split(
        "- name: Smoke test complete Windows runtime", maxsplit=1
    )[1].split("- name: Smoke test complete Unix runtime", maxsplit=1)[0]

    assert "if ($LASTEXITCODE -notin 0,2) { exit $LASTEXITCODE }" in windows_smoke
    assert "exit 0" in windows_smoke
