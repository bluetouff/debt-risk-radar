#!/usr/bin/env python3
"""High-signal secret scan for commits.

This intentionally avoids noisy dependency scans and focuses on staged text files.
It is not a replacement for server-side GitHub secret scanning, but it catches the
most common local mistakes before they enter history.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

TEXT_EXTENSIONS = {
    "",
    ".conf",
    ".css",
    ".env",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("Bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{24,}\b")),
]

CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?token)\b"
    r"\s*[:=]\s*['\"]?([^'\"\s#]+)"
)

PLACEHOLDER_VALUES = {
    "",
    "changeme",
    "change_me",
    "example",
    "placeholder",
    "redacted",
    "[redacted]",
    "<redacted>",
    "none",
    "null",
}

PLACEHOLDER_FRAGMENTS = (
    "ta_cle",
    "your_",
    "example",
    "placeholder",
    "redacted",
    "missing",
    "xxxx",
)


def run_git(args: list[str]) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def staged_paths() -> list[Path]:
    raw = run_git(["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"])
    return [ROOT / item.decode() for item in raw.split(b"\0") if item]


def repository_paths() -> list[Path]:
    tracked = run_git(["ls-files", "-z"]).split(b"\0")
    untracked = run_git(["ls-files", "--others", "--exclude-standard", "-z"]).split(b"\0")
    return [ROOT / item.decode() for item in tracked + untracked if item]


def should_scan(path: Path) -> bool:
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        return False
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if not path.is_file():
        return False
    suffixes = path.suffixes
    suffix = suffixes[-1] if suffixes else ""
    return suffix in TEXT_EXTENSIONS or path.name in {".gitignore", "pre-commit"}


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().strip("'\"").lower()
    if normalized in PLACEHOLDER_VALUES:
        return True
    if normalized.startswith(("<", "${", "$", "{")):
        return True
    return any(fragment in normalized for fragment in PLACEHOLDER_FRAGMENTS)


def scan_path(path: Path) -> list[str]:
    text = read_text(path)
    if text is None:
        return []

    findings: list[str] = []
    relative = path.relative_to(ROOT)
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(f"{relative}:{line_number}: possible {label}")
        for match in CREDENTIAL_ASSIGNMENT.finditer(line):
            value = match.group(2)
            if len(value) >= 10 and not looks_like_placeholder(value):
                findings.append(f"{relative}:{line_number}: possible credential assignment")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="scan staged files only")
    args = parser.parse_args()

    os.chdir(ROOT)
    paths = staged_paths() if args.staged else repository_paths()
    findings: list[str] = []
    for path in paths:
        if should_scan(path):
            findings.extend(scan_path(path))

    if findings:
        print("Secret scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1

    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
