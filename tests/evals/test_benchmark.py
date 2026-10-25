import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from evals.benchmark import run


@pytest.fixture
def fixture():
    chat = run.load_cases(run.HERE / "query_capabilities/cases.json")
    retrieval = run.load_cases(run.HERE / "retrieval_quality/cases.json")
    return {**chat, "cases": chat["cases"] + retrieval["cases"],
            "facts": {**chat["facts"], **retrieval["facts"]}}


@pytest.fixture
def baseline():
    return [json.loads(line) for suite in ("query_capabilities", "retrieval_quality")
            for line in (run.HERE / suite / "baseline.jsonl").read_text().splitlines()]


def test_every_case_has_historical_observations(fixture, baseline):
    assert {c["id"] for c in fixture["cases"]} == {r["case_id"] for r in baseline}
    assert len(baseline) == 36
    assert all(r["provenance"]["sha256"] for r in baseline)


def test_followup_selection_keeps_context_and_does_not_send_answers(fixture):
    cases = run.select_cases(fixture, ["chat-T02"], "chat")
    assert [c["id"] for c in cases] == ["chat-T01", "chat-T02"]
    payload = run.chat_request(fixture, cases[1], "test")
    assert set(payload) == {"tenantId", "content", "seriesIds"}
    assert payload["content"] == cases[1]["query"]
    playback = run.select_cases(fixture, ["chat-T08"])[0]
    assert run.chat_request(fixture, playback, "test")["extraContext"]["playback"]["positionSeconds"] == 900000
    with pytest.raises(ValueError, match="Unknown"):
        run.select_cases(fixture, ["typo"])
    with pytest.raises(ValueError, match="not a chat"):
        run.select_cases(fixture, ["retrieval-ep04-event-quote"], "chat")


def test_technical_completion_does_not_hide_known_miss(fixture, baseline):
    case = run.select_cases(fixture, ["chat-T07"])[0]
    old = next(r["observed"] for r in baseline if r["case_id"] == case["id"])
    result = run.evaluate(fixture, case, old)
    assert result["execution_completed"]
    assert not result["source_checks_passed"]
    assert result["failures"] == ["missing_known_source:ha-ep04-missed"]
    assert result["quality"] == "manual_review_required"


def test_premise_reference_comes_from_verified_guitar_event(fixture, baseline):
    case = run.select_cases(fixture, ["retrieval-ep04-event-premise"])[0]
    assert case["reference_facts"] == ["guitar-retort"]
    fact = fixture["facts"]["guitar-retort"]
    assert (fact["speaker"], fact["start_ms"]) == ("千早爱音", 1203200)
    old = next(r["observed"] for r in baseline if r["case_id"] == case["id"])
    result = run.evaluate(fixture, case, old)
    assert result["reference_matches"]["guitar-retort"] == {"found": True, "best_rank": 2}
    wrong = copy.deepcopy(old)
    for item in wrong["matches"]:
        item["speaker"] = "椎名立希"
    assert not run.evaluate(fixture, case, wrong)["source_checks_passed"]


def test_fuzzy_answer_is_not_rejected_for_choosing_another_supported_scene(fixture):
    case = run.select_cases(fixture, ["chat-event-drift"])[0]
    observed = {"status": "COMPLETED", "cards": [{
        "episode": 4, "series_id": fixture["datasets"]["ep04-review"]["series_id"],
        "start_ms": 420000, "end_ms": 430000,
    }]}
    result = run.evaluate(fixture, case, observed)
    assert result["source_checks_passed"]
    assert result["quality"] == "manual_review_required"
    assert not result["reference_matches"]["outsider-ep04"]["found"]


@pytest.mark.parametrize("end", [900001, None])
def test_playback_bound_is_enforced_even_with_invalid_time(fixture, end):
    case = run.select_cases(fixture, ["chat-T09"])[0]
    observed = {"status": "COMPLETED", "cards": [{"episode": 10, "start_ms": 890000, "end_ms": end}]}
    assert "playback_exceeds_time_bound:10" in run.evaluate(fixture, case, observed)["failures"]


def test_matching_timestamp_in_another_dataset_does_not_count(fixture, baseline):
    case = run.select_cases(fixture, ["chat-event-jp"])[0]
    observed = copy.deepcopy(next(r["observed"] for r in baseline if r["case_id"] == case["id"]))
    observed["cards"][0]["series_id"] = "wrong-series"
    assert not run.evaluate(fixture, case, observed)["source_checks_passed"]


@pytest.mark.parametrize("fail_first", [False, True])
def test_http_runner_reuses_conversation_and_blocks_failed_followup(fixture, fail_first):
    cases = run.select_cases(fixture, ["chat-T02"], "chat")
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/create"):
            return httpx.Response(201, json={"conversationId": "same-conversation"})
        assert request.url.params["conversationId"] == "same-conversation"
        payload = json.loads(request.content)
        assert set(payload) == {"tenantId", "content", "seriesIds"}
        return httpx.Response(200, json={"assistantMessage": {
            "status": "FAILED" if fail_first else "COMPLETED", "content": "example", "renderBlocks": [],
        }})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        rows = list(run.run_chat(client, fixture, cases, "local-default"))
    assert len(rows) == 2
    assert sum(r.url.path.endswith("/create") for r in requests) == 1
    assert len(requests) == (2 if fail_first else 3)
    assert rows[1][1]["status"] == ("BLOCKED" if fail_first else "COMPLETED")


