# Job Apply Agent

English | [简体中文](README.md)

A tool for collecting and filtering job postings, preparing application materials, and
coordinating applications through Greenhouse, Lever, and Ashby. Candidate data is stored
separately from the code. Live submission is disabled by default; Telegram and model
services are also opt-in.

The project is experimental. It has been used for supervised applications, but that does
not establish reliability for unattended operation. Browser connections, required questions,
and site-specific rules may still need human attention. When a submission outcome is unclear,
keep the record and verify the result before retrying.

## Getting started

Requires Python 3.12+ and uv. No personal resume repository or Git submodule is required.

```bash
uv sync --frozen --extra test
cp .env.example .env
uv run job-apply-agent onboard init
```

Initialization creates local templates in `private/candidate/`. It does not upload files,
call models, collect jobs, or submit applications. Follow the [onboarding guide](docs/en/onboarding.md)
to enter your information and provide a reviewed PDF or DOCX, then run:

```bash
uv run job-apply-agent onboard check
uv run job-apply-agent init-db
uv run job-apply-agent onboard import-answers
uv run job-apply-agent serve
```

Templates contain placeholders, so checks are expected to fail until you fill them in.
By default, the SQLite database, experience data, browser state, and generated output live
under `private/`, which Git ignores. The API listens on `127.0.0.1:8000` by default.

## What to prepare

| Input | Local location | Purpose |
| --- | --- | --- |
| A reviewed, complete resume | `private/candidate/resume.pdf` or `.docx` | Application attachment; set `RESUME_FILE` |
| Verifiable experience, skills, qualifications, and job preferences | `private/candidate/profile.json` | Job filtering and answers grounded in facts |
| Name, contact details, and confirmed form answers | `private/candidate/answer-bank.json` | Form completion, with region and expiration limits on reuse |
| Company ATS sources and search filters | `private/candidate/source-boards.json` | Explicit collection scope; examples are disabled by default |
| Priorities, exclusions, and review rules | `private/candidate/search-policy.json` | Explainable ranking and human review |
| Detailed experience data (optional) | `private/candidate/experiences.json` | Experimental experience retrieval and longer answers |
| Service credentials (as needed) | `.env` | Optional PostgreSQL, Telegram, model, and other services |

A complete resume and structured facts serve different purposes. Providing a resume does not
automatically verify every claim in it, and generated text is not treated as fact. New questions
about qualifications or consent require supporting evidence or an explicit choice.

## Operation and limits

- Onboarding, SQLite initialization, and local checks do not require external accounts.
- Collection uses public applicant tracking system (ATS) APIs. Queues and scheduled jobs require Redis; see [operations](docs/en/operations.md).
- With `ALLOW_LIVE_SUBMIT=false`, the worker stops before opening an application browser. This setting does not disable separately enabled Telegram notifications or other external services.
- With `RESUME_FILE` configured, each job gets an attachment copy, a hash, and an expiration date. Editing the original resume does not silently change a prepared attachment.
- Without a complete resume, a development composer can generate a simple DOCX from facts. It is not a substitute for a complete personal resume.
- Install browser dependencies separately with `uv sync --frozen --extra browser`. Driver, Codex, and LCU have different prerequisites; see operations.
- Telegram, model services, and ATS providers receive different subsets of personal data; see [data privacy](docs/en/data-privacy.md).

## Development and documentation

[Project structure](docs/en/project-structure.md) · [Onboarding](docs/en/onboarding.md) ·
[Operations](docs/en/operations.md) · [Data privacy and publishing](docs/en/data-privacy.md) ·
[Development and validation](docs/en/development.md)

```bash
uv sync --frozen --extra test --extra browser
uv run playwright install chromium
uv run pytest
python3 scripts/export_public.py --check
```

`public-files.txt` explicitly lists the files included in a source export. Before publishing,
create a fresh local directory without the original Git history:

```bash
python3 scripts/export_public.py --output ../job-apply-agent-public
```

This command does not create a remote repository, commit, push, or upload anything. It refuses
to overwrite an existing directory. The original workspace's history may contain old personal
configuration; checking current files does not audit that history. Share the reviewed export.

The project retains its Apache-2.0 license. Third-party browser and model runtimes are not
distributed with the source.
