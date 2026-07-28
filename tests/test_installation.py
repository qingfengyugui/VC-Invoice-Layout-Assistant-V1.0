from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_cli_help_and_doctor_are_available() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["invoice-layout"] == (
        "invoice_layout.cli:app"
    )
    command = [sys.executable, "-m", "invoice_layout.cli"]
    assert subprocess.run([*command, "--help"], check=False).returncode == 0
    assert subprocess.run([*command, "doctor"], check=False).returncode in {0, 2}


def test_wheel_contract_contains_packaged_ofd_renderer() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "rarfile>=4.2,<5" in project["project"]["dependencies"]
    force_include = project["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]
    assert force_include == {
        "src/invoice_layout/bin/ofd-renderer.jar": (
            "invoice_layout/bin/ofd-renderer.jar"
        )
    }


def test_dockerfile_builds_wheel_then_installs_only_wheel_in_runtime() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith(
        "FROM maven:3.9.16-eclipse-temurin-17 AS wheel-build\n"
    )
    assert "python -m build --wheel" in dockerfile
    assert "invoice_layout/bin/ofd-renderer.jar" in dockerfile
    assert "rm src/invoice_layout/bin/ofd-renderer.jar" in dockerfile

    runtime = dockerfile.split(
        "FROM python:3.13.14-slim-bookworm AS runtime\n", maxsplit=1
    )[1]
    assert "default-jre-headless" in runtime
    assert "poppler-utils" in runtime
    assert "tesseract-ocr-chi-sim" in runtime
    assert "fonts-noto-cjk" in runtime
    assert "unar" in runtime
    assert "COPY --from=wheel-build /build/dist/" in runtime
    assert "pip install --no-cache-dir /tmp/wheels/*.whl" in runtime
    assert "COPY src" not in runtime
    assert "pip install -e" not in runtime
    assert 'ENTRYPOINT ["invoice-layout"]' in runtime


def test_dockerignore_excludes_private_and_generated_invoice_material() -> None:
    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        ".git",
        ".worktrees",
        ".private-fixtures",
        "gray-output",
        ".env*",
        ".model-cache",
        "model-responses",
        "output",
        "*.pdf",
        "*.ofd",
        "*.xml",
        "*.png",
        "*.jpg",
        "*.jpeg",
        "*.heic",
        "*.zip",
        "*.7z",
        "*.rar",
        "!tools/ofd-renderer/pom.xml",
    }
    assert required <= ignored


def test_ci_has_exact_cross_platform_matrix_and_verification_gates() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)

    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    matrix = jobs["test"]["strategy"]["matrix"]
    assert matrix == {
        "os": ["windows-latest", "macos-latest", "ubuntu-latest"],
        "python": ["3.11", "3.13"],
    }
    assert len(matrix["os"]) * len(matrix["python"]) == 6
    assert workflow_text.count("actions/checkout@v6") == 2
    assert "actions/setup-python@v6" in workflow_text

    install_steps = {
        step["name"]: step["run"].lower()
        for step in jobs["test"]["steps"]
        if step.get("name", "").startswith("Install ") and "run" in step
    }
    linux = install_steps["Install Linux native dependencies"]
    for dependency in (
        "openjdk-17-jdk-headless",
        "maven",
        "poppler-utils",
        "tesseract-ocr",
        "tesseract-ocr-chi-sim",
        "fonts-noto-cjk",
        "unar",
    ):
        assert dependency in linux
    macos = install_steps["Install macOS native dependencies"]
    for dependency in (
        "openjdk@17",
        "maven",
        "poppler",
        "tesseract",
        "tesseract-lang",
        "font-noto-sans-cjk",
        "unar",
    ):
        assert dependency in macos
    windows = install_steps["Install Windows native dependencies"]
    for dependency in (
        "temurin17",
        "maven",
        "poppler",
        "tesseract",
        "/language:chi_sim",
        "7zip",
    ):
        assert dependency in windows
    assert "raw.githubusercontent.com/notofonts" not in windows
    for gate in (
        'pip install -e ".[dev]"',
        "python -m ruff check .",
        "python -m mypy src",
        "--cov=invoice_layout",
        "--cov-fail-under=85",
        "python scripts/validate_adapters.py",
        "python -m build --wheel",
        "invoice_layout/bin/ofd-renderer.jar",
        "usage: OFDRenderer",
        "invoice-layout doctor",
        "docker build -t invoice-layout-agent:test .",
        "invoice-layout process /case/input/geometry.pdf --provider local",
        "test -s /case/output/printable.pdf",
        "test -s /case/output/sendable.pdf",
        "test -s /case/output/report.json",
    ):
        assert gate in workflow_text


def test_installation_tests_run_under_supported_python() -> None:
    assert (3, 11) <= sys.version_info[:2] < (3, 14)


def test_release_workflow_builds_complete_portable_bundles() -> None:
    workflow_text = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    for target in (
        "windows-x64",
        "linux-x64",
        "macos-arm64",
        "macos-x64",
    ):
        assert target in workflow_text
    for gate in (
        "PyInstaller",
        "--onefile",
        "pypdfium2",
        "build_portable_bundle.py",
        "native/java",
        "7z",
        "doctor",
        "printable.pdf",
        "sendable.pdf",
        "SHA256SUMS",
        "gh release",
    ):
        assert gate in workflow_text
