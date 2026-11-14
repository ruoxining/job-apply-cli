#!/usr/bin/env python3
"""Exercise the installed wheel through real, non-interactive CLI processes."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sysconfig
import tempfile
from importlib.metadata import version
from pathlib import Path


def main() -> None:
    # No source imports or inherited application credentials; run outside the checkout.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "LANG"}
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory(prefix="package smoke ") as directory:
        root = Path(directory)
        candidate = root / "candidate files"
        executable = Path(sysconfig.get_path("scripts")) / (
            "job-apply-cli.exe" if os.name == "nt" else "job-apply-cli"
        )

        def run(*arguments: str, expected: int = 0, extra: dict | None = None):
            result = subprocess.run(
                [str(executable), *arguments],
                cwd=root,
                env=environment | (extra or {}),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == expected, (arguments, result.returncode, result.stderr)
            return result

        assert "usage:" in run("--help").stdout
        assert run("--version").stdout.strip() == version("job-apply-cli")
        invalid = run("unknown-command", expected=2)
        assert not invalid.stdout and "usage:" in invalid.stderr
        initialized = json.loads(run("onboard", "init", "--directory", str(candidate)).stdout)
        assert len(initialized["created"]) == 5
        assert json.loads(run("onboard", "init", "--directory", str(candidate)).stdout) == {
            "created": []
        }
        database = root / "test database.sqlite3"
        mapping = {
            "PROFILE_PATH": candidate / "profile.json",
            "ANSWER_BANK_PATH": candidate / "answer-bank.json",
            "SEARCH_POLICY_PATH": candidate / "search-policy.json",
            "SOURCE_BOARDS_PATH": candidate / "source-boards.json",
            "EXPERIENCE_LIBRARY_PATH": candidate / "experiences.json",
        }
        (root / ".env").write_text(
            "\n".join(f'{key}="{path.as_posix()}"' for key, path in mapping.items())
            + f'\nDATABASE_URL="sqlite:///{database.as_posix()}"\nALLOW_LIVE_SUBMIT=false\n',
            encoding="utf-8",
        )
        unready = run("onboard", "check", expected=1)
        assert not unready.stderr and json.loads(unready.stdout)["ready"] is False
        profile = {
            "allowed_countries": ["Canada"],
            "facts": [
                {"id": "f1", "statement": "Built a Python fixture.", "source": "synthetic test"}
            ],
        }
        mapping["PROFILE_PATH"].write_text(json.dumps(profile), encoding="utf-8")
        mapping["SOURCE_BOARDS_PATH"].write_text(
            '[{"ats":"lever","board_slug":"fixture"}]', encoding="utf-8"
        )
        mapping["EXPERIENCE_LIBRARY_PATH"].write_text('{"projects":[]}', encoding="utf-8")
        entries = [
            {"semantic_key": "first_name", "answer": "Alex"},
            {"semantic_key": "last_name", "answer": "Example"},
            {"semantic_key": "email", "answer": "alex@example.com"},
        ]
        mapping["ANSWER_BANK_PATH"].write_text(json.dumps(entries), encoding="utf-8")
        from docx import Document

        resume = root / "synthetic resume.docx"
        document = Document()
        document.add_paragraph("Synthetic packaging test. No real applicant.")
        document.save(str(resume))
        environment["RESUME_FILE"] = str(resume)
        ready = run("onboard", "check")
        assert not ready.stderr
        result = json.loads(ready.stdout)
        assert result["ready"] is True and result["mode"] == "shadow"
        assert {"ready", "mode", "counts", "errors", "limits"} <= result.keys()
        # Environment JSON takes precedence over the file; a malformed file must not leak.
        mapping["PROFILE_PATH"].write_text("broken file", encoding="utf-8")
        assert json.loads(
            run("onboard", "check", extra={"PROFILE_JSON": json.dumps(profile)}).stdout
        )["ready"]
        run("init-db")
        for _ in range(2):
            imported = run("onboard", "import-answers")
            assert not imported.stderr and json.loads(imported.stdout) == {"imported": 3}
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM answer_bank").fetchone()[0] == 3
        report = run("application-report")
        assert not report.stderr and isinstance(json.loads(report.stdout), dict)
        mapping["ANSWER_BANK_PATH"].write_text("broken file", encoding="utf-8")
        failed = run("onboard", "import-answers", expected=1)
        assert not failed.stdout and "no answers imported" in failed.stderr
        assert "Traceback" not in failed.stderr
    print("Installed-wheel CLI checks passed.")


if __name__ == "__main__":
    main()
