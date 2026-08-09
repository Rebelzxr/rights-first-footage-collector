#!/usr/bin/env python3
"""Collect rights-receipted stock-footage candidates for human review."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps


USER_AGENT = "rights-first-footage-collector/1.0"
DEFAULT_CACHE_TTL_HOURS = 24
DEFAULT_MAX_FILE_MB = 1024
MAX_API_RESPONSE_BYTES = 10 * 1024 * 1024
LICENSES = {
    "pexels": ("Pexels License", "https://www.pexels.com/license/"),
    "pixabay": (
        "Pixabay Content License",
        "https://pixabay.com/service/license-summary/",
    ),
}
DOWNLOAD_HOST_SUFFIXES = {
    "pexels": (".pexels.com",),
    "pixabay": (".pixabay.com",),
}
API_HOSTS = {"api.pexels.com", "pixabay.com"}
SECRET_NAME_PATTERN = re.compile(r"(?:api[_-]?key|token|secret|authorization)", re.I)


class CollectorError(RuntimeError):
    """Safe, user-facing collector error."""


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    rate_limit_remaining: dict[str, int] = field(default_factory=dict)
    rate_limit_reset: dict[str, str] = field(default_factory=dict)


class AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate redirects before contact and strip credentials across origins."""

    def __init__(self, allowed_hosts: Iterable[str]):
        super().__init__()
        self.allowed_hosts = tuple(allowed_hosts)

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        absolute_url = urllib.parse.urljoin(request.full_url, new_url)
        validate_https_url(absolute_url, self.allowed_hosts)
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, absolute_url
        )
        if redirected is None:
            return None
        old_host = (urllib.parse.urlsplit(request.full_url).hostname or "").lower()
        new_host = (urllib.parse.urlsplit(absolute_url).hostname or "").lower()
        if old_host != new_host:
            for name in ("Authorization", "Proxy-Authorization"):
                redirected.remove_header(name)
                redirected.headers.pop(name, None)
                redirected.unredirected_hdrs.pop(name, None)
        return redirected


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:72] or "footage"


def safe_source_component(value: str) -> str:
    readable = slugify(value)[:40]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}"


def validate_query(value: str) -> str:
    query = " ".join(value.split())
    if not query:
        raise CollectorError("queries must not be empty")
    if len(query) > 300:
        raise CollectorError("queries must be 300 characters or fewer")
    if any(ord(character) < 32 for character in query):
        raise CollectorError("queries must not contain control characters")
    return query


def load_env(path: Path | None) -> dict[str, str]:
    values = dict(os.environ)
    if path is None or not path.is_file():
        return values

    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise CollectorError(
                f"environment file permissions are too open: {path}; run chmod 600"
            )

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values.setdefault(key, value.strip().strip('"').strip("'"))
    return values


def safe_error(error: BaseException) -> str:
    """Return an error summary without URLs, query strings, or secret-shaped fields."""
    if isinstance(error, urllib.error.HTTPError):
        return f"provider request failed with HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return f"provider connection failed: {type(error.reason).__name__}"
    message = str(error)
    message = re.sub(r"https?://\S+", "[URL_REDACTED]", message)
    message = re.sub(
        r"(?i)(api[_-]?key|token|secret|authorization)\s*[=:]\s*\S+",
        r"\1=[REDACTED]",
        message,
    )
    return message[:500]


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    ensure_private_directory(path.parent)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(data)
    if os.name == "posix":
        temp_path.chmod(mode)
    temp_path.replace(path)


