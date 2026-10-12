"""Synthetic offline fixtures derived from the public contracts in docs/glasser.md.

These verify mapping and failure semantics, not live provider availability/quality.
"""
import copy
import io
import json
import urllib.error
from datetime import date, timedelta

import pytest

from radar import aisa, config, data, glasser, new_sites, review, sources, store, usecases
from radar import __main__ as cli


def call(capability="trends", cid="one", keywords=None):
    task = {"keywords": ["strata"] if keywords is None else keywords}
    if capability == "trends":
        task.update(date_from="2026-09-01", date_to="2026-10-10")
    else:
        task.update(location_code=2840, language_code="en")
    return {"call_id": cid, "tool": glasser.ENDPOINTS[capability][0], "arguments": {"body": [task]}}


def native(result=None, status=20000):
    return {"status_code": 20000, "tasks_error": int(status != 20000),
            "tasks": [{"status_code": status, "result": result}]}


def graph(keywords=None, values=None):
    keywords = keywords or ["strata"]
    values = values or [[5] * len(keywords), [10] * len(keywords)]
    return native([{"items": [{"type": "google_trends_graph", "keywords": keywords, "data": [
        {"date_from": (date(2026, 9, 1) + timedelta(days=i)).isoformat(),
         "date_to": (date(2026, 9, 1) + timedelta(days=i)).isoformat(),
         "values": vs, "missing_data": False} for i, vs in enumerate(values)]}]}])


def completed(output=None, charge="0.011", **more):
    return {"id": "run_test_1", "status": "COMPLETED", "charge_usd": charge,
            "provider_response": {"http_status": 200, "error": None},
            "output": output if output is not None else graph(), **more}


@pytest.fixture
def transport(monkeypatch):
    requests, replies = [], []
    monkeypatch.setenv("GLASSER_API_KEY", "gl_test_only")
    monkeypatch.setenv("AISA_API_KEY", "aisa_test_only")
    monkeypatch.setattr(glasser.time, "sleep", lambda seconds: None)

    def open_request(req, timeout):
        requests.append(req)
        assert req.full_url.startswith(glasser.BASE)
        assert req.get_header("Authorization") == "Bearer gl_test_only"
        assert "aisa_test_only" not in str(req.headers)
        assert 0 < timeout <= glasser.TIMEOUT
        answer = replies.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(glasser._OPENER, "open", open_request)
    return requests, replies


def test_default_and_mixed_routing_preserve_aisa_arguments(cfg, monkeypatch):
    calls, observed = [call()], []
    monkeypatch.setattr(aisa, "call_all", lambda cs: (observed.append(("trends", cs)) or {}, 1200))
    monkeypatch.setattr(aisa, "call", lambda cs: (observed.append(("volume", cs)) or {}, 90000))
    assert data.call_all(calls, cfg, capability="trends") == ({}, 1200)
    assert data.call_all(calls, {}, capability="volume") == ({}, 90000)
    assert observed == [("trends", calls), ("volume", calls)]
    assert observed[0][1] is calls
    cfg["data_providers"]["volume"] = "glasser"
    assert data.call_all(calls, cfg, capability="trends") == ({}, 1200)
    assert sources.aisa is aisa and new_sites.aisa is aisa and usecases.aisa is aisa


def test_config_omission_and_partial_override(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{}')
    assert config.load(path)["data_providers"] == {"trends": "aisa", "volume": "aisa"}
    path.write_text('{"data_providers":{"volume":"glasser"}}')
    assert config.load(path)["data_providers"] == {"trends": "aisa", "volume": "glasser"}
    assert config.DEFAULTS["data_providers"] == {"trends": "aisa", "volume": "aisa"}


@pytest.mark.parametrize("providers", ["glasser", None, {"x": "glasser"}, {"new_sites": "glasser"},
                                       {"trends": "typo"}, {"volume": None}])
def test_invalid_selection_fails_before_any_network(providers, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"data_providers": providers}))
    with pytest.raises(ValueError, match="data_providers"):
        cli.main(["daily", "--config", str(path), "--state", str(tmp_path / "state")])
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("options", [{"trends_location_code": 0}, {"trends_location_code": True},
                                    {"trends_language_code": "English"}, {"base_url": "https://example.com"}, []])
def test_invalid_glasser_settings(options):
    with pytest.raises(ValueError):
        data.validate({"data_providers": {"trends": "glasser"}, "glasser": options})


def test_trends_request_mapping_and_cost(cfg, transport):
    requests, replies = transport
    cfg["data_providers"]["trends"] = "glasser"
    cfg["glasser"] = {"trends_location_code": 2826, "trends_language_code": "en"}
    expected = graph()
    replies.append(completed(expected, charge="0.011234"))
    calls = [call()]
    before = copy.deepcopy(calls)
    output, cost = data.call_all(calls, cfg, capability="trends")
    assert output == {"one": expected} and cost == 11234
    req = requests[0]
    assert req.get_method() == "POST" and req.full_url == glasser.BASE
    assert req.get_header("Idempotency-key")
    assert json.loads(req.data) == {
        "provider": "dataforseo", "endpoint": "/v3/keywords_data/google_trends/explore/live", "endpoint_version": 4,
        "input": {"keywords": ["strata"], "date_from": "2026-09-01", "date_to": "2026-10-10",
                  "location_code": 2826, "language_code": "en", "item_types": ["google_trends_graph"]}}
    assert calls == before


