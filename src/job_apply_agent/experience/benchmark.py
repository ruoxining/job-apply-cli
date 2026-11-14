"""Server-owned, checkpointed paired replay of real historical ATS questions.

The input carries provenance, not invented form options. Gold rubrics are never
passed to either model. A model's own support check is NOT a correctness score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import statistics
import time
from datetime import UTC, date, datetime
from pathlib import Path

from job_apply_agent.ats.codex_planner import CodexCLIPlanner
from job_apply_agent.domain.schemas import AnswerBankEntry, FormQuestion

from .library import Experience, fingerprint
from .reasoning import AnswerReasoner
from .retrieval import HybridRetriever, SentenceEncoder, VectorCache


def write_private(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def usage_totals(record: dict) -> dict | None:
    calls = record.get("calls", [])
    if not calls or any(not c.get("usage") for c in calls):
        return None
    events = [event for call in calls for event in call["usage"]]
    required = ("input_tokens", "cached_input_tokens", "output_tokens")
    if any(any(key not in event for key in required) for event in events):
        return None
    return {key: sum(e[key] for e in events) for key in required}


def summarize(results: list[dict]) -> dict:
    summary = {}
    for arm in ("full", "hybrid"):
        rows = [r for r in results if r["arm"] == arm]
        usage = [usage_totals(r["trace"]) for r in rows]
        complete_usage = bool(rows) and all(u is not None for u in usage)
        available_usage = [u for u in usage if u is not None]
        summary[arm] = {
            "attempts": len(rows),
            "accepted_by_own_checker": sum(r["trace"]["status"] == "accepted" for r in rows),
            "unknown": sum(r["trace"]["status"] == "unknown" for r in rows),
            "rejected": sum(r["trace"]["status"] == "rejected" for r in rows),
            "errors": sum(r["trace"]["status"] == "error" for r in rows),
            "median_seconds": statistics.median(r["trace"]["elapsed_seconds"] for r in rows)
            if rows
            else None,
            "total_tokens": {
                key: sum(u[key] for u in available_usage) for key in available_usage[0]
            }
            if complete_usage
            else None,
            "usage_missing_attempts": sum(u is None for u in usage),
            "correctness": "pending independent rubric review; checker acceptance is not correctness",
        }
    return summary


async def run(args):
    if platform.system() != "Linux" or socket.gethostname() != args.expected_hostname:
        raise RuntimeError("Run this benchmark inside the explicitly named Linux container")
    data = json.loads(args.input.read_text())
    if not data.get("cases") or any(not c.get("provenance") for c in data["cases"]):
        raise ValueError("Real question provenance is required")
    if args.output.exists():
        raise ValueError("Use a fresh output directory; prior results must not be overwritten")
    args.output.mkdir(parents=True, mode=0o700)
    manifest = {
        "started_at": datetime.now(UTC).isoformat(),
        "hostname": socket.gethostname(),
        "python": platform.python_version(),
        "input_sha256": fingerprint(data),
        "requested_model": args.model,
        "embedding_model": SentenceEncoder.identity,
        "repeats": args.repeats,
        "limit": args.limit,
        "status": "preflight",
        "max_wall_seconds": args.max_wall_seconds,
        "max_model_calls": args.limit * args.repeats * 4,
        "mode": "real historical question replay; no browser actions or submissions",
    }
    write_private(args.output / "manifest.json", manifest)
    started = time.perf_counter()
    results = []
    try:
        planner = CodexCLIPlanner(binary=args.codex, model=args.model, timeout_seconds=180)
        await planner.check_ready()
        experiences = [Experience.model_validate(e) for e in data["experiences"]]
        entries = [AnswerBankEntry.model_validate(e) for e in data["answer_bank"]]
        cold_started = time.perf_counter()
        encoder = await asyncio.to_thread(SentenceEncoder)
        model_seconds = time.perf_counter() - cold_started
        indexed = [
            e
            for e in experiences
            if e.kind not in {"profile", "skills", "coverage", "employment_status"}
        ]
        retriever = await asyncio.to_thread(
            HybridRetriever, indexed, encoder, VectorCache(args.output / "vectors.sqlite3")
        )
        manifest.update(
            status="running",
            model_load_seconds=model_seconds,
            index_seconds=retriever.index_seconds,
            new_vectors=retriever.new_vectors,
            experience_count=len(experiences),
            description_chunk_count=len(retriever.chunks),
        )
        write_private(args.output / "manifest.json", manifest)
        for repeat in range(args.repeats):
            for case_index, case in enumerate(data["cases"][: args.limit]):
                arms = ("full", "hybrid") if (repeat + case_index) % 2 == 0 else ("hybrid", "full")
                for arm in arms:
                    if time.perf_counter() - started > args.max_wall_seconds - 360:
                        raise TimeoutError("bounded experiment wall time reached")
                    service = AnswerReasoner(
                        planner,
                        experiences,
                        entries,
                        mode=arm,
                        top_k=args.top_k,
                        retriever=retriever,
                    )
                    question = FormQuestion.model_validate(case["question"])
                    answer = await service.resolve(
                        question,
                        job_context=case["job_context"],
                        today=date.fromisoformat(data["as_of"]),
                        region=case.get("region"),
                    )
                    row = {
                        "case_id": case["id"],
                        "repeat": repeat,
                        "arm": arm,
                        "answer": answer.model_dump(mode="json"),
                        "trace": service.records[-1],
                        "observed_model": planner.observed_model,
                        "provenance": case["provenance"],
                    }
                    results.append(row)
                    write_private(args.output / f"{repeat}-{case_index:02}-{arm}.json", row)
                    write_private(args.output / "summary.json", summarize(results))
                    manifest["completed_attempts"] = len(results)
                    write_private(args.output / "manifest.json", manifest)
                    print(
                        json.dumps(
                            {
                                "case_id": case["id"],
                                "arm": arm,
                                "repeat": repeat,
                                "status": row["trace"]["status"],
                            }
                        ),
                        flush=True,
                    )
        manifest["status"] = "complete_pending_quality_review"
    except BaseException as exc:
        manifest.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        manifest.update(
            finished_at=datetime.now(UTC).isoformat(), elapsed_seconds=time.perf_counter() - started
        )
        write_private(args.output / "manifest.json", manifest)
        write_private(args.output / "summary.json", summarize(results))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-hostname", required=True)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--repeats", type=int, choices=(1, 2), default=2)
    parser.add_argument("--limit", type=int, choices=range(1, 21), default=20)
    parser.add_argument("--top-k", type=int, choices=range(1, 11), default=5)
    parser.add_argument("--max-wall-seconds", type=int, default=7200)
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
