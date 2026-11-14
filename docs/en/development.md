# Development and validation

English | [简体中文](../development.md)

```bash
uv sync --frozen --extra test --extra browser
uv run playwright install chromium
uv run pytest
```

Browser fixture tests require the `browser` extra and Playwright Chromium. When dependencies
are missing, distinguish skipped tests, environment failures, and logic failures. Tests use
fictional identities and local fixtures; do not copy real application data into the test directory.

Before adding functionality, inspect existing modules. Preserve submission claims, the rule
against resubmitting unknown outcomes, answer region and expiration scopes, and fact provenance
constraints. Keep the existing version chain when changing migrations. Personal model prompts
and experimental data belong in private files.

`public-files.txt` defines the export scope. Update it when adding files, then run:

```bash
python3 scripts/export_public.py --check
python3 scripts/export_public.py --output ../job-apply-agent-public
```

The output directory must not already exist. The check script does not run Git push or audit
the original repository's full history. When building distributions, inspect archive contents
to confirm that they contain only allowed source files and normal generated package metadata.

Retained experimental tools include synthetic browser fixtures and an experience replay tool
that accepts explicit inputs. They are not production deployment prerequisites. Scripts tied
to a private host, historical input paths, or old images have been removed from public entry points.

Unfinished product work includes complete interaction for unknown questions, long-term
unattended reliability, queue failure recovery, fuller source health metrics, email integration,
generic HTML parsing, and independent matching quality evaluation. Local unit tests do not
replace these outstanding integration checks.
