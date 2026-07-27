"""Validate portable invoice-layout-agent Skills without reading private inputs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

TARGETS = ("codex", "claude-code", "openclaw", "workbuddy", "qoder", "qclaw")
EXPECTED_COMPATIBILITY = {
    "codex": {
        "official_documentation": [
            "https://developers.openai.com/codex/skills",
            "https://developers.openai.com/codex/mcp",
        ],
        "skill_roots": [".agents/skills"],
        "invocation": {"supported": "mcp", "command": "codex mcp add invoice-layout -- invoice-layout mcp"},
    },
    "claude-code": {
        "official_documentation": [
            "https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview",
            "https://code.claude.com/docs/en/mcp",
        ],
        "skill_roots": [".claude/skills", "~/.claude/skills"],
        "invocation": {"supported": "mcp", "command": "claude mcp add --transport stdio invoice-layout -- invoice-layout mcp"},
    },
    "openclaw": {
        "official_documentation": ["https://docs.openclaw.ai/tools/skills"],
        "skill_roots": ["skills", ".agents/skills", "~/.openclaw/skills"],
        "invocation": {"supported": "cli", "command": "openclaw skills install ./path/to/invoice-layout-agent"},
    },
    "workbuddy": {
        "official_documentation": [
            "https://docs.work-buddy.ai/",
            "https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview",
            "https://code.claude.com/docs/en/mcp",
        ],
        "skill_roots": [".claude/skills", "~/.claude/skills"],
        "invocation": {"supported": "mcp", "command": "claude mcp add --transport stdio invoice-layout -- invoice-layout mcp"},
    },
    "qoder": {
        "official_documentation": [
            "https://docs.qoder.com/qoderwork/skills",
            "https://docs.qoder.com/en/cli/Skills",
            "https://docs.qoder.com/en/cli/mcp-servers",
        ],
        "skill_roots": ["~/.qoderwork/skills", "~/.qoder/skills"],
        "invocation": {"supported": "mcp", "command": "qodercli mcp add invoice-layout -- invoice-layout mcp"},
    },
    "qclaw": {
        "official_documentation": ["https://github.com/QuantumClaw/QClaw"],
        "skill_roots": ["ClawHub", "Skills dashboard"],
        "invocation": {
            "supported": "cli",
            "command": "qclaw skill search invoice-layout-agent && qclaw skill install <official-search-result-slug>",
        },
    },
}
EXPECTED_OPENAI_METADATA = {
    "interface": {
        "display_name": "Invoice Layout Agent",
        "short_description": "Prepare private invoice layout batches safely",
        "default_prompt": "Use $invoice-layout-agent to prepare and lay out a private invoice batch.",
    }
}
REQUIRED = (
    "RUNTIME.md", "`doctor` command", "prepare_invoice_batch", "inspect every preview",
    "exact page/hash binding", "layout_invoices", "local OCR", "both PDFs",
    "private report", "not mailed", "sendable PDF excludes it", "out of Git",
    "files, folders, archives, images, PDFs, OFD, XML, or mixed input",
    "flight, rail, lodging, taxi, then other transport",
    "Normal A4 pages contain only original ticket content or safe crops",
    "Do not add page numbers, category labels, filenames, annotations, borders, or crop marks",
    "Do not use image generation, enhance images, or otherwise alter",
    "Do not generate a spreadsheet or copy original archives into a new archive",
    "must be last, occupy its own page, and be excluded from the sendable PDF",
    "RAR", "PDFium", "Java/OFDRW", "Never ask the user to install WPS",
    "manual visual acceptance",
    "do not silently omit",
)
COMMAND_FORBIDDEN = re.compile(r"(?:api[_ -]?key|openai[_-]|sk-[A-Za-z0-9])", re.IGNORECASE)
BUSINESS_CODE = re.compile(r"^\s*(?:def|class)\s+|import\s+invoice_layout", re.MULTILINE)


def _load_frontmatter(document: str) -> dict[str, Any] | None:
    lines = document.splitlines()
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
        parsed = yaml.safe_load("\n".join(lines[1:end]))
    except (ValueError, yaml.YAMLError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _errors(root: Path) -> list[str]:
    errors: list[str] = []
    expected_folders = {"common", *TARGETS}
    folders = {path.name for path in root.iterdir() if path.is_dir()} if root.is_dir() else set()
    if folders != expected_folders:
        errors.append(f"platform inventory must be {sorted(expected_folders)}")
    skills = [root / "common" / "invoice-layout-agent", *[
        root / target / "invoice-layout-agent" for target in TARGETS
    ]]
    for skill in skills:
        document_path = skill / "SKILL.md"
        if not document_path.is_file():
            errors.append(f"missing {document_path}")
            continue
        document = document_path.read_text(encoding="utf-8")
        frontmatter = _load_frontmatter(document)
        if (
            frontmatter is None
            or set(frontmatter) != {"name", "description"}
            or frontmatter.get("name") != "invoice-layout-agent"
            or not isinstance(frontmatter.get("description"), str)
            or not frontmatter["description"].startswith("Use when")
        ):
            errors.append(f"invalid frontmatter in {document_path}")
        metadata_path = skill / "agents" / "openai.yaml"
        try:
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            errors.append(f"invalid agents/openai.yaml in {skill}: {error}")
        else:
            if metadata != EXPECTED_OPENAI_METADATA:
                errors.append(f"invalid agents/openai.yaml contract in {skill}")
        for phrase in REQUIRED:
            if phrase.casefold() not in document.casefold():
                errors.append(f"{document_path} must contain {phrase!r}")
        if BUSINESS_CODE.search(document):
            errors.append(f"{document_path} embeds invoice business-rule code")
        for command in re.findall(r"`([^`]+)`", document):
            if COMMAND_FORBIDDEN.search(command):
                errors.append(f"{document_path} command requests credentials")
    metadata_path = root / "compatibility.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [*errors, f"invalid compatibility metadata: {error}"]
    if set(metadata) != set(TARGETS):
        errors.append("compatibility target inventory is incorrect")
    for target, expected in EXPECTED_COMPATIBILITY.items():
        record = metadata.get(target)
        if record != {"verified_at": "2026-07-27", **expected}:
            for field, value in expected.items():
                if not isinstance(record, dict) or record.get(field) != value:
                    errors.append(f"{target} {field} is incorrect")
            if not isinstance(record, dict) or record.get("verified_at") != "2026-07-27":
                errors.append(f"{target} verification date is incorrect")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("platforms"))
    args = parser.parse_args()
    errors = _errors(args.root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("adapter validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
