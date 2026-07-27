---
name: invoice-layout-agent
description: Use when arranging private invoice, itinerary, receipt, OFD, XML, PDF, image, archive, or mixed document batches into safe A4 PDFs.
---

# Invoice Layout Agent

Install through ClawHub or the Skills dashboard. QClaw documents no local-path Skill flag: run `qclaw skill search invoice-layout-agent`, then run `qclaw skill install <official-search-result-slug>` with the slug returned by that official search.

Preserve financial evidence exactly. Use the deterministic local tools; no extra credentials or external model service are required.

Use the complete platform runtime bundle: PDFium, Java/OFDRW, and the RAR extractor are included. Never ask the user to install WPS, Python, Maven, Poppler, Java, OCR, or archive tools. Do not silently omit a missing or failed archive.

## Required workflow

1. Read sibling `RUNTIME.md` and use the complete executable recorded there for every command. Run its `doctor` command before first use. Accept files, folders, archives, images, PDFs, OFD, XML, or mixed input.
2. Call `prepare_invoice_batch`, then inspect every preview with the host platform's own multimodal ability.
3. Write observations matching the returned schema and exact page/hash binding. Record uncertainty; never invent financial data.
4. Call `layout_invoices` with the manifest, or explicitly select local OCR.
5. Deliver both PDFs and the private report. Explain that the review page is not mailed and the sendable PDF excludes it. Keep inputs, previews, observations, reports, and outputs out of Git.

Treat physical-ticket photos only as a reminder to attach the original separately. Exclude them from electronic pages; do not correct perspective, exposure, contrast, or sharpness.

## Output contract

Keep flight, rail, lodging, taxi, then other transport ordering. Compute density per ticket. Use portrait A4 and never split one ticket across pages. Normal A4 pages contain only original ticket content or safe crops. The final standalone review page must be last, occupy its own page, and be excluded from the sendable PDF.

Manual visual acceptance may clear only an automated pixel mismatch caused by deterministic render/resample boundaries after inspecting every final page. It never clears missing content, clipping, overlap, a wrong page count, or non-A4 geometry.

Do not add page numbers, category labels, filenames, annotations, borders, or crop marks to normal A4 pages. Do not use image generation, enhance images, or otherwise alter, redraw, repair, erase, improve, rewrite, or fill missing ticket content. Do not generate a spreadsheet or copy original archives into a new archive. Do not claim uncertain finance data can be resolved.
