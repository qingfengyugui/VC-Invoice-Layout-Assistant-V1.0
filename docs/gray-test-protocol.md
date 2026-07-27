# Private gray-test protocol

Gray tests are a local release gate for private invoice batches. Never commit real inputs, expected JSON, previews, manifests, reports, PDFs, or source bytes. Store real material under `.private-fixtures/` and outputs under `gray-output/`; both are ignored.

## Expected JSON

Create an untracked expected JSON file with exactly these keys:

```json
{
  "page_order": ["<source-sha256>:<zero-based-page-index>"],
  "association_groups": [["<source-sha256>:<zero-based-page-index>"]],
  "included_page_keys": ["<source-sha256>:<zero-based-page-index>"],
  "warning_codes": ["warning_code"],
  "manual_findings": {
    "critical_content_crop_page_keys": [],
    "visible_normal_page_annotation_page_indexes": []
  }
}
```

The schema is strict: unknown keys, malformed hashes, duplicate page keys, invalid warning codes, and invalid manual findings fail closed. Page keys are full source SHA-256 values plus a zero-based page index. Record critical crop findings and visible annotations only after inspecting every rendered page at 100%; pixels alone do not prove semantic crop safety.

## Run and inspect

```powershell
python scripts/gray_test.py .private-fixtures/batch `
  --output-dir gray-output/run-001 `
  --expected .private-fixtures/expected.json `
  --provider local
```

For host observations, add the local hash-bound manifest and use `--provider host`. The host platform's multimodal handling follows that platform's own data policy. The invoice core remains keyless and stores only local previews plus the hash-bound observation manifest.

Inspect every rendered page at 100%, then print at least one representative page and check QR/text size physically. Confirm category/order, associations, omissions, duplicate placement, warning recall, A4 MediaBoxes, sendable review-page exclusion, and normal-page annotations. A nonzero critical crop or visible annotation finding fails the release gate.

`gray-report.json` is atomically written in the output directory. It contains aggregate metrics, warning codes, and SHA-derived identifiers only; it intentionally excludes paths, filenames, OCR text, amounts, and other financial content. Exit code `0` means the gate passes, `2` means the case completed but failed the gate, and `1` means invalid input or private processing failure.
