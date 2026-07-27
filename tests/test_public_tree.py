from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_public_tree import scan_public_tree

ROOT = Path(__file__).resolve().parents[1]


def _tracked_repository(root: Path, files: dict[str, bytes | str]) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


@pytest.mark.parametrize(
    "name",
    [
        "evidence.pdf",
        "flight.ofd",
        "batch.rar",
        "batch.zip",
        "preview.png",
        "report.json",
        ".env",
        "model-responses/result.json",
        ".private-fixtures/input.txt",
        "gray-output/result.txt",
    ],
)
def test_scanner_rejects_private_artifact_paths(name: str, tmp_path: Path) -> None:
    repository = _tracked_repository(tmp_path, {name: "synthetic"})

    findings = scan_public_tree(repository)

    assert findings
    assert all(name.split("/")[0] in finding for finding in findings)


def test_scanner_rejects_private_paths_credentials_and_response_markers(
    tmp_path: Path,
) -> None:
    non_system_path = chr(ord("C") + 3) + ":" + chr(92) + "finance" + chr(92) + "invoice.pdf"
    user_path = "C:" + chr(92) + "Users" + chr(92) + "Example" + chr(92) + "invoice.pdf"
    credential = "s" + "k-" + "A" * 32
    model_marker = '{"' + "model" + '_response": "private"}'
    content = f"{non_system_path}\n{user_path}\n{credential}\n{model_marker}"
    repository = _tracked_repository(tmp_path, {"notes.txt": content})

    findings = scan_public_tree(repository)

    assert len(findings) == 4
    assert all("notes.txt" in finding for finding in findings)
    assert all(credential not in finding for finding in findings)


def test_scanner_rejects_system_drive_and_posix_absolute_paths(tmp_path: Path) -> None:
    system_path = "C:" + chr(92) + "finance" + chr(92) + "invoice.pdf"
    slash = chr(47)
    posix_path = f"{slash}var{slash}private{slash}invoice.pdf"
    repository = _tracked_repository(
        tmp_path, {"notes.txt": f"{system_path}\n{posix_path}\n"}
    )

    findings = scan_public_tree(repository)

    assert len(findings) == 2
    assert all("notes.txt" in finding for finding in findings)


def test_scanner_decodes_common_utf16_text_before_scanning(tmp_path: Path) -> None:
    credential = "s" + "k-" + "C" * 32
    user_path = "C:" + chr(92) + "Users" + chr(92) + "Example" + chr(92) + "invoice.pdf"
    repository = _tracked_repository(
        tmp_path, {"notes.txt": f"{credential}\n{user_path}".encode("utf-16")}
    )

    findings = scan_public_tree(repository)

    assert len(findings) == 3
    assert all(credential not in finding for finding in findings)


def test_scanner_allows_source_docs_yaml_and_synthetic_xml(tmp_path: Path) -> None:
    repository = _tracked_repository(
        tmp_path,
        {
            "module.py": "print('synthetic')\n",
            "README.md": "# Synthetic geometry fixture\n",
            ".github/workflows/ci.yml": "name: ci\n",
            "tests/example.xml": "<Invoice><Amount>0.00</Amount></Invoice>\n",
            "tools/OFDRenderer.java": "final class OFDRenderer {}\n",
        },
    )

    assert scan_public_tree(repository) == []


def test_scanner_ignores_untracked_private_files(tmp_path: Path) -> None:
    repository = _tracked_repository(tmp_path, {"README.md": "safe\n"})
    (repository / "private.pdf").write_bytes(b"private")

    assert scan_public_tree(repository) == []


def test_cli_reports_only_relative_path_and_reason(tmp_path: Path) -> None:
    credential = "s" + "k-" + "B" * 32
    repository = _tracked_repository(tmp_path, {"notes.txt": credential})

    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_public_tree.py",
            "--root",
            str(repository),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "notes.txt" in result.stderr
    assert credential not in result.stderr
