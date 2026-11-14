#!/usr/bin/env python3
"""Install the pinned PR-Agent with the documented OpenTelemetry security patch."""

import argparse
import subprocess
import tempfile
from pathlib import Path

REVISION = "e49163c25383184f87c90bd99d983eb2c9a3349f"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="reviewer-source-") as directory:
        source = Path(directory)
        for command in (
            ["git", "init", "--quiet", str(source)],
            [
                "git",
                "-C",
                str(source),
                "remote",
                "add",
                "origin",
                "https://github.com/The-PR-Agent/pr-agent.git",
            ],
            ["git", "-C", str(source), "fetch", "--quiet", "--depth=1", "origin", REVISION],
            ["git", "-C", str(source), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        ):
            subprocess.run(command, check=True, timeout=180)
        actual = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        if actual != REVISION:
            raise ValueError("Unexpected reviewer revision")
        # Upstream pins vulnerable 1.45.1. Patch metadata coherently, not an incompatible
        # --no-deps override. Revalidate this four-pin patch when updating upstream.
        metadata = source / "pyproject.toml"
        text = metadata.read_text()
        packages = ("api", "sdk", "exporter-otlp-proto-http", "exporter-otlp-proto-grpc")
        for package in packages:
            old = f'"opentelemetry-{package}==1.45.1"'
            if text.count(old) != 1:
                raise ValueError("Upstream dependency patch no longer matches")
            text = text.replace(old, f'"opentelemetry-{package}==1.45.2"')
        metadata.write_text(text)
        python = args.venv / "bin/python"
        subprocess.run(["uv", "venv", "--python", "3.12", str(args.venv)], check=True, timeout=180)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(source)], check=True, timeout=300
        )
        subprocess.run(["uv", "pip", "check", "--python", str(python)], check=True, timeout=30)


if __name__ == "__main__":
    main()
