#!/usr/bin/env python3
"""Audit and export explicitly listed source files, never Git history or private data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[A-Za-z0-9_.+%-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "access_token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{25,}|sk-proj-[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})\b"
    ),
    "telegram_token": re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b"),
    "personal_home": re.compile(
        r"/(?:Users|home)/(?!appuser(?:/|\b)|example(?:/|\b))[A-Za-z0-9_.-]+/"
    ),
}
BLOCKED_PARTS = {
    ".git",
    "private",
    "artifacts",
    "browser-profile",
    ".venv",
    "node_modules",
    "__pycache__",
}
BLOCKED_SUFFIXES = {".pdf", ".docx", ".sqlite3", ".db", ".pem", ".key", ".log", ".zip", ".gz"}


def manifest_paths(root: Path) -> list[Path]:
    entries = [
        line.strip()
        for line in (root / "public-files.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if len(entries) != len(set(entries)):
        raise ValueError("duplicate public manifest entries")
    result = []
    for name in entries:
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or set(relative.parts) & BLOCKED_PARTS:
            raise ValueError("unsafe manifest path")
        if relative.suffix in BLOCKED_SUFFIXES or (
            relative.name.startswith(".env") and name != ".env.example"
        ):
            raise ValueError("private file type in public manifest")
        path = root / name
        # Refuse symlinks anywhere in the path, even ones currently pointing inside the tree.
        if any(
            (root / Path(*relative.parts[:i])).is_symlink()
            for i in range(1, len(relative.parts) + 1)
        ):
            raise ValueError("symlinks cannot be exported")
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("missing or external manifest file")
        result.append(path)
    if root / "public-files.txt" not in result:
        raise ValueError("public manifest must include itself")
    return result


def audit(root: Path, paths: list[Path], markers: list[str] = ()) -> list[dict]:
    """Return locations and rule names only; never echo matched secret values."""
    findings = []
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            findings.append({"file": str(path.relative_to(root)), "rule": "unexpected_binary"})
            continue
        for number, line in enumerate(content.splitlines(), 1):
            rules = [label for label, pattern in PATTERNS.items() if pattern.search(line)]
            for match in EMAIL.finditer(line):
                domain = match.group(1).lower()
                if not (
                    domain in {"example.com", "example.org", "example.net"}
                    or domain.endswith((".test", ".invalid", ".example"))
                ):
                    rules.append("non_example_email")
            if any(marker and marker.casefold() in line.casefold() for marker in markers):
                rules.append("local_private_marker")
            for rule in sorted(set(rules)):
                findings.append({"file": str(path.relative_to(root)), "line": number, "rule": rule})
    return findings


def export(root: Path, output: Path, paths: list[Path], markers: list[str]) -> dict:
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory")
    if output.resolve() == root.resolve() or root.resolve().is_relative_to(output.resolve()):
        raise ValueError("output cannot contain the source tree")
    findings = audit(root, paths, markers)
    if findings:
        raise ValueError("privacy audit failed; run --check for locations")
    output.mkdir(parents=True)
    hashes = {}
    for path in paths:
        relative = path.relative_to(root)
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        destination.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
        hashes[str(relative)] = hashlib.sha256(destination.read_bytes()).hexdigest()
    # Re-read the export itself, including its own manifest, before claiming success.
    assert not audit(output, manifest_paths(output), markers)
    return {"files": len(paths), "sha256": hashes, "git_history_included": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--markers", type=Path, help="local JSON list of private strings to reject")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        paths = manifest_paths(root)
        marker_file = args.markers or root / "private/open-source-prep/privacy-markers.json"
        markers = json.loads(marker_file.read_text()) if marker_file.is_file() else []
        if not isinstance(markers, list) or not all(isinstance(m, str) for m in markers):
            raise ValueError("marker file must be a JSON list of strings")
        if args.markers and not args.markers.is_file():
            raise ValueError("requested marker file is missing")
        findings = audit(root, paths, markers)
        if findings:
            print(json.dumps({"passed": False, "findings": findings}, indent=2))
            return 1
        if args.output:
            result = export(root, args.output, paths, markers)
            print(json.dumps(result, indent=2))
        else:
            print(
                json.dumps(
                    {"passed": True, "files": len(paths), "private_marker_count": len(markers)}
                )
            )
        return 0
    except (OSError, ValueError) as exc:
        # All custom errors are fixed messages. OSError may contain a private path.
        print(
            str(exc) if isinstance(exc, ValueError) else "Cannot read or write a required file.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