def test_google_ads_response_reaches_volume_consumer(cfg, transport):
    requests, replies = transport
    cfg["data_providers"]["volume"] = "glasser"
    replies.append(completed(native([{"keyword": "ai detector", "search_volume": 6000,
                                      "monthly_searches": [{"year": 2026, "month": 8}]}]), charge="0.09"))
    assert usecases.volumes(["ai detector"], 2826, cfg) == ({"ai detector": {"volume": 6000, "month": "2026-08"}}, 90000)
    sent = json.loads(requests[0].data)
    assert sent["endpoint"] == "/v3/keywords_data/google_ads/search_volume/live"
    assert sent["endpoint_version"] == 2
    assert sent["input"] == {"keywords": ["ai detector"], "location_code": 2826, "language_code": "en"}


def test_keyword_series_uses_two_separately_scaled_calls(cfg, transport):
    requests, replies = transport
    cfg["data_providers"]["trends"] = "glasser"
    replies.extend([completed(graph(["strata"])), completed(graph(["strata", "gpts"]))])
    series, cost = usecases.fetch_series(["strata"], "gpts", date(2026, 10, 10), cfg)
    assert series["strata"]["solo"] == [(date(2026, 9, 1), 5), (date(2026, 9, 2), 10)]
    assert series["strata"]["pair"] == [(date(2026, 9, 1), 5, 5), (date(2026, 9, 2), 10, 10)]
    assert cost == 22000
    assert requests[0].get_header("Idempotency-key") != requests[1].get_header("Idempotency-key")


def test_202_polls_the_same_run_without_another_post(transport):
    requests, replies = transport
    replies.extend([{"id": "run_test_1", "status": "QUEUED"},
                    {"id": "run_test_1", "status": "RUNNING"}, completed()])
    assert glasser.call_all([call()], "trends")[1] == 11000
    assert [r.get_method() for r in requests] == ["POST", "GET", "GET"]
    assert [r.full_url for r in requests[1:]] == [glasser.BASE + "/run_test_1"] * 2
    assert all(r.data is None for r in requests[1:])


@pytest.mark.parametrize("state", ["FAILED", "STOPPED", "UNKNOWN"])
def test_failed_stopped_unknown_runs_are_errors(state, transport):
    requests, replies = transport
    replies.append(completed(status=state))
    with pytest.raises(glasser.GlasserError, match="did not complete"):
        glasser.call_all([call()], "trends")
    assert len(requests) == 1


def test_pending_limit_does_not_resubmit(transport, monkeypatch):
    requests, replies = transport
    monkeypatch.setattr(glasser, "MAX_POLLS", 2)
    replies.extend([{"id": "run_test_1", "status": "RUNNING"}] * 3)
    with pytest.raises(glasser.GlasserError, match="run=run_test_1.*still pending.*charge unknown"):
        glasser.call_all([call()], "trends")
    assert [r.get_method() for r in requests] == ["POST", "GET", "GET"]


@pytest.mark.parametrize("http_status", [404, 500, None, "200"])
def test_completed_run_with_provider_error_is_not_empty(http_status, transport):
    requests, replies = transport
    replies.append(completed(provider_response={"http_status": http_status}))
    with pytest.raises(glasser.GlasserError, match="provider HTTP error"):
        glasser.call_all([call()], "trends")
    assert len(requests) == 1


@pytest.mark.parametrize("output", [native(status=40501), {"status_code": 50000}, {},
                                   {"status_code": 20000, "tasks": []},
                                   {"status_code": 20000, "tasks": [{"status_code": 20000}]}])
def test_dataforseo_task_errors_and_missing_output_raise(output, transport):
    _, replies = transport
    replies.append(completed(output))
    with pytest.raises(glasser.GlasserError, match="DataForSEO"):
        glasser.call_all([call()], "trends")


@pytest.mark.parametrize("empty", [None, []])
def test_explicit_successful_empty_result_is_preserved(empty, transport):
    _, replies = transport
    replies.append(completed(native(empty)))
    assert glasser.call_all([call()], "trends")[0]["one"]["tasks"][0]["result"] == empty


@pytest.mark.parametrize("charge", [None, 0.011, "NaN", "Infinity", "-0.01", "invalid"])
def test_invalid_charges_raise(charge, transport):
    _, replies = transport
    replies.append(completed(charge=charge))
    with pytest.raises(glasser.GlasserError, match="charge_usd"):
        glasser.call_all([call()], "trends")


