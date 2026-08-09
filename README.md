# Rights-First Footage Collector

A small, auditable stock-footage collector for AI-assisted video pipelines.

It searches a licensed local catalog first, then uses official Pexels or Pixabay APIs only for unresolved visual beats. Downloads remain candidates until a human reviews the contact sheet and source pages.

## Why this exists

Most automated video tools optimize for filling a timeline. This project optimizes for four different questions:

1. Can the footage be traced to its creator and source page?
2. Is the stated licence known?
3. Does the actual footage match the narration?
4. Has the same or visually similar footage already been used?

## Features

- Pexels and Pixabay API search.
- Optional catalog-first lookup.
- Creator, source-page, provider-ID and licence receipts.
- Minimum 1920×1080 landscape and four-second duration gate.
- Streaming downloads with pre-contact redirect allowlists and size limits.
- SHA-256 duplicate checks.
- Three-frame perceptual dHash checks with a configurable Hamming threshold.
- Start, middle and end review frames.
- Contact-sheet generation.
- Private API cache with a mandatory 24-hour minimum for Pixabay.
- A hard cap on network download attempts, including rejected files.
- Automatic removal of rejected media and review frames.
- Atomic mode-600 manifests, receipts and cache files on POSIX.
- No social-media scraping and no automatic production approval.

## Install

Requirements:

- Python 3.10+
- `ffmpeg` and `ffprobe`

```bash
git clone https://github.com/Rebelzxr/rights-first-footage-collector.git
cd rights-first-footage-collector
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Add one or both provider keys to `.env`. Never commit that file.

## Use

```bash
python3 rights-first-footage-collector/scripts/collect.py \
  --query "gothic cathedral flying buttress aerial" \
  --query "stone cathedral interior vault" \
  --provider pexels \
  --env-file .env \
  --out ./runtime/cathedral-candidates \
  --download \
  --max-downloads 6
```

To search a local JSONL catalog first:

```bash
python3 rights-first-footage-collector/scripts/collect.py \
  --query "off grid solar charge controller close up" \
  --provider all \
  --env-file .env \
  --catalog /absolute/path/to/catalog.jsonl \
  --out ./runtime/solar-candidates \
  --download
```

A catalog row can contain:

```json
{"path":"/media/solar-controller.mp4","tags":["solar","controller","off-grid"],"provider":"pexels","source_id":"123","source_page":"https://www.pexels.com/video/123/","creator":"Example Creator","license":"Pexels License","sha256":"...","dhashes":["...","...","..."]}
```

## Outputs

```text
runtime/cathedral-candidates/
├── RUN_RECEIPT.json
├── candidate_manifest.jsonl
├── contact_sheet.jpg
├── clips/
└── frames/
```

Every accepted external file stays `CANDIDATE_NEEDS_REVIEW`. The collector never writes to the supplied catalog. `--max-downloads` caps network attempts, not merely accepted files; rejected media and its frames are removed.

## Security model

- API keys are read from process environment variables or a mode-600 env file.
- Download URLs and secret-shaped fields are excluded from manifests.
- Errors remove URLs and secret-like values.
- API and download hosts are matched on exact domains or dot-delimited subdomains and must use HTTPS.
- Every redirect target is checked before contact, and authorization headers are stripped across origins.
- Downloads are streamed into partial files with a hard size limit, then moved atomically.
- Output, cache and receipt files use private POSIX permissions.

Provider API responses are cached privately and can contain temporary download URLs. Protect the cache directory and do not publish it. Pixabay cache entries are retained for at least 24 hours even if a lower CLI value is requested.

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 /path/to/quick_validate.py rights-first-footage-collector
```

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md). Please do not include API keys, downloaded media, provider response caches or unlicensed sample footage in an issue or pull request.

## Licence

Code and skill instructions are released under the [MIT License](LICENSE). Provider media remains governed by its own licence and source-page terms.
