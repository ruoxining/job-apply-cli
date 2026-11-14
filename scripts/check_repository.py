#!/usr/bin/env python3
"""Check distribution metadata, the source allowlist and immutable Action references."""

import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(p for p in lock["package"] if p["name"] == metadata["name"])
    assert package["version"] == metadata["version"], "Distribution and lock version differ"
    source = (ROOT / "src/job_apply_agent/__init__.py").read_text()
    assert f'__version__ = "{metadata["version"]}"' in source
    manifest = {
        line
        for line in (ROOT / "public-files.txt").read_text().splitlines()
        if line and not line.startswith("#")
    }
    tracked = set(
        subprocess.check_output(
            ["git", "ls-files"],
            cwd=ROOT,
            text=True,
        ).splitlines()
    )
    assert tracked <= manifest, f"Tracked files missing from public allowlist: {tracked - manifest}"
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for action in re.findall(r"uses:\s*([^\s#]+)", path.read_text()):
            if action.startswith("./.github/workflows/"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[a-f0-9]{40}", action), action
    config = json.loads((ROOT / "release-please-config.json").read_text())
    assert config["packages"]["."]["package-name"] == metadata["name"]
    print("Repository metadata and automation checks passed.")


if __name__ == "__main__":
    main()