@pytest.mark.parametrize("error", [TimeoutError("gl_test_only"),
    urllib.error.HTTPError(glasser.BASE, 402, "insufficient balance gl_test_only", {}, io.BytesIO(b"gl_test_only")),
    urllib.error.HTTPError(glasser.BASE, 429, "rate limited", {}, io.BytesIO(b"aisa_test_only"))])
def test_transport_failure_is_not_retried_and_does_not_expose_keys(error, transport):
    requests, replies = transport
    replies.append(error)
    with pytest.raises(glasser.GlasserError) as caught:
        glasser.call_all([call()], "trends")
    assert len(requests) == 1
    assert requests[0].get_header("Idempotency-key") in str(caught.value)
    assert "gl_test_only" not in str(caught.value) and "aisa_test_only" not in str(caught.value)


def test_redirects_cannot_forward_credentials():
    with pytest.raises(glasser.GlasserError, match="redirect refused"):
        glasser._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example")


def test_missing_glasser_key_never_uses_aisa_key(transport, monkeypatch):
    requests, _ = transport
    monkeypatch.delenv("GLASSER_API_KEY")
    with pytest.raises(glasser.GlasserError, match="GLASSER_API_KEY"):
        glasser.call_all([call()], "trends")
    assert requests == []


def test_preflight_rejects_entire_batch_before_any_charge(transport):
    requests, _ = transport
    for bad in [call(keywords=[]), call(keywords=["a"] * 6),
                {**call(), "tool": "post_dataforseo_domains_whois_overview_live"},
                {**call(), "arguments": None}, {**call(), "arguments": {"body": [{}, {}]}}]:
        with pytest.raises(glasser.GlasserError):
            glasser.call_all([call(cid="good"), bad], "trends")
    with pytest.raises(glasser.GlasserError, match="100"):
        glasser.call_all([call("volume", keywords=["a"] * 101)], "volume")
    assert requests == []


def test_unsupported_parameters_never_dropped(transport):
    requests, _ = transport
    c = call()
    c["arguments"]["body"][0]["filters"] = [["domain", "like", "%ai%"]]
    with pytest.raises(glasser.GlasserError, match="no fields were dropped"):
        glasser.call_all([c], "trends")
    with pytest.raises(glasser.GlasserError, match="does not support"):
        glasser.call_all([], "new_sites")
    assert requests == []


@pytest.mark.parametrize("change", ["weekly", "missing", "keyword_order", "values", "gap"])
def test_invalid_trends_data_does_not_become_zero(change, transport):
    _, replies = transport
    output = graph()
    item = output["tasks"][0]["result"][0]["items"][0]
    if change == "weekly":
        item["data"][0]["date_to"] = "2026-09-07"
    elif change == "missing":
        item["data"][0]["missing_data"] = True
    elif change == "keyword_order":
        item["keywords"] = ["wrong term"]
    elif change == "values":
        item["data"][0]["values"] = [None]
    else:
        item["data"][1].update(date_from="2026-09-04", date_to="2026-09-04")
    replies.append(completed(output))
    with pytest.raises(glasser.GlasserError, match="invalid trends"):
        glasser.call_all([call()], "trends")


def test_review_failure_leaves_lead_pending(cfg, tmp_path, transport):
    _, replies = transport
    cfg["data_providers"]["trends"] = "glasser"
    conn = store.connect(tmp_path)
    store.record(conn, [{"term": "strata", "kinds": ["x"], "signals": []}], date(2026, 9, 1))
    replies.append(completed(native(status=40501)))
    result = cli.run(lambda: review.review(cfg, conn, date(2026, 10, 10)))
    assert "error" in result and "DataForSEO" in result["error"]
    assert conn.execute("SELECT reviewed_at, verdict FROM leads").fetchone() == (None, None)


def test_review_success_uses_real_mapping_and_records_hit(cfg, tmp_path, transport):
    _, replies = transport
    cfg["data_providers"]["trends"] = "glasser"
    conn = store.connect(tmp_path)
    store.record(conn, [{"term": "strata", "kinds": ["x"], "signals": []}], date(2026, 9, 10))
    replies.append(completed(graph(values=[[0]] * 9 + [[30], [100], [30]])))
    result = review.review(cfg, conn, date(2026, 10, 10))
    assert result["checked"][0]["verdict"] == "hit"
    assert result["cost_micros"] == 11000
    assert conn.execute("SELECT verdict FROM leads").fetchone() == ("hit",)


def test_polling_time_budget_stops_without_recreating(transport, monkeypatch):
    requests, replies = transport
    replies.append({"id": "run_test_1", "status": "RUNNING"})
    ticks = iter([0, 91])
    monkeypatch.setattr(glasser.time, "monotonic", lambda: next(ticks))
    with pytest.raises(glasser.GlasserError, match="still pending"):
        glasser.call_all([call()], "trends")
    assert len(requests) == 1
