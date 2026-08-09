---
name: rights-first-footage-collector
description: Find, download, receipt, and visually review commercially reusable stock-footage candidates. Use when an agent must search an existing licensed catalog before Pexels or Pixabay, preserve creator and licence evidence, reject technical failures and duplicate clips, create contact sheets, or prepare candidates without scraping social platforms or automatically approving media for production.
---

# Rights-First Footage Collector

Collect candidates, not an unreviewed media dump.

## Hard rules

- Search a supplied local catalog before external providers.
- Use official provider APIs. Never scrape or bulk-rip YouTube or social platforms.
- Keep API keys in process environment variables or a mode-600 env file.
- Never print keys, persist them in manifests, or place them in command arguments.
- Treat every external download as `CANDIDATE_NEEDS_REVIEW`.
- Never modify the production catalog automatically.
- Reject missing rights fields, invalid media, duplicate source IDs, duplicate files, and perceptually similar footage.
- Inspect the contact sheet and provider source pages before accepting any candidate.

## Requirements

- Python 3.10+
- Pillow
- `ffmpeg` and `ffprobe` on `PATH`
- `PEXELS_API_KEY` and/or `PIXABAY_API_KEY`

Read [references/source-policy.md](references/source-policy.md) before adding a provider or approving footage.

## Run

```bash
python3 scripts/collect.py \
  --query "gothic cathedral flying buttress aerial" \
  --query "stone cathedral interior vault" \
  --provider pexels \
  --env-file /absolute/path/to/private.env \
  --catalog /absolute/path/to/catalog.jsonl \
  --out /absolute/path/to/candidates \
  --download \
  --max-downloads 6
```

Environment variables override values in `--env-file`. On POSIX, the supplied env file must use mode `0600`.

## Outputs

- `candidate_manifest.jsonl`
- `RUN_RECEIPT.json`
- `contact_sheet.jpg` when files are downloaded
- `clips/*.mp4`
- `frames/*-{start,middle,end}.jpg`

The manifest omits provider download URLs and secret-shaped fields. The private API cache may contain provider responses and uses mode-600 files under `~/.cache/rights-first-footage-collector` unless `--cache-dir` is supplied.

## Review workflow

1. Turn the approved script into concrete visual beats.
2. Search the licensed local catalog for every beat.
3. Run the collector only for unresolved beats.
4. Inspect `contact_sheet.jpg` and each provider source page.
5. Confirm that every shot proves its narration beat.
6. Copy only accepted originals into the production asset store.
7. Append accepted receipts through the production catalog operator.
8. Run the target pipeline's no-repeat, source, timeline, and approval gates.

## Acceptance gate

Accept a candidate only when all are true:

- The footage matches the exact subject, scale, action, and setting.
- Provider, creator, source page, provider ID, licence and licence URL are recorded.
- The file passes resolution, orientation, duration and media-probe checks.
- SHA-256 and three-frame dHash comparisons pass against the run and supplied catalog.
- A human reviewer has inspected the actual frames and source page.

## Verify

From the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 /path/to/quick_validate.py rights-first-footage-collector
```
