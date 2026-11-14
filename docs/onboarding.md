# Onboarding

English | [简体中文](zh-CN/onboarding.md)

Start with the installation commands in the [README](../README.md). `onboard init`
copies bundled examples into `private/candidate/` without overwriting existing files.
Edit those local files directly; you do not need to upload your resume to the project author
or a hosted platform. The project currently provides a CLI and API, with no upload web page
or automatic extraction of facts from PDFs.

## 1. Provide a complete resume

Copy a PDF or DOCX that you have reviewed into `private/candidate/`. The default setting is:

```dotenv
RESUME_FILE=private/candidate/resume.pdf
```

You can use another local path. Supported formats are PDF and DOCX, up to 20 MiB. Application
preparation copies the file and locks its hash; it does not edit the original or rewrite your
experience. Format checks do not verify factual accuracy or review the layout.

Keep LaTeX sources, template projects, and a personal resume Git repository in any separate
private location. Give this project only the exported file's path. It does not need to clone
or synchronize the resume repository, or include it in a package or container image.

## 2. Enter facts and job preferences

Edit `profile.json`, replace every `REPLACE_` placeholder, and confirm each item:

- Target countries, job types, job titles, remote preferences, and acceptable work arrangements.
- Actual years of work experience, company or industry experience, and skills you have.
- Countries where you are authorized to work and whether you need employer sponsorship. Default Boolean values do not confirm your eligibility.
- A stable `id`, accurate `statement`, evidence `source`, and applicable dates, skills, and verified numbers for each entry in `facts`.

The `source` can refer to your project records or local supporting material. Include only
claims you can confirm. Models must not invent achievements, employment, degrees, or skills.
`allowed_rewrites` lists equivalent wording that you have reviewed.

The optional `experiences.json` accepts more detailed experience. Its structure is shown
below; the example does not represent anyone's actual background:

```json
{
  "projects": [{
    "id": "sample-project",
    "name": "Example service",
    "description": ["Built and tested a small Python service."],
    "skills": ["Python"]
  }],
  "experience": [], "education": [], "publications": [], "awards": [], "certificate": []
}
```

Leave lists empty when you have no detailed entries. Experience reasoning also requires a
configured model backend; ordinary initialization does not enable it.

## 3. Enter reusable answers

Edit `answer-bank.json`. At minimum, fill in `first_name`, `last_name`, and `email`. Also
provide confirmed values such as `full_name`, `phone`, `linkedin_url`, and `github_url` when
needed. Remove entries that do not apply. Each entry uses the `AnswerBankEntry` structure:

```json
{
  "semantic_key": "work_authorization_canada",
  "answer": "REPLACE_WITH_YOUR_CONFIRMED_ANSWER",
  "regions": ["Canada"],
  "sensitivity": "sensitive",
  "valid_until": "2030-01-01"
}
```

Replace both the answer and expiration date with values that reflect your situation; do not
import this example unchanged. For field keys, see the mappings in `services/answers.py`,
`ats/answer_binding.py`, and `ats/playwright_adapter.py`. Answers to choice questions must
still match the current page's options; similar wording alone does not justify a selection.

An empty `regions` list means no region restriction. Use explicit regions for work authorization,
sponsorship, and similar answers. An empty `valid_until` means no expiration date is set; it
does not imply that the fact will never change. Keep salary expectations, current employer,
and availability up to date as well. Do not include passwords, verification codes, government
ID numbers, or payment card details. Consent to interview recording or transcription should
remain scoped to the relevant company and question.

Only `onboard import-answers` writes these entries to the configured database. An existing
entry with the same key and region scope is updated; different scopes are stored separately.
Placeholders, expired entries, and duplicate scopes are rejected. Error output does not print
personal answers.

## 4. Configure collection and filtering

Examples in `source-boards.json` default to `enabled: false`. Find the ATS type and board slug
on the company's actual careers page, enter them, then enable the source. Do not use a job
aggregator's search page. Each source supports `max_jobs`, `title_keywords`, and
`location_keywords`; an empty keyword list leaves that filter unrestricted.

Use `search-policy.json` for company and role preferences, exclusions, scoring weights, and
human review thresholds. These are your rules. Model output and source website text must
not override them.

## 5. Validate locally and import

```bash
uv run job-apply-agent onboard check
uv run job-apply-agent init-db
uv run job-apply-agent onboard import-answers
uv run job-apply-agent application-report
```

`check` validates files, types, required inputs, remaining placeholders, answer expiration,
and resume format. It does not contact ATS providers or establish that every application form
has a complete set of answers. `init-db` is for initial local development; existing deployments
use Alembic migrations.

To use a custom data directory, run `onboard init --directory /your/private/candidate` and
update `PROFILE_PATH`, `ANSWER_BANK_PATH`, `SOURCE_BOARDS_PATH`, `SEARCH_POLICY_PATH`,
`EXPERIENCE_LIBRARY_PATH`, and `RESUME_FILE` in `.env`. `check` and `import-answers` always
read the configured paths.

## 6. Enable services as needed

The API uses a Bearer Token dedicated to your local instance. Generate a random value with
the command below and save it as `API_TOKEN` in the ignored `.env` file. Do not copy it into
distributed examples or commits:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Business endpoints require `Authorization: Bearer <token>`. An absent or shorter-than-32-character
configured token returns 503; missing or incorrect request credentials return 401. `/health`
does not require a token. This token grants full API access to the instance, with no per-user
roles or candidate isolation. Collaborators should use separate test instances and fictional data.

Keep `ALLOW_LIVE_SUBMIT=false` initially. Start Redis only when you need queues. Configure
a Telegram bot token and the single allowed chat ID only when you need Telegram review or
notifications. Configure models and browsers separately. See [operations](operations.md)
and [data privacy](data-privacy.md). Keep credentials in the ignored `.env` file or deployment
environment; do not save real values in the distributed examples.
