"""Offline checks that scanner diagnostics never include matched file contents."""

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import secret_scan


def detection_cases():
    """Construct synthetic matches, not usable credentials or stored secrets."""
    return [
        ("private key", "-----BEGIN " + "PRIVATE KEY-----"),
        ("OpenAI key", "sk-" + "A" * 24),
        ("GitHub token", "ghp_" + "B" * 24),
        ("AWS access key", "AKIA" + "C" * 16),
        ("Slack token", "xoxb-" + "D" * 24),
        ("Bearer token", "Bearer " + "E" * 32),
    ]


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "fixture.env"
        self.root_patch = patch.object(secret_scan, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_each_detection_rule_returns_only_location_and_category(self):
        for label, value in detection_cases():
            with self.subTest(category=label):
                self.path.write_text(f"normal line\nbefore {value} after\n")
                self.assertEqual(secret_scan.scan_path(self.path), [f"fixture.env:2: possible {label}"])

    def test_assignment_variants_and_placeholders(self):
        for field in ("api_key", "api-key", "secret", "token", "password", "passwd", "pwd", "access_token"):
            for separator in ("=", ":"):
                for quote in ("", "'", '"'):
                    with self.subTest(field=field, separator=separator, quote=quote):
                        self.path.write_text(f"{field} {separator} {quote}{'F' * 16}{quote}\n")
                        self.assertEqual(secret_scan.scan_path(self.path), ["fixture.env:1: possible credential assignment"])
        for value, expected in [("G" * 9, []), ("G" * 10, ["fixture.env:1: possible credential assignment"]),
                                ("your_api_key", []), ("${API_KEY}", []), ("<redacted>", [])]:
            with self.subTest(value_kind="boundary or placeholder"):
                self.path.write_text(f"{'password'} = {value}\n")
                self.assertEqual(secret_scan.scan_path(self.path), expected)

    def test_multiline_diagnostics_keep_order_without_payloads(self):
        first, second = detection_cases()[1:3]
        self.path.write_text(f"# {first[1]}\nnormal\n{first[1]} {second[1]}\n{'token'} = {second[1]}\n")
        self.assertEqual(secret_scan.scan_path(self.path), [
            "fixture.env:3: possible OpenAI key",
            "fixture.env:3: possible GitHub token",
            "fixture.env:4: possible GitHub token",
            "fixture.env:4: possible credential assignment",
        ])

    def test_binary_non_utf8_and_unreadable_files_are_unchanged(self):
        for content in (b"\x00binary", b"\xff\xfe"):
            self.path.write_bytes(content)
            self.assertEqual(secret_scan.scan_path(self.path), [])
        with patch.object(Path, "read_bytes", side_effect=OSError("fixture unavailable")):
            self.assertEqual(secret_scan.scan_path(self.path), [])

    def test_cli_modes_fail_without_disclosing_payloads(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        scanner = scripts / "secret_scan.py"
        shutil.copyfile(Path(secret_scan.__file__), scanner)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)
        values = [value for _, value in detection_cases()]
        self.path.write_text("\n".join(f"before {value} after" for value in values) + "\n")
        subprocess.run(["git", "-C", str(self.root), "add", "fixture.env"], check=True, capture_output=True)
        for flags in ([], ["--staged"]):
            with self.subTest(mode=flags):
                result = subprocess.run([sys.executable, str(scanner), *flags], cwd=self.root,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                expected = "Secret scan failed:\n" + "".join(
                    f"  - fixture.env:{line}: possible {label}\n"
                    for line, (label, _) in enumerate(detection_cases(), 1)
                )
                self.assertEqual(result.stderr, expected)
                for value in values:
                    self.assertNotIn(value, result.stdout + result.stderr)

    def test_cli_success_and_mode_selection(self):
        self.path.write_text("normal text\n")
        for flags in ([], ["--staged"]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", ["secret_scan.py", *flags]), \
                    patch.object(secret_scan.os, "chdir"), \
                    patch.object(secret_scan, "staged_paths", return_value=[self.path]) as staged, \
                    patch.object(secret_scan, "repository_paths", return_value=[self.path]) as repository, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(secret_scan.main(), 0)
            self.assertEqual(stdout.getvalue(), "Secret scan passed.\n")
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(staged.call_count, int(bool(flags)))
            self.assertEqual(repository.call_count, int(not flags))


if __name__ == "__main__":
    unittest.main()
