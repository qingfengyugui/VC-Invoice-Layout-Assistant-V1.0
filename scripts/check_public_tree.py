"""Reject private artifacts and sensitive content from the tracked public tree."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

FORBIDDEN_SUFFIXES = {
    ".7z",
    ".bmp",
    ".gif",
    ".gz",
    ".heic",
    ".jpeg",
    ".jpg",
    ".ofd",
    ".pdf",
    ".png",
    ".rar",
    ".tar",
    ".tgz",
    ".tif",
    ".tiff",
    ".webp",
    ".zip",
}
FORBIDDEN_COMPONENTS = {
    ".invoice-layout-private",
    ".model-cache",
    ".private-fixtures",
    "gray-output",
    "model-responses",
    "output-final",
    "qa-final",
}
WINDOWS_PATH = re.compile(r"(?i)(?<![a-z])(?P<drive>[a-z]):[\\/][^\r\n]+")
USER_PROFILE_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/](?:users|documents and settings)[\\/][^\\/\r\n]+"
    r"|/(?:users|home)/[^/\r\n]+)"
)
POSIX_PRIVATE_PATH = re.compile(
    r"(?i)(?<![:a-z0-9_.-])/(?:data|finance|home|private|srv|users|var)/[^\s`\"']+"
)
CREDENTIAL = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})"
)
MODEL_RESPONSE = re.compile(r'''["'](?:model_response|response_body)["']\s*:''')
GRAY_OUTPUT_MARKER = re.compile(r'''["'](?:gray_output_path|gray_report_path)["']\s*:''')
PUBLIC_WINDOWS_PREFIXES = ("\\program files\\", "\\programdata\\", "\\windows\\")
PUBLIC_POSIX_PREFIXES = ("/var/lib/apt/lists",)


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("unable to read tracked-file inventory")
    return [
        raw.decode("utf-8", errors="surrogateescape")
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def _path_reason(relative: str) -> str | None:
    path = PurePosixPath(relative)
    components = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    if components & FORBIDDEN_COMPONENTS:
        return "private artifact directory"
    if name == ".env" or name.startswith(".env."):
        return "environment file"
    if name == "report.json" or name.endswith(("-report.json", "_report.json")):
        return "private report"
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        return "financial, archive, or preview artifact"
    if name.startswith("tmp_gray_"):
        return "one-off gray-test helper"
    return None


def _content_reasons(content: str) -> list[str]:
    reasons: list[str] = []
    windows_paths = [
        match.group(0).replace("/", "\\").casefold()
        for match in WINDOWS_PATH.finditer(content)
    ]
    if any(
        not any(path[2:].startswith(prefix) for prefix in PUBLIC_WINDOWS_PREFIXES)
        for path in windows_paths
    ):
        reasons.append("absolute Windows path")
    if USER_PROFILE_PATH.search(content):
        reasons.append("user-profile absolute path")
    posix_paths = [match.group(0).casefold() for match in POSIX_PRIVATE_PATH.finditer(content)]
    if any(
        not any(path.startswith(prefix) for prefix in PUBLIC_POSIX_PREFIXES)
        for path in posix_paths
    ):
        reasons.append("private absolute POSIX path")
    if CREDENTIAL.search(content):
        reasons.append("credential-like token")
    if MODEL_RESPONSE.search(content):
        reasons.append("model-response content")
    if GRAY_OUTPUT_MARKER.search(content):
        reasons.append("gray-test output content")
    return reasons


def _read_text(path: Path) -> str | None:
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                return payload.decode("utf-16")
            except UnicodeDecodeError:
                return None
        return None


def scan_public_tree(root: Path) -> list[str]:
    """Return path-only findings for tracked files; never echo matched values."""
    root = root.resolve()
    findings: list[str] = []
    for relative in _tracked_files(root):
        path_reason = _path_reason(relative)
        if path_reason:
            findings.append(f"{relative}: {path_reason}")
            continue
        content = _read_text(root / relative)
        if content is None:
            continue
        findings.extend(
            f"{relative}: {reason}" for reason in _content_reasons(content)
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        findings = scan_public_tree(args.root)
    except RuntimeError as error:
        print(f"public tree validation failed: {error}", file=sys.stderr)
        return 1
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("public tree validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
