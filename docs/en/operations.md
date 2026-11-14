# Operations

English | [简体中文](../operations.md)

## Local development and shadow mode

Complete [onboarding](onboarding.md) first. You can run the API and query SQLite independently:

```bash
uv run job-apply-agent serve
uv run job-apply-agent application-report
```

The API binds to loopback by default and currently has no user authentication layer for
public access. Do not expose it directly to the internet. Queues and scheduling require
Redis; the development Compose configuration provides optional local infrastructure:

```bash
docker compose -f docker-compose.dev.yml up -d
```

Development Compose exposes PostgreSQL on host port 5433 and Redis on 6379, both bound to
loopback. To use PostgreSQL, change `DATABASE_URL` in `.env` to the development connection
and run Alembic migrations. Local SQLite is sufficient for checking configuration when you
do not need PostgreSQL.

## Optional self-hosted Compose deployment

The production Compose configuration includes PostgreSQL, Redis, migrations, the API, a
worker, a scheduler, and the Telegram bot. Set `APP_ENV=production`, a non-default database
password, and Telegram credentials. The candidate directory defaults to `./private/candidate`
and is mounted read-only at `/candidate`; it is not copied into the image. Set `RESUME_FILENAME`
to the actual attachment filename in that directory. Do not push images containing private
data to a registry.

```bash
uv run job-apply-agent onboard check
python3 scripts/validate_deployment.py
docker compose config --quiet
docker compose up -d --build
```

Exporting or publishing the source does not run these deployment commands. Use the migration
chain for existing databases and make a private backup before upgrading; `init-db` does not
replace migrations. Preserve access controls when backing up PostgreSQL, artifact volumes,
and candidate inputs.

## Live applications and browsers

Live applications require explicitly setting `ALLOW_LIVE_SUBMIT=true`. Deployment validation
also requires:

```dotenv
LIVE_SUBMIT_ACK=I_UNDERSTAND_REAL_APPLICATIONS_WILL_BE_SENT
```

First use a small supervised sample to verify fields, attachments, and submission receipts.
Configure queue and source quotas in `.env`; increasing a quota does not bypass review.
`managed-apply <application-id>` hands one task to the worker.
`managed-backlog start|status|stop` manages a queue with an explicit scope. Applications that
already have a submission claim must not be retried as ordinary failures.

After installing browser dependencies, the Driver backend uses a separate Chromium profile.
Linux Compose runs with Xvfb and AT-SPI. Set `BROWSER_CHANNEL=chromium` and
`BROWSER_HEADLESS=false`. If you need model planning, enable `CUA_ENABLED` and provide the
corresponding service credentials.

| Backend | Prerequisites | Current scope |
| --- | --- | --- |
| `driver` | The `browser` extra, Chromium, CUA Driver, and a configured GLM service | Standard optional execution path; verify the runtime first |
| `codex` | A separately installed and authenticated Codex CLI, plus the required browser dependencies | Experimental path; private CLI credentials and old images are not included |
| `lcu` | A local LCU/ChatGPT runtime you are authorized to use, with its paths configured | Experimental adapter; the proprietary runtime is not distributed with the source |

Stop and report unavailable backends. Passing mock tests does not establish that a real
runtime works. `EXPERIENCE_REASONING_ENABLED` applies only to the relevant Codex path;
`hybrid` / `auto` retrieval also requires the `retrieval` extra. Model services may receive
candidate facts; read [data privacy](data-privacy.md) before enabling them.

## Telegram and status checks

Telegram accepts only the single configured chat ID. `/jd <url>` adds an individual job;
review buttons enqueue locked application materials, with the default shadow-mode setting
still enforced. Starting the bot communicates with Telegram and may send resumes.

The remote read-only status script requires an explicit host and path:

```bash
python3 scripts/server_status.py --host YOUR_SSH_ALIAS --remote-dir /srv/job-apply-agent
```

It connects to the specified server only when you run it. Use `application-report` locally
to review newly confirmed submissions, historical applications, unknown outcomes, and
exclusions. A database status alone is not an external ATS receipt.