def cache_key(provider: str, query: str, page: int, limit: int) -> str:
    payload = json.dumps(
        {"provider": provider, "query": query, "page": page, "limit": limit},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_https_url(url: str, allowed_hosts: Iterable[str]) -> str:
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname:
        raise CollectorError("provider returned a non-HTTPS URL")
    allowed = tuple(host.lower().lstrip(".") for host in allowed_hosts)
    if not any(
        hostname == host or hostname.endswith(f".{host}") for host in allowed
    ):
        raise CollectorError(f"provider returned an unapproved host: {hostname}")
    return url


def build_opener(allowed_hosts: Iterable[str]) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(AllowlistedRedirectHandler(allowed_hosts))


def record_rate_limits(provider: str, headers: Any, stats: CacheStats) -> None:
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    remaining = normalized.get("x-ratelimit-remaining")
    reset = normalized.get("x-ratelimit-reset")
    if remaining is not None:
        try:
            stats.rate_limit_remaining[provider] = int(remaining)
        except ValueError:
            pass
    if reset:
        stats.rate_limit_reset[provider] = reset[:80]


def request_json(
    *,
    provider: str,
    url: str,
    headers: dict[str, str] | None,
    cache_dir: Path,
    cache_ttl_hours: int,
    query: str,
    page: int,
    limit: int,
    stats: CacheStats,
) -> dict[str, Any]:
    validate_https_url(url, API_HOSTS)
    cache_path = cache_dir / f"{cache_key(provider, query, page, limit)}.json"
    if cache_path.is_file() and not cache_path.is_symlink():
        age = time.time() - cache_path.stat().st_mtime
        if age <= cache_ttl_hours * 3600:
            stats.hits += 1
            return json.loads(cache_path.read_text(encoding="utf-8"))

    if stats.rate_limit_remaining.get(provider, 1) <= 0:
        reset = stats.rate_limit_reset.get(provider, "the provider reset time")
        raise CollectorError(f"{provider} API rate limit exhausted; retry after {reset}")

    stats.misses += 1
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    opener = build_opener(API_HOSTS)
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with opener.open(request, timeout=60) as response:
                final_url = response.geturl()
                validate_https_url(final_url, API_HOSTS)
                raw = response.read(MAX_API_RESPONSE_BYTES + 1)
                if len(raw) > MAX_API_RESPONSE_BYTES:
                    raise CollectorError("provider response exceeds the size limit")
                payload = json.loads(raw.decode("utf-8"))
                record_rate_limits(provider, response.headers, stats)
            atomic_write(cache_path, json.dumps(payload).encode("utf-8"))
            return payload
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if isinstance(error, urllib.error.HTTPError) and error.code == 429:
                retry_after = str(
                    (error.headers or {}).get("Retry-After")
                    or "the provider reset time"
                )[:80]
                raise CollectorError(
                    f"{provider} API rate limit reached; retry after {retry_after}"
                ) from error
            if isinstance(error, urllib.error.HTTPError) and error.code not in {
                408,
                500,
                502,
                503,
                504,
            }:
                break
            if attempt < 2:
                time.sleep(2**attempt)
    raise CollectorError(safe_error(last_error or RuntimeError("request failed")))


def select_landscape_file(files: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    options = [
        item
        for item in files
        if item
        and item.get("file_type", "video/mp4") == "video/mp4"
        and int(item.get("width") or 0) >= 1920
        and int(item.get("height") or 0) >= 1080
        and int(item.get("width") or 0) > int(item.get("height") or 0)
        and item.get("link", item.get("url"))
    ]
    options.sort(
        key=lambda item: (
            int(item.get("width") or 0) * int(item.get("height") or 0),
            int(item.get("file_size") or 0),
        ),
        reverse=True,
    )
    return options[0] if options else None


def required_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text or text.lower() in {"none", "null", "unknown", "n/a"}:
        return None
    return text


def pexels_search(
    query: str,
    page: int,
    limit: int,
    key: str,
    cache_dir: Path,
    cache_ttl_hours: int,
    stats: CacheStats,
) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "query": query,
            "orientation": "landscape",
            "size": "large",
            "page": page,
            "per_page": min(80, max(3, limit * 3)),
        }
    )
    payload = request_json(
        provider="pexels",
        url=f"https://api.pexels.com/v1/videos/search?{params}",
        headers={"Authorization": key},
        cache_dir=cache_dir,
        cache_ttl_hours=cache_ttl_hours,
        query=query,
        page=page,
        limit=limit,
        stats=stats,
    )
    rows: list[dict[str, Any]] = []
    for video in payload.get("videos", []):
        selected = select_landscape_file(video.get("video_files", []))
        if not selected:
            continue
        source_id = required_text(video.get("id"))
        source_page_value = required_text(video.get("url"))
        creator = required_text((video.get("user") or {}).get("name"))
        if not source_id or not source_page_value or not creator:
            continue
        download_url = validate_https_url(
            str(selected.get("link")), DOWNLOAD_HOST_SUFFIXES["pexels"]
        )
        source_page = validate_https_url(source_page_value, ("pexels.com",))
        rows.append(
            {
                "provider": "pexels",
                "source_id": source_id,
                "source_page": source_page,
                "creator": creator,
                "_download_url": download_url,
                "api_width": int(selected.get("width") or 0),
                "api_height": int(selected.get("height") or 0),
                "api_duration_sec": float(video.get("duration") or 0),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def pixabay_search(
    query: str,
    page: int,
    limit: int,
    key: str,
    cache_dir: Path,
    cache_ttl_hours: int,
    stats: CacheStats,
) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "key": key,
            "q": query,
            "video_type": "all",
            "safesearch": "true",
            "page": page,
            "per_page": min(200, max(3, limit * 3)),
        }
    )
    payload = request_json(
        provider="pixabay",
        url=f"https://pixabay.com/api/videos/?{params}",
        headers=None,
        cache_dir=cache_dir,
        cache_ttl_hours=cache_ttl_hours,
        query=query,
        page=page,
        limit=limit,
        stats=stats,
    )
    rows: list[dict[str, Any]] = []
    for video in payload.get("hits", []):
        choices = video.get("videos") or {}
        selected = select_landscape_file(
            {**item, "file_type": "video/mp4", "link": item.get("url")}
            for item in choices.values()
            if item
        )
        if not selected:
            continue
        source_id = required_text(video.get("id"))
        source_page_value = required_text(video.get("pageURL"))
        creator = required_text(video.get("user"))
        if not source_id or not source_page_value or not creator:
            continue
        download_url = validate_https_url(
            str(selected.get("link")), DOWNLOAD_HOST_SUFFIXES["pixabay"]
        )
        source_page = validate_https_url(source_page_value, ("pixabay.com",))
        rows.append(
            {
                "provider": "pixabay",
                "source_id": source_id,
                "source_page": source_page,
                "creator": creator,
                "_download_url": download_url,
                "api_width": int(selected.get("width") or 0),
                "api_height": int(selected.get("height") or 0),
                "api_duration_sec": float(video.get("duration") or 0),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def load_catalog(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def catalog_matches(
    rows: list[dict[str, Any]], query: str, limit: int = 8
) -> list[dict[str, Any]]:
    tokens = {
        token for token in re.findall(r"[a-z0-9]+", query.lower()) if len(token) >= 3
    }
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        haystack = " ".join(
            str(row.get(key) or "")
            for key in ("tags", "subject", "description", "path", "file", "query")
        ).lower()
        score = sum(token in haystack for token in tokens)
        path_value = row.get("path") or row.get("file")
        known_licence = str(row.get("license") or "").lower() not in {"", "unknown"}
        if score and path_value and Path(str(path_value)).is_file() and known_licence:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in scored[:limit]]


def rights_complete(row: dict[str, Any]) -> bool:
    return all(
        required_text(row.get(key))
        for key in (
            "provider",
            "source_id",
            "source_page",
            "creator",
            "license",
            "license_url",
        )
    )


def ensure_within_root(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_parent = path.parent.resolve()
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise CollectorError("output target escapes the configured output directory")
    return path


def download(
    url: str,
    path: Path,
    provider: str,
    max_file_bytes: int,
    output_root: Path,
) -> str:
    validate_https_url(url, DOWNLOAD_HOST_SUFFIXES[provider])
    ensure_within_root(path, output_root)
    ensure_private_directory(path.parent)
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = build_opener(DOWNLOAD_HOST_SUFFIXES[provider])
    temporary = tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".download-", suffix=".part", delete=False
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        with opener.open(request, timeout=300) as response:
            validate_https_url(response.geturl(), DOWNLOAD_HOST_SUFFIXES[provider])
            declared = int(response.headers.get("Content-Length") or 0)
            if declared and declared > max_file_bytes:
                raise CollectorError("download exceeds the configured size limit")
            with temporary_path.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_file_bytes:
                        raise CollectorError("download exceeds the configured size limit")
                    digest.update(chunk)
                    handle.write(chunk)
        if total == 0:
            raise CollectorError("provider returned an empty download")
        if os.name == "posix":
            temporary_path.chmod(0o600)
        temporary_path.replace(path)
        return digest.hexdigest()
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate:format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    if not payload.get("streams"):
        raise CollectorError("download has no video stream")
    stream = payload["streams"][0]
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": stream["r_frame_rate"],
        "duration_sec": round(float(payload["format"]["duration"]), 3),
    }


def make_frames(
    video: Path,
    frame_dir: Path,
    stem: str,
    duration: float,
    output_root: Path,
) -> list[Path]:
    ensure_within_root(frame_dir, output_root)
    ensure_private_directory(frame_dir)
    frames: list[Path] = []
    try:
        for label, fraction in (("start", 0.2), ("middle", 0.5), ("end", 0.8)):
            frame = ensure_within_root(
                frame_dir / f"{stem}-{label}.jpg", output_root
            )
            with tempfile.NamedTemporaryFile(
                dir=frame_dir, prefix=".frame-", suffix=".jpg", delete=False
            ) as handle:
                temporary_frame = Path(handle.name)
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-ss",
                        f"{max(0.2, duration * fraction):.3f}",
                        "-i",
                        str(video),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=640:-2",
                        str(temporary_frame),
                    ],
                    check=True,
                )
                if os.name == "posix":
                    temporary_frame.chmod(0o600)
                temporary_frame.replace(frame)
            finally:
                if temporary_frame.exists():
                    temporary_frame.unlink()
            frames.append(frame)
        return frames
    except (subprocess.SubprocessError, OSError):
        for frame in frames:
            if frame.is_symlink() or frame.is_file():
                frame.unlink()
        raise