def test_retrieval_uses_query_parameters_without_grading_hints(fixture):
    case = run.select_cases(fixture, ["retrieval-ep04-event-quote"])[0]
    calls = []

    def search(query, **kwargs):
        calls.append((query, kwargs))
        return SimpleNamespace(model_dump=lambda **kwargs: {
            "chunk_result": {"hits": [], "partial": False}, "matches": [],
        })

    rows = list(run.run_retrieval({"event": SimpleNamespace(search=search)}, fixture, [case], "test"))
    assert rows[0][1]["status"] == "success"
    assert calls == [(case["query"], {"tenant_id": "test", "series_ids": [fixture["datasets"]["ep04-review"]["series_id"]], **case["parameters"]})]


def test_dry_run_and_replay_never_create_clients(monkeypatch, capsys, tmp_path, baseline):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline command tried to create a network client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    assert run.main(["--suite", "query_capabilities"]) == 0
    assert json.loads(capsys.readouterr().out)["api_called"] is False
    source = tmp_path / "live-format.jsonl"
    source.write_text(json.dumps({"kind": "run"}) + "\n" + "\n".join(json.dumps(r) for r in baseline))
    for suite, completed, checked in [("query_capabilities", 13, 12), ("retrieval_quality", 19, 16)]:
        assert run.main(["--suite", suite, "--replay", str(source)]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["suite"] == suite
        assert summary["execution_completed"] == completed
        assert summary["source_checks_passed"] == checked
        assert summary["overall_accuracy"] is None
        assert summary["api_called"] is False


def test_existing_output_is_not_overwritten_or_sent(monkeypatch, tmp_path):
    out = tmp_path / "results.jsonl"
    out.write_text("keep")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("Must check output before live calls"))
    with pytest.raises(FileExistsError):
        run.main(["--suite", "query_capabilities", "--live", "chat", "--data-version", "test", "--out", str(out)])
    assert out.read_text() == "keep"


def test_live_command_reports_source_regression_and_saves_failure(monkeypatch, tmp_path):
    client_type = httpx.Client

    def respond(request):
        if request.url.path.endswith("/create"):
            return httpx.Response(201, json={"conversationId": "test"})
        return httpx.Response(200, json={"assistantMessage": {
            "status": "COMPLETED", "content": "No supporting card", "renderBlocks": [],
        }})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond)))
    out = tmp_path / "regression.jsonl"
    assert run.main(["--suite", "query_capabilities", "--live", "chat", "--only", "chat-T07", "--data-version", "test", "--out", str(out)]) == 1
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows[0]["kind"] == "run"
    assert rows[1]["evaluation"]["execution_completed"]
    assert not rows[1]["evaluation"]["source_checks_passed"]


@pytest.mark.parametrize("argv", [
    [],
    ["--suite", "query_capabilities", "--live", "retrieval"],
    ["--suite", "retrieval_quality", "--cases", str(run.HERE / "query_capabilities/cases.json")],
])
def test_suite_must_be_explicit_and_match_execution(argv, monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("Invalid suite must not call services"))
    with pytest.raises(SystemExit) as exc:
        run.main(argv)
    assert exc.value.code == 2


def test_chat_behavior_preserves_full_history_and_historical_fixture_versions():
    cases = json.loads((run.HERE / "chat_behavior/cases.json").read_text())
    by_id = {c["id"]: c for c in cases}
    assert len(by_id) == len(cases) == 10
    assert by_id["c19_original_history"]["expected"] == "respond"
    assert len(by_id["c19_original_history"]["state"]["recent_messages"]) == 2
    assert by_id["retrieve_feedback_quote"]["expected"] == "retrieve"
    for name, count in [("jev-vs-direct-20261024", 40), ("hook-models-20261024", 60)]:
        path = run.HERE / "chat_behavior/baselines" / f"{name}.jsonl"
        metadata, *rows = [json.loads(line) for line in path.read_text().splitlines()]
        archived = {c["id"]: c for c in metadata["fixture"]}
        assert metadata["suite"] == "chat_behavior"
        assert archived.keys() == by_id.keys()
        assert len(rows) == count
        assert len({(r["arm"], r["case_id"], r["repeat"]) for r in rows}) == count
        for arm, summary in metadata["summary"]["arms"].items():
            selected = [r for r in rows if r["arm"] == arm]
            assert {(r["case_id"], r["repeat"]) for r in selected} == {
                (case_id, repeat) for case_id in by_id for repeat in (1, 2)}
            assert {grade: sum(r["historical_review"]["business_quality"] == grade for r in selected)
                    for grade in summary["review_counts"]} == summary["review_counts"]
        for row in rows:
            assert row["expected"] == archived[row["case_id"]]["expected"]
        if name == "hook-models-20261024":
            assert metadata["fixture"] == cases
        else:
            assert [key for key in by_id if by_id[key] != archived[key]] == ["holdout_mixed"]
            assert all(r["historical_review"]["business_quality"] == "unassessed"
                       for r in rows if r["case_id"] == "holdout_mixed")


def test_kimi_observations_remain_query_coverage_with_recorded_failures(capsys):
    path = run.HERE / "query_capabilities/kimi-20261025.jsonl"
    assert run.main(["--suite", "query_capabilities", "--replay", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["observations"], summary["execution_completed"], summary["source_checks_passed"]) == (13, 11, 9)
    assert summary["api_called"] is False
