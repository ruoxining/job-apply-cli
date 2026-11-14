import json
from pathlib import Path

import pytest
from sqlalchemy import select

from job_apply_agent.config import Settings
from job_apply_agent.domain.enums import ATSKind
from job_apply_agent.domain.schemas import AnswerBankEntry, JobPosting
from job_apply_agent.models import AnswerBankRecord, JobPostingRecord
from job_apply_agent.onboarding import check, import_answers, initialize, read_answers
from job_apply_agent.services.resumes import ResumeVariantService


def test_init_preserves_existing_files_and_example_is_not_ready(tmp_path):
    directory = tmp_path / "candidate"
    assert len(initialize(directory)) == 5
    profile = directory / "profile.json"
    original = profile.read_bytes()
    assert initialize(directory) == []
    assert profile.read_bytes() == original
    settings = Settings(
        _env_file=None,
        profile_path=profile,
        answer_bank_path=directory / "answer-bank.json",
        source_boards_path=directory / "source-boards.json",
        search_policy_path=directory / "search-policy.json",
        experience_library_path=directory / "experiences.json",
    )
    result = check(settings)
    assert not result["ready"]
    assert result["mode"] == "shadow"
    assert any("resume" in e for e in result["errors"])
    assert "candidate@example.test" not in json.dumps(result)


def test_reviewed_candidate_can_pass_check_without_network(tmp_path):
    initialize(tmp_path)
    profile = {
        "allowed_countries": ["Canada"],
        "facts": [
            {"id": "p1", "statement": "Built a Python fixture service.", "source": "project notes"}
        ],
    }
    (tmp_path / "profile.json").write_text(json.dumps(profile))
    (tmp_path / "source-boards.json").write_text('[{"ats":"lever","board_slug":"sample"}]')
    entries = [
        {"semantic_key": "first_name", "answer": "Alex"},
        {"semantic_key": "last_name", "answer": "Example"},
        {"semantic_key": "email", "answer": "alex@fixture.invalid"},
    ]
    (tmp_path / "answer-bank.json").write_text(json.dumps(entries))
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\nsynthetic fixture\n%%EOF")
    settings = Settings(
        _env_file=None,
        profile_path=tmp_path / "profile.json",
        answer_bank_path=tmp_path / "answer-bank.json",
        source_boards_path=tmp_path / "source-boards.json",
        search_policy_path=tmp_path / "search-policy.json",
        experience_library_path=tmp_path / "experiences.json",
        resume_file=resume,
    )
    assert check(settings)["ready"]


def test_import_is_scoped_idempotent_and_does_not_dispatch(session):
    entries = [AnswerBankEntry(semantic_key="authorization", answer="Yes", regions=["Canada"])]
    assert import_answers(session, entries) == 1
    assert import_answers(session, entries) == 1
    import_answers(
        session,
        [AnswerBankEntry(semantic_key="authorization", answer="No", regions=["United States"])],
    )
    assert len(session.scalars(select(AnswerBankRecord)).all()) == 2
    assert session.scalars(select(JobPostingRecord)).all() == []


def test_placeholder_and_duplicate_answers_are_rejected(tmp_path):
    initialize(tmp_path)
    with pytest.raises(ValueError):
        read_answers(tmp_path / "answer-bank.json")
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps([{"semantic_key": "name", "answer": "Alex"}] * 2))
    with pytest.raises(ValueError, match="duplicate"):
        read_answers(path)


def test_provided_resume_is_copied_and_not_rewritten(session, tmp_path):
    import hashlib

    original = tmp_path / "reviewed.pdf"
    content = b"%PDF-1.4\nowner reviewed fixture\n%%EOF"
    original.write_bytes(content)
    job = JobPosting(
        source=ATSKind.LEVER,
        source_id="j1",
        company="Example",
        title="Engineer",
        url="https://example.test/jobs/j1",
    )
    session.add(
        JobPostingRecord(
            id="j1",
            canonical_key="j" * 64,
            source="lever",
            source_id="j1",
            company=job.company,
            title=job.title,
            url=job.url,
            normalized={},
        )
    )
    session.flush()
    record = ResumeVariantService(tmp_path / "artifacts", provided_file=original).create(
        session, job_id="j1", job=job, facts=[]
    )
    assert record.resume_template == "user-provided"
    assert record.resume_fact_ids == []
    assert record.resume_sha256 == hashlib.sha256(content).hexdigest()
    original.write_bytes(b"modified source")
    assert Path(record.resume_pdf_path).read_bytes() == content
    assert record.submit_started_at is None