def dhash(path: Path) -> str:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("L").resize(
            (9, 8), Image.Resampling.LANCZOS
        )
    pixels = list(image.get_flattened_data())
    value = 0
    bit = 0
    for row in range(8):
        for column in range(8):
            offset = row * 9 + column
            if pixels[offset] > pixels[offset + 1]:
                value |= 1 << bit
            bit += 1
    return f"{value:016x}"


def hamming_distance(left: str, right: str) -> int:
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except (TypeError, ValueError):
        return 65


def normalize_hash_sets(row: dict[str, Any]) -> list[list[str]]:
    values = row.get("dhashes")
    if isinstance(values, list) and values:
        return [[str(item) for item in values]]
    value = row.get("dhash") or row.get("phash")
    return [[str(value)]] if value else []


def visually_near_duplicate(
    candidate: list[str],
    existing: Iterable[list[str]],
    threshold: int,
) -> bool:
    for other in existing:
        if not other:
            continue
        if len(candidate) >= 2 and len(other) >= 2:
            comparisons = zip(candidate, other)
            matches = sum(
                hamming_distance(left, right) <= threshold
                for left, right in comparisons
            )
            if matches >= min(2, len(candidate), len(other)):
                return True
        elif any(
            hamming_distance(left, right) <= threshold
            for left in candidate
            for right in other
        ):
            return True
    return False


