# Data storage and publishing

English | [简体中文](../data-privacy.md)

## Local and external data flows

| Operation | Data destination |
| --- | --- |
| Initialization, input checks, answer import, and result reports | Configured local files and database; these commands do not call external services |
| Public job collection | ATS providers receive ordinary job queries |
| Telegram review and notifications, when enabled | Telegram receives job information, review attachments, and results |
| Model planning or experience reasoning, when enabled | The configured model service may receive page text, job descriptions, relevant candidate facts, and answers |
| Filling out or uploading a real application | The employer or ATS receives entered information and attachments; entering fields may already transmit data |

`ALLOW_LIVE_SUBMIT=false` prevents the worker from opening a live application browser. It is
not a network kill switch for the whole system. Publishing the code does not require publishing
personal data, and local orchestration does not mean that all processing stays local.

## Private content

Keep personal resume repositories, source files, exported resumes, experience, contact details,
qualifications, answers, job preferences, application records, receipts, screenshots, model
logs, databases, browser sessions, and credentials in `private/` or another private directory.
The root `.env` is also ignored. Git ignore rules do not remove previously committed content.

`private/` is excluded from Git, the Docker build context, and source exports. Python
distributions include only explicitly allowed source and project files. The export manifest
rejects private paths, databases, resume files, and symbolic links. Examples contain only
placeholders and fictional content; do not write real data into `src/job_apply_agent/examples/`.

A personal resume can live in a completely independent private Git repository. Use its
exported PDF/DOCX and experience JSON through file paths. No Git submodule, remote
synchronization, or personal account URL in the source is needed.

## Export locally before sharing

```bash
python3 scripts/export_public.py --check
python3 scripts/export_public.py --output ../job-apply-agent-public
```

The script copies files listed in `public-files.txt`, refuses to overwrite an existing directory,
and excludes `.git`, private data, and remote configuration. It does not commit, push, or upload.
Checks cover recognizable credential formats, non-example email addresses, personal home
directories, and an optional list of private strings. Put a local JSON array of strings in
`private/open-source-prep/privacy-markers.json`, or supply `--markers` explicitly. Output reports
only the file, line, and rule; it does not print matched content.

Automated scans cover defined rules, not every form of personal experience or indirect
identifying detail. Review the manifest, source, and documentation before publishing. Review
new public files before adding them to the manifest. Old commits in an original repository
may contain personal data; cleaning the current working tree does not establish that its
history is safe. Create a new public repository from an export without history, or separately
audit and clean the full history before publishing it.

Logs and database backups are private data too. Container users must configure private
volumes, access controls, and backup locations. This project does not upload them to cloud
storage or public repositories on the user's behalf.
