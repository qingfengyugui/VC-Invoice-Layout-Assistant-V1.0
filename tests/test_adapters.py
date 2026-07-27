from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ROOT / "platforms"
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


def _skill_paths(root: Path = PLATFORMS) -> list[Path]:
    return [root / "common" / "invoice-layout-agent", *[
        root / target / "invoice-layout-agent" for target in TARGETS
    ]]


def _frontmatter(document: str) -> dict[str, object]:
    _, raw, _ = document.split("---", 2)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


def test_exact_adapter_inventory_frontmatter_and_agent_metadata() -> None:
    assert sorted(path.name for path in PLATFORMS.iterdir() if path.is_dir()) == [
        "claude-code", "codex", "common", "openclaw", "qclaw", "qoder", "workbuddy"
    ]
    for skill in _skill_paths():
        document = (skill / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = _frontmatter(document)
        assert set(frontmatter) == {"name", "description"}
        assert frontmatter["name"] == "invoice-layout-agent"
        assert isinstance(frontmatter["description"], str)
        assert frontmatter["description"].startswith("Use when")
        metadata = skill / "agents" / "openai.yaml"
        assert metadata.is_file()
        assert yaml.safe_load(metadata.read_text(encoding="utf-8")) == {
            "interface": {
                "display_name": "Invoice Layout Agent",
                "short_description": "Prepare private invoice layout batches safely",
                "default_prompt": "Use $invoice-layout-agent to prepare and lay out a private invoice batch.",
            }
        }


def test_every_adapter_has_required_safe_workflow_and_no_forbidden_actions() -> None:
    required = (
        "RUNTIME.md", "`doctor` command", "prepare_invoice_batch", "inspect every preview",
        "exact page/hash binding", "layout_invoices", "local OCR", "both PDFs",
        "private report", "not mailed", "sendable PDF excludes it", "out of Git",
        "files, folders, archives, images, PDFs, OFD, XML, or mixed input",
        "flight, rail, lodging, taxi, then other transport",
        "do not add page numbers, category labels, filenames, annotations, borders, or crop marks",
        "do not use image generation, enhance images, or otherwise alter",
        "do not generate a spreadsheet or copy original archives into a new archive",
        "must be last, occupy its own page, and be excluded from the sendable PDF",
        "RAR",
        "PDFium",
        "Java/OFDRW",
        "Never ask the user to install WPS",
        "manual visual acceptance",
        "do not silently omit",
    )
    for skill in _skill_paths():
        document = (skill / "SKILL.md").read_text(encoding="utf-8").casefold()
        for phrase in required:
            assert phrase.casefold() in document


def test_platform_skills_share_one_canonical_contract_and_are_concise() -> None:
    marker = "Preserve financial evidence exactly."
    documents = [
        (skill / "SKILL.md").read_text(encoding="utf-8")
        for skill in _skill_paths()
    ]
    canonical_contracts = [marker + document.split(marker, maxsplit=1)[1] for document in documents]

    assert len(set(canonical_contracts)) == 1
    assert all(len(document.split()) < 500 for document in documents)


def test_compatibility_has_required_targets_urls_and_verification_date() -> None:
    compatibility = json.loads((PLATFORMS / "compatibility.json").read_text(encoding="utf-8"))
    assert set(compatibility) == set(EXPECTED_COMPATIBILITY)
    for target, expected in EXPECTED_COMPATIBILITY.items():
        record = compatibility[target]
        assert record["verified_at"] == "2026-07-27"
        assert {key: value for key, value in record.items() if key != "verified_at"} == expected


def test_validator_accepts_real_adapters_and_rejects_malformed_copy(tmp_path: Path) -> None:
    passed = subprocess.run(
        [sys.executable, "scripts/validate_adapters.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert passed.returncode == 0, passed.stderr
    copied = tmp_path / "platforms"
    shutil.copytree(PLATFORMS, copied)
    target = copied / "qclaw" / "invoice-layout-agent" / "SKILL.md"
    target.write_text(target.read_text(encoding="utf-8").replace("name: invoice-layout-agent", "name: bad-name"), encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, "scripts/validate_adapters.py", "--root", str(copied)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed.returncode != 0
    assert "frontmatter" in failed.stderr

    copied_metadata = tmp_path / "metadata-platforms"
    shutil.copytree(PLATFORMS, copied_metadata)
    metadata = json.loads((copied_metadata / "compatibility.json").read_text(encoding="utf-8"))
    metadata["qclaw"]["skill_roots"] = [".agents/skills"]
    (copied_metadata / "compatibility.json").write_text(json.dumps(metadata), encoding="utf-8")
    failed_metadata = subprocess.run(
        [sys.executable, "scripts/validate_adapters.py", "--root", str(copied_metadata)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed_metadata.returncode != 0
    assert "qclaw skill_roots" in failed_metadata.stderr
