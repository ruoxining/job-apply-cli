from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from job_apply_agent.api import app, get_dispatcher, get_session
from job_apply_agent.models import JobPostingRecord


def seed_job(
    session, *, company="stripe", title="Backend Engineer", source="greenhouse",
    score=80.0, state="auto_apply_queued", channel="auto", suffix="",
    first_seen=None, claimed=None, submission_status="pending",
):
    token = f"{source}-{company}-{suffix or title}"
    job = JobPostingRecord(
        canonical_key=token.ljust(64, "0")[:64], source=source, source_id=token,
        sources=[], company=company, title=title,
        url=f"https://jobs.example.com/{company}/{suffix or title}", location="Toronto, ON",
        normalized={}, eligible=score > 0, score=score, score_breakdown={"company": score},
        application_state=state, application_channel=channel,
        first_seen_at=first_seen or datetime.now(timezone.utc),
        last_seen_at=first_seen or datetime.now(timezone.utc),
        apply_claimed_on=claimed, submission_status=submission_status,
    )
    session.add(job)
    session.commit()
    return job


def client_for(session):
    def override_session():
        yield session
    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def test_quota_endpoint_derives_consumption_from_jobs(session):
    day = date(2026, 10, 10)
    for index in range(2):
        seed_job(
            session, company=f"acme-{index}", suffix=str(index),
            first_seen=datetime(2026, 10, 10, index, tzinfo=timezone.utc),
            claimed=day if index == 0 else None,
        )
    try:
        payload = client_for(session).get("/v1/quotas/2026-10-10").json()
    finally:
        app.dependency_overrides.clear()
    assert payload["fetched"] == 2
    assert payload["applied"] == 1


def test_list_jobs_filters_single_table_fields(session):
    seed_job(session, company="stripe", title="High Scorer", score=90)
    seed_job(session, company="ashbycorp", title="Low Scorer", source="ashby", score=40, state="ineligible")
    try:
        client = client_for(session)
        filtered = client.get("/v1/jobs", params={"min_score": 70}).json()
        by_state = client.get("/v1/jobs", params={"state": "ineligible"}).json()
    finally:
        app.dependency_overrides.clear()
    assert [item["company"] for item in filtered["items"]] == ["stripe"]
    assert by_state["items"][0]["title"] == "Low Scorer"


def test_list_applications_is_a_job_lifecycle_view(session):
    seed_job(session, company="stripe", title="Queued")
    seed_job(
        session, company="linear", title="Review Me", source="ashby",
        state="awaiting_resume_review", channel="telegram_review", suffix="review",
    )
    try:
        payload = client_for(session).get(
            "/v1/applications", params={"state": "awaiting_resume_review"}
        ).json()
    finally:
        app.dependency_overrides.clear()
    assert payload["total"] == 1
    assert payload["items"][0]["job"]["company"] == "linear"


def test_operations_status_summarizes_two_table_database(session):
    seed_job(
        session, company="linear", title="Platform Engineer", score=88,
        state="submitting", claimed=datetime.now(timezone.utc).date(),
        submission_status="running",
    )
    try:
        response = client_for(session).get("/v1/ops/status")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    payload = response.json()
    assert payload["database"] == {"status": "ok", "jobs": 1, "tables": 2}
    assert payload["discovery"]["useful_jobs_today"] == 1
    assert payload["applications"]["active_submissions"] == 1


def test_run_collections_uses_the_configured_private_policy(session, tmp_path, monkeypatch):
    from job_apply_agent.config import Settings
    import job_apply_agent.api as api

    policy_path = tmp_path / "policy.json"
    policy_path.write_text('{"version": 7, "excluded_keywords": ["unwanted-role"]}')
    monkeypatch.setattr(api, "get_settings", lambda: Settings(
        _env_file=None, search_policy_path=policy_path, profile_json='{"skills": ["Python"]}'
    ))
    captured = {}
    def fake_dispatcher(boards, profile, policy):
        captured.update(boards=boards, profile=profile, policy=policy)
    app.dependency_overrides[get_session] = lambda: iter([session])
    app.dependency_overrides[get_dispatcher] = lambda: fake_dispatcher
    try:
        response = TestClient(app).post(
            "/v1/collections/run",
            json={"boards": [{"ats": "greenhouse", "board_slug": "acme"}]},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert captured["policy"]["version"] == 7
    assert captured["policy"]["excluded_keywords"] == ["unwanted-role"]