def public_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if not key.startswith("_") and not SECRET_NAME_PATTERN.search(key)
    }


def remove_generated(paths: Iterable[Path], root: Path) -> int:
    removed = 0
    resolved_root = root.resolve()
    for path in paths:
        resolved_parent = path.parent.resolve()
        if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
            raise CollectorError(
                "refusing to remove a generated file outside the output directory"
            )
        if path.is_symlink() or path.is_file():
            path.unlink()
            removed += 1
    return removed


def contact_sheet(rows: list[dict[str, Any]], output: Path) -> None:
    if not rows:
        return
    cell_w, cell_h, columns = 640, 420, 2
    height = ((len(rows) + columns - 1) // columns) * cell_h
    sheet = Image.new("RGB", (cell_w * columns, height), "#101512")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("Arial.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    for index, row in enumerate(rows):
        x, y = (index % columns) * cell_w, (index // columns) * cell_h
        frame_paths = [output.parent / name for name in row["review_frames"]]
        for frame_index, frame_path in enumerate(frame_paths):
            with Image.open(frame_path) as opened:
                still = ImageOps.fit(
                    opened.convert("RGB"), (202, 340), Image.Resampling.LANCZOS
                )
            sheet.paste(still, (x + 8 + frame_index * 210, y + 8))
        draw.text(
            (x + 10, y + 354),
            f"{index + 1:02d} {row['provider']} {row['source_id']}",
            font=font,
            fill="#f2ead4",
        )
        draw.text(
            (x + 10, y + 382), row["query"][:62], font=font, fill="#d7b55b"
        )
    with tempfile.NamedTemporaryFile(
        dir=output.parent, prefix=".sheet-", suffix=".jpg", delete=False
    ) as handle:
        temporary_sheet = Path(handle.name)
    try:
        sheet.save(temporary_sheet, quality=88)
        if os.name == "posix":
            temporary_sheet.chmod(0o600)
        temporary_sheet.replace(output)
    finally:
        if temporary_sheet.exists():
            temporary_sheet.unlink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect rights-receipted footage candidates"
    )
    parser.add_argument("--query", action="append", required=True)
    parser.add_argument(
        "--provider", choices=("pexels", "pixabay", "all"), default="pexels"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--cache-ttl-hours", type=int, default=DEFAULT_CACHE_TTL_HOURS)
    parser.add_argument("--limit-per-query", type=int, default=3)
    parser.add_argument("--max-downloads", type=int, default=6)
    parser.add_argument("--max-file-mb", type=int, default=DEFAULT_MAX_FILE_MB)
    parser.add_argument("--page-start", type=int, default=1)
    parser.add_argument("--dhash-threshold", type=int, default=6)
    parser.add_argument("--download", action="store_true")
    parser.add_argument(
        "--force-external",
        action="store_true",
        help="search providers even when valid local matches exist",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.limit_per_query <= 8:
        raise CollectorError("--limit-per-query must stay within 1..8")
    if not 1 <= args.max_downloads <= 24:
        raise CollectorError("--max-downloads must stay within 1..24")
    if not 1 <= args.cache_ttl_hours <= 168:
        raise CollectorError("--cache-ttl-hours must stay within 1..168")
    if not 1 <= args.max_file_mb <= 4096:
        raise CollectorError("--max-file-mb must stay within 1..4096")
    if not 0 <= args.dhash_threshold <= 16:
        raise CollectorError("--dhash-threshold must stay within 0..16")

    queries = list(dict.fromkeys(validate_query(value) for value in args.query))
    if len(queries) > 50:
        raise CollectorError("no more than 50 unique queries are allowed per run")
    out = args.out.expanduser().resolve()
    if out == Path(out.anchor) or out == Path.home().resolve():
        raise CollectorError("refusing a broad output directory")
    ensure_private_directory(out)
    clips_dir, frames_dir = out / "clips", out / "frames"
    cache_dir = (
        args.cache_dir.expanduser().resolve()
        if args.cache_dir
        else Path.home() / ".cache" / "rights-first-footage-collector"
    )
    ensure_private_directory(cache_dir)
    if args.download:
        ensure_private_directory(clips_dir)
        ensure_private_directory(frames_dir)

    env = load_env(args.env_file)
    catalog = load_catalog(args.catalog)
    local = {query: catalog_matches(catalog, query) for query in queries}
    unresolved = [query for query in queries if args.force_external or not local[query]]
    providers = ("pexels", "pixabay") if args.provider == "all" else (args.provider,)
    if "pixabay" in providers and any(len(query) > 100 for query in unresolved):
        raise CollectorError("Pixabay queries must be 100 characters or fewer")
    effective_cache_ttl_hours = (
        max(24, args.cache_ttl_hours)
        if "pixabay" in providers
        else args.cache_ttl_hours
    )
    required_keys = {"pexels": "PEXELS_API_KEY", "pixabay": "PIXABAY_API_KEY"}
    if unresolved:
        missing = [required_keys[item] for item in providers if not env.get(required_keys[item])]
        if missing:
            raise CollectorError("missing required environment variables: " + ", ".join(missing))

    catalog_source_ids = {
        (str(row.get("provider") or row.get("source") or "").lower(), str(row.get("source_id") or ""))
        for row in catalog
        if (row.get("provider") or row.get("source")) and row.get("source_id")
    }
    catalog_sha256 = {str(row.get("sha256")) for row in catalog if row.get("sha256")}
    existing_visuals = [hashes for row in catalog for hashes in normalize_hash_sets(row)]
    cache_stats = CacheStats()
    candidates: list[dict[str, Any]] = []
    seen_source_ids: set[tuple[str, str]] = set()

    for query_index, query in enumerate(unresolved):
        page = args.page_start + (query_index % 4)
        for provider in providers:
            search = pexels_search if provider == "pexels" else pixabay_search
            found = search(
                query,
                page,
                args.limit_per_query,
                env[required_keys[provider]],
                cache_dir,
                effective_cache_ttl_hours,
                cache_stats,
            )
            for row in found:
                identity = (row["provider"], row["source_id"])
                if identity in seen_source_ids:
                    continue
                seen_source_ids.add(identity)
                licence, licence_url = LICENSES[row["provider"]]
                candidate = {
                    **row,
                    "query": query,
                    "api_page": page,
                    "license": licence,
                    "license_url": licence_url,
                    "status": (
                        "REJECTED_CATALOG_SOURCE_DUPLICATE"
                        if identity in catalog_source_ids
                        else "CANDIDATE_NEEDS_REVIEW"
                    ),
                    "collected_at": utc_now(),
                }
                if rights_complete(candidate):
                    candidates.append(candidate)

    downloaded: list[dict[str, Any]] = []
    seen_sha256: set[str] = set()
    seen_visuals: list[list[str]] = []
    max_bytes = args.max_file_mb * 1024 * 1024
    download_attempts = 0
    rejected_files_removed = 0
    for index, row in enumerate(candidates):
        if not args.download or download_attempts >= args.max_downloads:
            break
        if row["status"] != "CANDIDATE_NEEDS_REVIEW":
            continue
        source_component = safe_source_component(str(row["source_id"]))
        stem = (
            f"{index + 1:02d}-{slugify(row['query'])}-"
            f"{row['provider']}-{source_component}"
        )
        video = ensure_within_root(clips_dir / f"{stem}.mp4", out)
        frames: list[Path] = []
        download_attempts += 1
        try:
            sha256 = download(
                row["_download_url"], video, row["provider"], max_bytes, out
            )
            media = probe(video)
            if (
                media["duration_sec"] < 4
                or media["width"] < 1920
                or media["height"] < 1080
                or media["width"] <= media["height"]
            ):
                row["status"] = "REJECTED_TECHNICAL"
                rejected_files_removed += remove_generated([video], out)
                row["rejected_files_removed"] = True
                continue
            frames = make_frames(
                video, frames_dir, stem, media["duration_sec"], out
            )
            visual_hashes = [dhash(frame) for frame in frames]
            duplicate = (
                sha256 in seen_sha256
                or sha256 in catalog_sha256
                or visually_near_duplicate(
                    visual_hashes,
                    [*seen_visuals, *existing_visuals],
                    args.dhash_threshold,
                )
            )
            row.update(
                {
                    **media,
                    "file": str(video.relative_to(out)),
                    "review_frames": [str(frame.relative_to(out)) for frame in frames],
                    "sha256": sha256,
                    "dhashes": visual_hashes,
                }
            )
            if duplicate:
                row["status"] = "REJECTED_DUPLICATE"
                rejected_files_removed += remove_generated([video, *frames], out)
                row.pop("file", None)
                row.pop("review_frames", None)
                row["rejected_files_removed"] = True
                continue
            seen_sha256.add(sha256)
            seen_visuals.append(visual_hashes)
            downloaded.append(row)
        except (CollectorError, subprocess.SubprocessError, OSError, ValueError) as error:
            row["status"] = "REJECTED_DOWNLOAD_OR_PROBE"
            row["error"] = safe_error(error)
            rejected_files_removed += remove_generated([video, *frames], out)
            row["rejected_files_removed"] = True

    manifest = out / "candidate_manifest.jsonl"
    public_rows = [public_candidate(row) for row in candidates]
    atomic_write(
        manifest,
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in public_rows).encode(
            "utf-8"
        ),
    )
    public_downloaded = [public_candidate(row) for row in downloaded]
    sheet_path = out / "contact_sheet.jpg"
    if public_downloaded:
        contact_sheet(public_downloaded, sheet_path)

    receipt = {
        "schema_version": 2,
        "status": "CANDIDATES_NEED_VISUAL_REVIEW",
        "providers": list(providers),
        "queries": queries,
        "local_catalog_matches": {query: len(rows) for query, rows in local.items()},
        "external_queries": len(unresolved),
        "candidates": len(candidates),
        "download_attempts": download_attempts,
        "downloaded": len(downloaded),
        "rejected_files_removed": rejected_files_removed,
        "cache_hits": cache_stats.hits,
        "cache_misses": cache_stats.misses,
        "cache_ttl_hours": effective_cache_ttl_hours,
        "rate_limit_remaining": cache_stats.rate_limit_remaining,
        "rate_limit_reset": cache_stats.rate_limit_reset,
        "manifest": manifest.name,
        "contact_sheet": sheet_path.name if public_downloaded else None,
        "catalog_modified": False,
        "download_urls_in_manifest": False,
        "private_cache_may_contain_provider_download_urls": True,
        "secrets_printed": False,
        "completed_at": utc_now(),
    }
    receipt_path = out / "RUN_RECEIPT.json"
    atomic_write(receipt_path, (json.dumps(receipt, indent=2) + "\n").encode("utf-8"))
    return receipt


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        receipt = run(args)
        print(json.dumps(receipt, indent=2))
        return 0
    except (CollectorError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"ERROR: {safe_error(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
