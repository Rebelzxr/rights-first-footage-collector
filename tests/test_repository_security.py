from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
COLLECTOR = (
    ROOT / "rights-first-footage-collector" / "scripts" / "collect.py"
)


class RepositorySecurityTests(unittest.TestCase):
    def test_no_personal_absolute_paths_in_tracked_text(self) -> None:
        blocked = ("/" + "Users" + "/", "DAINER" + " OS", "dainer" + "1992")
        for path in ROOT.rglob("*"):
            if (
                not path.is_file()
                or any(
                    part in {".git", "runtime", "__pycache__"}
                    or part.startswith(".venv")
                    for part in path.parts
                )
            ):
                continue
            if path.suffix.lower() in {".pyc", ".jpg", ".png", ".mp4"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in blocked:
                self.assertNotIn(marker, text, f"personal marker in {path}")

    def test_collector_uses_no_shell_eval_or_exec(self) -> None:
        tree = ast.parse(COLLECTOR.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"eval", "exec"})
                for keyword in node.keywords:
                    if keyword.arg == "shell":
                        self.assertNotEqual(
                            getattr(keyword.value, "value", None), True
                        )

    def test_no_http_provider_endpoints(self) -> None:
        text = COLLECTOR.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r'["\']http://', text))

    def test_gitignore_blocks_sensitive_runtime_outputs(self) -> None:
        patterns = set(
            (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        )
        for required in {
            ".env",
            ".env.*",
            "runtime/",
            "clips/",
            "frames/",
            "candidate_manifest.jsonl",
            "RUN_RECEIPT.json",
        }:
            self.assertIn(required, patterns)


if __name__ == "__main__":
    unittest.main()
