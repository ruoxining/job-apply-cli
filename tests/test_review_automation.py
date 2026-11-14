"""Security boundaries of privileged review orchestration, using a fake GitHub API."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ai_review", Path(__file__).resolve().parents[1] / "scripts/ai_review.py"
)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def file_patch(**changes):
    return {
        "filename": "src/example.py",
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "patch": "@@ -1 +1 @@\n-old\n+new",
        **changes,
    }


def test_missing_or_truncated_patches_cannot_be_reported_as_complete():
    for file in [file_patch(patch=""), file_patch(additions=4), file_patch(patch=None)]:
        with pytest.raises(ValueError):
            review.build_diff([file])


def test_review_limits_do_not_silently_clip():
    with pytest.raises(ValueError, match="40-file"):
        review.build_diff([file_patch()] * 41)
    with pytest.raises(ValueError, match="90 KB"):
        review.build_diff([file_patch(patch="@@ -1 +1 @@\n-old\n+" + "x" * 90_000)])


def test_generated_files_are_explicitly_excluded():
    diff, excluded = review.build_diff([file_patch(), file_patch(filename="uv.lock")])
    assert "src/example.py" in diff and "uv.lock" not in diff
    assert excluded == ["uv.lock"]


def test_review_output_cannot_embed_html_mentions_or_images():
    body = review.render_review(
        {"key_issues_to_review": ["```\n<img src='evil'> @someone"]}, "abc", []
    )
    assert "<img" not in body and "@someone" not in body
    assert body.count("```") == 2


class FakeAPI:
    def __init__(self, *, draft=False, stale=False, duplicate=False, latest="success"):
        self.draft, self.stale, self.duplicate, self.latest = draft, stale, duplicate, latest
        self.reads = 0
        self.posts = []

    def request(self, path, data=None):
        if data is not None:
            self.posts.append(data)
            return {}
        if path == "pulls/1":
            self.reads += 1
            return {
                "state": "open",
                "draft": self.draft,
                "base": {"ref": "main"},
                "head": {"sha": "new" if self.stale and self.reads > 1 else "abc"},
            }
        if path.startswith("actions/"):
            return {
                "workflow_runs": [
                    {
                        "id": 10,
                        "event": "pull_request",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "id": 11,
                        "event": "pull_request",
                        "status": "completed",
                        "conclusion": self.latest,
                    },
                ]
            }
        raise AssertionError(path)

    def pages(self, path):
        if path.endswith("reviews"):
            return (
                [{"user": {"login": "github-actions[bot]"}, "body": review.marker("abc")}]
                if self.duplicate
                else []
            )
        return [file_patch()]


@pytest.mark.parametrize("options", [{"draft": True}, {"duplicate": True}, {"latest": "failure"}])
def test_ineligible_pr_never_calls_model_or_posts(monkeypatch, options):
    def forbidden(*_):
        raise AssertionError("The model must not be called")

    monkeypatch.setattr(review, "run_model", forbidden)
    api = FakeAPI(**options)
    review.review_one(api, 1)
    assert api.posts == []


def test_stale_model_result_is_discarded(monkeypatch):
    monkeypatch.setattr(review, "run_model", lambda _: {"key_issues_to_review": []})
    api = FakeAPI(stale=True)
    review.review_one(api, 1)
    assert api.posts == []


def test_ai_posts_only_a_comment_on_verified_commit(monkeypatch):
    monkeypatch.setattr(review, "run_model", lambda _: {"key_issues_to_review": []})
    api = FakeAPI()
    review.review_one(api, 1)
    assert len(api.posts) == 1
    assert api.posts[0]["event"] == "COMMENT"
    assert api.posts[0]["commit_id"] == "abc"


@pytest.mark.parametrize(
    ("later_result", "expected"),
    [("action_required", True), ("failure", False), ("cancelled", False), (None, False)],
)
def test_unexecuted_approval_placeholder_cannot_hide_real_ci(later_result, expected):
    class RunsAPI:
        def request(self, _):
            return {
                "workflow_runs": [
                    {
                        "id": 10,
                        "event": "workflow_dispatch",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "id": 11,
                        "event": "pull_request",
                        "status": "in_progress" if later_result is None else "completed",
                        "conclusion": later_result,
                    },
                ]
            }

    assert review.latest_ci_passed(RunsAPI(), "abc") is expected


def test_approval_placeholder_alone_is_not_successful_ci():
    class RunsAPI:
        def request(self, _):
            return {
                "workflow_runs": [
                    {
                        "id": 11,
                        "event": "pull_request",
                        "status": "completed",
                        "conclusion": "action_required",
                    }
                ]
            }

    assert not review.latest_ci_passed(RunsAPI(), "abc")
