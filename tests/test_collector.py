from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
from argparse import Namespace
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1]
    / "rights-first-footage-collector"
    / "scripts"
    / "collect.py"
)
SPEC = importlib.util.spec_from_file_location("footage_collect", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CollectorTests(unittest.TestCase):
    def test_slugify_and_query_validation(self) -> None:
        self.assertEqual(
            MODULE.slugify("Ancient Temple: Mist & Stone"),
            "ancient-temple-mist-stone",
        )
        self.assertEqual(MODULE.validate_query("  stone   arch  "), "stone arch")
        with self.assertRaises(MODULE.CollectorError):
            MODULE.validate_query("\x01unsafe")

    def test_env_parser_prefers_process_and_requires_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("PEXELS_API_KEY=file-value\n", encoding="utf-8")
            path.chmod(0o600)
            old_value = os.environ.get("PEXELS_API_KEY")
            os.environ["PEXELS_API_KEY"] = "process-value"
            try:
                values = MODULE.load_env(path)
            finally:
                if old_value is None:
                    os.environ.pop("PEXELS_API_KEY", None)
                else:
                    os.environ["PEXELS_API_KEY"] = old_value
            self.assertEqual(values["PEXELS_API_KEY"], "process-value")
            if os.name == "posix":
                path.chmod(0o644)
                with self.assertRaises(MODULE.CollectorError):
                    MODULE.load_env(path)

    def test_safe_error_redacts_urls_and_secret_values(self) -> None:
        message = MODULE.safe_error(
            RuntimeError(
                "token=abc123 failed at https://pixabay.com/api/?key=abc123"
            )
        )
        self.assertNotIn("abc123", message)
        self.assertNotIn("https://", message)
        self.assertIn("[REDACTED]", message)

    def test_safe_http_error_omits_url(self) -> None:
        error = urllib.error.HTTPError(
            "https://pixabay.com/api/?key=secret", 429, "rate", {}, None
        )
        self.assertEqual(
            MODULE.safe_error(error), "provider request failed with HTTP 429"
        )

    def test_https_host_allowlist(self) -> None:
        self.assertEqual(
            MODULE.validate_https_url(
                "https://videos.pexels.com/video.mp4", (".pexels.com",)
            ),
            "https://videos.pexels.com/video.mp4",
        )
        for url in (
            "http://videos.pexels.com/video.mp4",
            "https://pexels.com.evil.test/video.mp4",
            "file:///tmp/video.mp4",
        ):
            with self.assertRaises(MODULE.CollectorError):
                MODULE.validate_https_url(url, (".pexels.com",))

    def test_cache_key_contains_no_api_key_input(self) -> None:
        value = MODULE.cache_key("pixabay", "stone arch", 1, 3)
        self.assertEqual(len(value), 64)
        self.assertNotIn("stone", value)

    def test_request_json_reads_fresh_private_cache(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_dir = Path(folder)
            key = MODULE.cache_key("pexels", "temple", 1, 3)
            cache_path = cache_dir / f"{key}.json"
            cache_path.write_text('{"videos": []}', encoding="utf-8")
            stats = MODULE.CacheStats()
            payload = MODULE.request_json(
                provider="pexels",
                url="https://api.pexels.com/v1/videos/search?query=temple",
                headers={"Authorization": "secret"},
                cache_dir=cache_dir,
                cache_ttl_hours=24,
                query="temple",
                page=1,
                limit=3,
                stats=stats,
            )
            self.assertEqual(payload, {"videos": []})
            self.assertEqual(stats.hits, 1)
            self.assertEqual(stats.misses, 0)

    def test_expired_cache_is_not_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "cache.json"
            cache_path.write_text("{}", encoding="utf-8")
            old = time.time() - 25 * 3600
            os.utime(cache_path, (old, old))
            self.assertGreater(time.time() - cache_path.stat().st_mtime, 24 * 3600)

    def test_select_landscape_file_picks_highest_resolution(self) -> None:
        selected = MODULE.select_landscape_file(
            [
                {
                    "file_type": "video/mp4",
                    "width": 1080,
                    "height": 1920,
                    "link": "portrait",
                },
                {
                    "file_type": "video/mp4",
                    "width": 1920,
                    "height": 1080,
                    "link": "hd",
                },
                {
                    "file_type": "video/mp4",
                    "width": 3840,
                    "height": 2160,
                    "link": "uhd",
                },
            ]
        )
        self.assertEqual(selected["link"], "uhd")

    def test_hamming_distance_and_near_duplicate_threshold(self) -> None:
        self.assertEqual(MODULE.hamming_distance("0", "f"), 4)
        candidate = ["0000000000000000", "0000000000000000", "ffffffffffffffff"]
        close = ["0000000000000001", "0000000000000003", "0000000000000000"]
        far = ["ffffffffffffffff"] * 3
        self.assertTrue(MODULE.visually_near_duplicate(candidate, [close], 2))
        self.assertFalse(MODULE.visually_near_duplicate(candidate, [far], 2))

    def test_public_candidate_strips_private_and_secret_fields(self) -> None:
        public = MODULE.public_candidate(
            {
                "provider": "pixabay",
                "source_page": "https://pixabay.com/videos/id-1/",
                "_download_url": "https://cdn.pixabay.com/private.mp4",
                "api_key": "secret",
                "authorization": "secret",
            }
        )
        self.assertEqual(
            public,
            {
                "provider": "pixabay",
                "source_page": "https://pixabay.com/videos/id-1/",
            },
        )

    def test_catalog_requires_existing_file_and_known_license(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "temple.mp4"
            clip.touch()
            rows = [
                {
                    "path": str(clip),
                    "tags": ["ancient", "temple"],
                    "license": "Pexels License",
                },
                {
                    "path": str(clip),
                    "tags": ["ancient", "temple"],
                    "license": "unknown",
                },
            ]
            self.assertEqual(len(MODULE.catalog_matches(rows, "ancient temple")), 1)

    def test_atomic_write_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "receipt.json"
            MODULE.atomic_write(path, b"{}")
            self.assertEqual(path.read_bytes(), b"{}")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_catalog_only_run_needs_no_api_key_and_persists_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clip = root / "temple.mp4"
            clip.touch()
            catalog = root / "catalog.jsonl"
            catalog.write_text(
                json.dumps(
                    {
                        "path": str(clip),
                        "tags": ["ancient", "temple"],
                        "license": "Pexels License",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = Namespace(
                query=["ancient temple"],
                provider="pexels",
                out=root / "out",
                env_file=None,
                catalog=catalog,
                cache_dir=root / "cache",
                cache_ttl_hours=24,
                limit_per_query=3,
                max_downloads=6,
                max_file_mb=10,
                page_start=1,
                dhash_threshold=6,
                download=False,
                force_external=False,
            )
            receipt = MODULE.run(args)
            self.assertEqual(receipt["external_queries"], 0)
            self.assertEqual(receipt["local_catalog_matches"]["ancient temple"], 1)
            output = (root / "out" / "RUN_RECEIPT.json").read_text(encoding="utf-8")
            self.assertNotIn("API_KEY", output)
            self.assertFalse(receipt["download_urls_in_manifest"])


if __name__ == "__main__":
    unittest.main()
