from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import uvicorn

from . import __version__
from .db import Base, get_engine


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-apply-agent")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="create database tables for local development")
    onboarding = subparsers.add_parser(
        "onboard", help="initialize and check private candidate files"
    )
    onboarding.add_argument("action", choices=("init", "check", "import-answers"))
    onboarding.add_argument("--directory", type=Path, default=Path("private/candidate"))
    serve = subparsers.add_parser("serve", help="run the FastAPI service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    managed_apply = subparsers.add_parser(
        "managed-apply",
        help="queue a complete worker-owned application run and exit",
    )
    managed_apply.add_argument("application_id")
    report = subparsers.add_parser(
        "application-report",
        help="analyze persisted outcomes without modifying or dispatching jobs",
    )
    report.add_argument("--application-id", action="append", dest="application_ids")
    managed_backlog = subparsers.add_parser(
        "managed-backlog",
        help="start, inspect, or stop the worker-owned application backlog",
    )
    managed_backlog.add_argument("action", choices=("start", "status", "stop"))
    managed_backlog.add_argument("--application-id", action="append", dest="application_ids")
    args = parser.parse_args()
    if args.command == "onboard":
        from .config import get_settings
        from .onboarding import check, import_answers, initialize, read_answers

        if args.action == "init":
            print(json.dumps({"created": initialize(args.directory)}, indent=2))
        elif args.action == "check":
            result = check(get_settings())
            print(json.dumps(result, indent=2))
            if not result["ready"]:
                raise SystemExit(1)
        else:
            from .db import get_session_factory

            try:
                entries = read_answers(get_settings().answer_bank_path)
                with get_session_factory()() as session:
                    count = import_answers(session, entries)
                    session.commit()
            except (OSError, ValueError, TypeError):
                parser.exit(1, "Invalid answer file or conflicting scopes; no answers imported.\n")
            print(json.dumps({"imported": count}))
    elif args.command == "init-db":
        import job_apply_agent.models  # noqa: F401

        Base.metadata.create_all(get_engine())
    elif args.command == "serve":
        uvicorn.run("job_apply_agent.api:app", host=args.host, port=args.port)
    elif args.command == "managed-apply":
        from .managed_apply import dispatch_managed_application

        dispatch = dispatch_managed_application(args.application_id)
        print(json.dumps(asdict(dispatch), sort_keys=True))
    elif args.command == "application-report":
        from .db import get_session_factory
        from .services.application_report import application_report

        with get_session_factory()() as session:
            result = application_report(session, application_ids=args.application_ids)
        print(json.dumps(result, sort_keys=True, indent=2))
    elif args.command == "managed-backlog":
        from .managed_backlog import (
            dispatch_managed_backlog_if_enabled,
            get_managed_backlog_status,
            start_managed_backlog,
            stop_managed_backlog,
        )

        if args.action == "start":
            start = start_managed_backlog(application_ids=args.application_ids)
            tick = dispatch_managed_backlog_if_enabled()
            print(
                json.dumps(
                    {"start": asdict(start), "tick": asdict(tick)},
                    sort_keys=True,
                )
            )
        elif args.action == "status":
            print(json.dumps(get_managed_backlog_status(), sort_keys=True))
        else:
            print(json.dumps(stop_managed_backlog(), sort_keys=True))


if __name__ == "__main__":
    main()
