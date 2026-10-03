# README Visual Refresh Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for inline implementation.

**Goal:** Shrink the existing README logo and add original capability/architecture visuals matching the user's references.

**Architecture:** README-only content changes with a raster capability poster and a native SVG architecture diagram. Keep the logo file and runtime behavior unchanged, retain the Mermaid architecture as a text fallback, and use actual documented system boundaries.

**Tech Stack:** Markdown/HTML, built-in imagegen, PNG/SVG, native Quick Look preview, existing Python documentation checks.

---

### Task 1: Existing image display

- [x] Verify that `multiclaw.png` is 1254 × 1254 and inspect both reference images.
- [x] Replace the README logo Markdown with `<img src="multiclaw.png" alt="MultiClaw 标志" width="50%" />` without changing the image file.

### Task 2: Original assets

- [x] Generate the four-card, bright capability poster as `docs/assets/multiclaw-capabilities.png` using the built-in tool and the approved reference direction.
- [x] Draw the five-layer, dark architecture diagram as `docs/assets/multiclaw-architecture.svg` with tenant scope and reviewed-write boundaries. Use a self-contained 1800 × 1260 SVG, five colored layer groups, no scripts or external resources, and large Chinese labels.
- [x] Inspect all Chinese labels, relationships and unsupported feature claims; resolve any important defect.
- [x] Preserve the final prompts and intended local paths in `docs/assets/readme-visual-prompts.md`.

### Task 3: README integration and checks

- [x] Add the capability image with descriptive alt text after the introduction/status notice.
- [x] Add the SVG directly under architecture overview with descriptive alt text, and retain the existing Mermaid in `<details>` with summary `查看文本版架构图`.
- [x] Run `uv run --no-sync python scripts/check_docs.py` and `uv run --no-sync pytest -q tests/test_documentation.py`: documentation validation passed and all 11 tests passed.
- [x] Run `sips -g pixelWidth -g pixelHeight` on the PNG and `xmllint --noout` on the SVG; verify exactly five layer groups and every README local image path. Quick Look cropped its preview, so use a full 1800 × 1260 headless Chrome render for direct inspection instead.
- [x] Run `git diff --check`, confirm `multiclaw.png` is unchanged and hand off image paths for review without committing or pushing.

## Generation blocker

The capability image is a verified 1672 × 941 PNG (about 1.5 MB); independent
spec review found no Important issues. GitHub's Markdown render API confirmed
the existing logo's `width="50%"` attribute is retained. Both architecture
generation attempts failed with a network request error and produced no image.
The nonexistent architecture image reference was removed from README, while
the expanded text/Mermaid fallback remains. The architecture asset and final
integration were incomplete. The user subsequently requested completion after
the proposed native SVG approach; proceed with that approach, without API/CLI
fallback or additional model calls.

## Completion verification

The native SVG is complete and embedded in README; the generation blocker no
longer blocks delivery. A full browser render was visually inspected and an
independent reviewer approved spec compliance and quality with no Important
findings. The browser used an isolated temporary profile and was stopped after
rendering. SVG XML validation, five-layer count, image path checks, documentation
validation, all 11 documentation tests and diff checks passed. The original
logo and favicon assets remain unchanged. No commit or push was performed.
