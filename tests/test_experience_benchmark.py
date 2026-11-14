from job_apply_agent.experience.benchmark import summarize, usage_totals


def test_usage_includes_generation_and_check_and_never_subtracts_cached_twice():
    trace = {
        "calls": [
            {"usage": [{"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 5}]},
            {"usage": [{"input_tokens": 60, "cached_input_tokens": 30, "output_tokens": 3}]},
        ]
    }
    assert usage_totals(trace) == {
        "input_tokens": 160,
        "cached_input_tokens": 110,
        "output_tokens": 8,
    }


def test_incomplete_usage_is_unknown_not_free():
    assert usage_totals({"calls": []}) is None
    assert usage_totals({"calls": [{"usage": None}]}) is None
    assert usage_totals({"calls": [{"usage": [{"input_tokens": 50}]}]}) is None


def test_own_checker_acceptance_is_not_reported_as_correctness():
    result = summarize(
        [{"arm": "full", "trace": {"status": "accepted", "elapsed_seconds": 1, "calls": []}}]
    )
    assert result["full"]["total_tokens"] is None
    assert result["full"]["accepted_by_own_checker"] == 1
    assert result["full"]["correctness"].startswith("pending")
