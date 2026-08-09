from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
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
        self.assertEqual(
            MODULE.validate_https_url("https://pixabay.com/api/", ("pixabay.com",)),
            "https://pixabay.com/api/",
        )
        with self.assertRaises(MODULE.CollectorError):
            MODULE.validate_https_url(
                "https://evilpixabay.com/api/", ("pixabay.com",)
            )

    def test_redirect_handler_blocks_before_contact_and_strips_authorization(self) -> None:
        handler = MODULE.AllowlistedRedirectHandler(MODULE.API_HOSTS)
        request = MODULE.urllib.request.Request(
            "https://api.pexels.com/v1/videos/search",
            headers={"Authorization": "secret"},
        )
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://pixabay.com/api/videos/",
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))
        relative = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "/v1/videos/next",
        )
        self.assertIsNotNone(relative)
        self.assertEqual(
            relative.full_url, "https://api.pexels.com/v1/videos/next"
        )
        self.assertEqual(relative.get_header("Authorization"), "secret")
        with self.assertRaises(MODULE.CollectorError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://evilpixabay.com/steal",
            )

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
            stats = MODULE.CacheStats(
                rate_limit_remaining={"pexels": 0},
                rate_limit_reset={"pexels": "tomorrow"},
            )
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

    def test_provider_source_ids_are_safe_filename_components(self) -> None:
        components = [
            MODULE.safe_source_component(value)
            for value in ("../../../../escape", r"..\..\escape", "a/b", "normal-42")
        ]
        for component in components:
            self.assertNotIn("/", component)
            self.assertNotIn("\\", component)
            self.assertNotIn("..", component)
        self.assertEqual(len(set(components)), len(components))

    def test_output_target_must_remain_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "output"
            root.mkdir()
            safe = root / "clips" / "clip.mp4"
            self.assertEqual(MODULE.ensure_within_root(safe, root), safe)
            with self.assertRaisesRegex(MODULE.CollectorError, "escapes"):
                MODULE.ensure_within_root(root.parent / "escape.mp4", root)

    @unittest.skipUnless(os.name == "posix", "POSIX permission and symlink test")
    def test_symlinked_output_directory_does_not_mutate_external_target(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "output"
            external = root / "external"
            output.mkdir()
            external.mkdir()
            external.chmod(0o755)
            (output / "clips").symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(MODULE.CollectorError, "symlinked"):
                MODULE.ensure_private_subdirectory(output / "clips", output)

            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o755)
            self.assertEqual(list(external.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permission and symlink test")
    def test_symlinked_top_level_output_is_rejected_without_external_writes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            external = root / "external"
            external.mkdir()
            external.chmod(0o755)
            output_link = root / "output-link"
            output_link.symlink_to(external, target_is_directory=True)
            args = Namespace(
                query=["stone arch"],
                provider="pexels",
                out=output_link,
                env_file=None,
                catalog=None,
                cache_dir=root / "cache",
                cache_ttl_hours=24,
                limit_per_query=1,
                max_downloads=1,
                max_file_mb=10,
                page_start=1,
                dhash_threshold=6,
                download=False,
                force_external=False,
            )

            with self.assertRaisesRegex(MODULE.CollectorError, "symlinked"):
                MODULE.run(args)

            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o755)
            self.assertEqual(list(external.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permission and symlink test")
    def test_symlinked_cache_directory_is_rejected_without_external_writes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            external = root / "external-cache"
            external.mkdir()
            external.chmod(0o755)
            cache_link = root / "cache-link"
            cache_link.symlink_to(external, target_is_directory=True)
            args = Namespace(
                query=["stone arch"],
                provider="pexels",
                out=root / "output",
                env_file=None,
                catalog=None,
                cache_dir=cache_link,
                cache_ttl_hours=24,
                limit_per_query=1,
                max_downloads=1,
                max_file_mb=10,
                page_start=1,
                dhash_threshold=6,
                download=False,
                force_external=False,
            )

            with self.assertRaisesRegex(MODULE.CollectorError, "symlinked"):
                MODULE.run(args)

            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o755)
            self.assertEqual(list(external.iterdir()), [])

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

    def test_pixabay_accepts_root_source_page_and_rejects_missing_rights(self) -> None:
        valid_payload = {
            "hits": [
                {
                    "id": 9,
                    "pageURL": "https://pixabay.com/videos/cathedral-9/",
                    "user": "Creator",
                    "duration": 12,
                    "videos": {
                        "large": {
                            "url": "https://cdn.pixabay.com/video/9.mp4",
                            "width": 1920,
                            "height": 1080,
                        }
                    },
                }
            ]
        }
        original = MODULE.request_json
        MODULE.request_json = lambda **_kwargs: valid_payload
        try:
            rows = MODULE.pixabay_search(
                "cathedral", 1, 1, "secret", Path("."), 24, MODULE.CacheStats()
            )
        finally:
            MODULE.request_json = original
        self.assertEqual(rows[0]["source_page"], "https://pixabay.com/videos/cathedral-9/")

        invalid_payload = {
            "hits": [
                {
                    "id": None,
                    "pageURL": "https://pixabay.com/videos/cathedral-9/",
                    "user": None,
                    "duration": 12,
                    "videos": valid_payload["hits"][0]["videos"],
                }
            ]
        }
        MODULE.request_json = lambda **_kwargs: invalid_payload
        try:
            rows = MODULE.pixabay_search(
                "cathedral", 1, 1, "secret", Path("."), 24, MODULE.CacheStats()
            )
        finally:
            MODULE.request_json = original
        self.assertEqual(rows, [])

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

    def test_pixabay_enforces_24_hour_cache_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            observed: list[int] = []
            original_search = MODULE.pixabay_search
            old_key = os.environ.get("PIXABAY_API_KEY")

            def fake_search(
                _query, _page, _limit, _key, _cache_dir, cache_ttl_hours, _stats
            ):
                observed.append(cache_ttl_hours)
                return []

            MODULE.pixabay_search = fake_search
            os.environ["PIXABAY_API_KEY"] = "test-key"
            try:
                args = Namespace(
                    query=["stone arch"],
                    provider="pixabay",
                    out=root / "out",
                    env_file=None,
                    catalog=None,
                    cache_dir=root / "cache",
                    cache_ttl_hours=1,
                    limit_per_query=1,
                    max_downloads=1,
                    max_file_mb=10,
                    page_start=1,
                    dhash_threshold=6,
                    download=False,
                    force_external=False,
                )
                receipt = MODULE.run(args)
            finally:
                MODULE.pixabay_search = original_search
                if old_key is None:
                    os.environ.pop("PIXABAY_API_KEY", None)
                else:
                    os.environ["PIXABAY_API_KEY"] = old_key
            self.assertEqual(observed, [24])
            self.assertEqual(receipt["cache_ttl_hours"], 24)

    def test_pixabay_rejects_queries_over_100_characters_before_search(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            called = False
            original_search = MODULE.pixabay_search
            old_key = os.environ.get("PIXABAY_API_KEY")

            def fake_search(*_args):
                nonlocal called
                called = True
                return []

            MODULE.pixabay_search = fake_search
            os.environ["PIXABAY_API_KEY"] = "test-key"
            try:
                args = Namespace(
                    query=["a" * 101],
                    provider="pixabay",
                    out=root / "out",
                    env_file=None,
                    catalog=None,
                    cache_dir=root / "cache",
                    cache_ttl_hours=24,
                    limit_per_query=1,
                    max_downloads=1,
                    max_file_mb=10,
                    page_start=1,
                    dhash_threshold=6,
                    download=False,
                    force_external=False,
                )
                with self.assertRaisesRegex(MODULE.CollectorError, "100 characters"):
                    MODULE.run(args)
            finally:
                MODULE.pixabay_search = original_search
                if old_key is None:
                    os.environ.pop("PIXABAY_API_KEY", None)
                else:
                    os.environ["PIXABAY_API_KEY"] = old_key
            self.assertFalse(called)

    def test_untrusted_source_id_cannot_escape_download_directory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            captured: list[tuple[Path, Path]] = []
            personal_root = "/" + "Users" + "/example/private"
            original_search = MODULE.pexels_search
            original_download = MODULE.download
            old_key = os.environ.get("PEXELS_API_KEY")

            def fake_search(*_args):
                return [
                    {
                        "provider": "pexels",
                        "source_id": "../../../../escape",
                        "source_page": "https://www.pexels.com/video/1/",
                        "creator": "Creator",
                        "_download_url": "https://videos.pexels.com/1.mp4",
                        "api_width": 1920,
                        "api_height": 1080,
                        "api_duration_sec": 8.0,
                    }
                ]

            def fake_download(_url, path, _provider, _max_bytes, output_root):
                captured.append((path, output_root))
                MODULE.ensure_within_root(path, output_root)
                raise subprocess.CalledProcessError(
                    1,
                    [
                        personal_root + "/ffmpeg",
                        personal_root + "/video.mp4",
                    ],
                )

            MODULE.pexels_search = fake_search
            MODULE.download = fake_download
            os.environ["PEXELS_API_KEY"] = "test-key"
            try:
                args = Namespace(
                    query=["stone arch"],
                    provider="pexels",
                    out=root / "out",
                    env_file=None,
                    catalog=None,
                    cache_dir=root / "cache",
                    cache_ttl_hours=24,
                    limit_per_query=1,
                    max_downloads=1,
                    max_file_mb=10,
                    page_start=1,
                    dhash_threshold=6,
                    download=True,
                    force_external=False,
                )
                MODULE.run(args)
            finally:
                MODULE.pexels_search = original_search
                MODULE.download = original_download
                if old_key is None:
                    os.environ.pop("PEXELS_API_KEY", None)
                else:
                    os.environ["PEXELS_API_KEY"] = old_key
            self.assertEqual(len(captured), 1)
            path, output_root = captured[0]
            self.assertEqual(path.parent, output_root / "clips")
            self.assertNotIn("/", path.name)
            self.assertNotIn("\\", path.name)
            self.assertNotIn("..", path.name)
            manifest = (root / "out" / "candidate_manifest.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(personal_root, manifest)
            self.assertIn("media command failed: CalledProcessError", manifest)

    def test_download_cap_counts_failed_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            attempts: list[str] = []
            original_search = MODULE.pexels_search
            original_download = MODULE.download
            old_key = os.environ.get("PEXELS_API_KEY")

            def fake_search(*_args):
                return [
                    {
                        "provider": "pexels",
                        "source_id": str(index),
                        "source_page": f"https://www.pexels.com/video/{index}/",
                        "creator": f"Creator {index}",
                        "_download_url": f"https://videos.pexels.com/{index}.mp4",
                        "api_width": 1920,
                        "api_height": 1080,
                        "api_duration_sec": 8.0,
                    }
                    for index in range(1, 4)
                ]

            def fake_download(url, *_args):
                attempts.append(url)
                raise MODULE.CollectorError("simulated failure")

            MODULE.pexels_search = fake_search
            MODULE.download = fake_download
            os.environ["PEXELS_API_KEY"] = "test-key"
            try:
                args = Namespace(
                    query=["stone arch"],
                    provider="pexels",
                    out=root / "out",
                    env_file=None,
                    catalog=None,
                    cache_dir=root / "cache",
                    cache_ttl_hours=24,
                    limit_per_query=3,
                    max_downloads=2,
                    max_file_mb=10,
                    page_start=1,
                    dhash_threshold=6,
                    download=True,
                    force_external=False,
                )
                receipt = MODULE.run(args)
            finally:
                MODULE.pexels_search = original_search
                MODULE.download = original_download
                if old_key is None:
                    os.environ.pop("PEXELS_API_KEY", None)
                else:
                    os.environ["PEXELS_API_KEY"] = old_key
            self.assertEqual(len(attempts), 2)
            self.assertEqual(receipt["download_attempts"], 2)
            self.assertEqual(receipt["downloaded"], 0)

    def test_rate_limit_exhaustion_stops_before_network(self) -> None:
        stats = MODULE.CacheStats(
            rate_limit_remaining={"pexels": 0},
            rate_limit_reset={"pexels": "tomorrow"},
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(MODULE.CollectorError, "rate limit exhausted"):
                MODULE.request_json(
                    provider="pexels",
                    url="https://api.pexels.com/v1/videos/search?query=arch",
                    headers={"Authorization": "secret"},
                    cache_dir=Path(folder),
                    cache_ttl_hours=24,
                    query="arch",
                    page=1,
                    limit=1,
                    stats=stats,
                )

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
            self.assertEqual(receipt["manifest"], "candidate_manifest.jsonl")
            self.assertIsNone(receipt["contact_sheet"])


if __name__ == "__main__":
    unittest.main()
