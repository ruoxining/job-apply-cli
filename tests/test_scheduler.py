from datetime import datetime, timezone

from job_apply_agent import scheduler
from job_apply_agent.config import Settings


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, *, nx=False, xx=False, ex=None):
        if nx and key in self.values:
            return False
        if xx and key not in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key):
        self.values.pop(key, None)


def test_dispatch_if_due_claims_only_once(session, monkeypatch, tmp_path):
    captured = []
    fake = FakeRedis()
    monkeypatch.setattr(scheduler.redis.Redis, "from_url", lambda url: fake)
    monkeypatch.setattr(scheduler, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(
        scheduler.collect_configured_boards, "send", lambda **kwargs: captured.append(kwargs)
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text('{"version": 1}')
    settings = Settings(
        _env_file=None,
        collection_hour_utc=6,
        source_boards_json='[{"ats":"greenhouse","board_slug":"acme"}]',
        profile_json='{"skills":["python"]}',
        search_policy_path=policy_path,
    )
    now = datetime(2026, 10, 16, 7, tzinfo=timezone.utc)
    assert scheduler.dispatch_if_due(settings, now=now) is True
    assert scheduler.dispatch_if_due(settings, now=now) is False
    assert len(captured) == 1
    assert "run_id" not in captured[0]


def test_dispatch_if_due_waits_until_configured_hour(monkeypatch):
    settings = Settings(
        collection_hour_utc=8,
        source_boards_json='[{"ats":"greenhouse","board_slug":"acme"}]',
    )
    assert (
        scheduler.dispatch_if_due(settings, now=datetime(2026, 10, 16, 7, 59, tzinfo=timezone.utc))
        is False
    )
