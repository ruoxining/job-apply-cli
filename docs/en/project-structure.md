# Project structure and workflow

English | [简体中文](../project-structure.md)

```text
src/job_apply_agent/
  examples/       Initialization templates without real identities
  onboarding.py   Local template creation, input checks, and answer import
  collectors/     Public ATS and single-link collection, normalization
  domain/         Data schemas, status and sensitivity enums
  resume/         Generic DOCX composer constrained by supplied facts
  services/       Filtering, quotas, resume snapshots, answers, review, reports
  ats/            Playwright and optional model / Computer Use backends
  experience/     Experience storage, revisions, optional retrieval and reasoning
  telegram/       Human review, experience editing, result notifications
  tasks.py        Persistent state and queued tasks
  scheduler.py    Scheduled collection entry point
  api.py          Local operations and manual scheduling API
  models.py       SQLAlchemy models
alembic/          Database migrations, retaining the existing version chain
scripts/          Source checks, deployment checks, status, export, experiment tools
private/          User inputs, resumes, database, browser state, output; not distributed
```

The collection flow is: source configuration → ATS collector → normalization and deduplication
→ eligibility checks and scoring → material preparation → automatic queue or Telegram review.
Before live execution, the system checks the state, locked resume, and submission setting.
It persists a submission claim before final submission and records completion time and a
receipt only after confirming success. If a click has an unclear outcome, retain the claim
and verify the external result first.

`job_postings` stores jobs, current materials, answers, status, and receipts. `answer_bank`
stores confirmed answers. `experiences` and `experience_revisions` store the optional
experience library and its revisions. All are private runtime data. Older migrations used
other tables; simplifying current models is not a reason to delete historical migrations.

`RESUME_FILE` points to an attachment already reviewed by the candidate. The service checks
its format, copies it, and binds a hash and expiration date. It does not claim to verify each
sentence in an existing PDF. When no file is configured, the development composer generates
a simple DOCX from `ProfileFact` entries. It can convert that file to PDF when LibreOffice
is installed; the hash matches the file actually preferred for use.

Driver, Codex, and LCU are distinct execution backends, retained despite similarities in
their interfaces. Test fixtures and integration boundaries remain in place. Launch scripts
that depend on personal historical inputs, fixed private hosts, or old images that cannot
be rebuilt are excluded from the public entry points.
