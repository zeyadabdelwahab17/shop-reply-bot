from evals import run_eval


def rec(qid, status, category="price", ms=100, **extra):
    return {"id": qid, "category": category, "question": "?", "should_handoff": False,
            "status": status, "latency_ms": ms, **extra}


def test_metrics_exclude_invalid_from_accuracy():
    records = [
        rec("a", "correct", problems=[]),
        rec("b", "wrong", problems=["false_handoff"]),
        rec("c", "wrong", category="handoff_wholesale", problems=["missed_handoff"]),
        {"id": "d", "category": "price", "question": "?", "should_handoff": False, "status": "invalid",
         "error": "timeout"},
    ]
    m, per_cat = run_eval.compute_metrics(records)
    assert (m["questions"], m["graded"], m["invalid"], m["correct"]) == (4, 3, 1, 1)
    assert m["accuracy"] == 33.3
    assert (m["false_handoffs"], m["missed_handoffs"]) == (1, 1)
    assert per_cat["handoff"] == {"correct": 0, "total": 1, "accuracy": 0.0}


def test_p95_nearest_rank():
    assert run_eval.p95(list(range(1, 21))) == 19
    assert run_eval.p95([]) is None


def test_groq_json_failure_counts_as_invalid_json_not_invalid_call():
    err = "other: Error code: 400 - Failed to generate JSON. Please adjust your prompt."
    assert run_eval.is_json_failure(err)
    questions = [{"id": "q", "should_handoff": False, "must_contain": [], "must_not_contain": []}]
    out = run_eval.regrade([{"id": "q", "category": "price", "status": "invalid", "error": err}], questions)
    assert out[0]["status"] == "wrong" and out[0]["invalid_json"]


def test_parse_seconds():
    assert run_eval.parse_seconds("7.66s") == 7.66
    assert run_eval.parse_seconds("1m2s") == 62
    assert run_eval.parse_seconds("120ms") == 0.12
    assert run_eval.parse_seconds(None) is None
