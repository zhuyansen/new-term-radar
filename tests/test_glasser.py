"""Glasser: the same data as AIsa's, asked first when its key is set (0.8). No network: Glasser's answers are
stubbed in the shapes its Runs had on 2026-11-26 (runs_get of real Runs, trimmed)."""
import io
import json
import re
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from radar import (ai_answers, aisa, backend, competition, feeds, glasser, judge, markets, new_sites, report, review,
                   seller_history, sellers, sidelights, site_words, social, sources, store, usecases, wordage)

ROOT = Path(__file__).resolve().parents[1]


def run_of(status="COMPLETED", output=None, http=200, charge="0.002", error=None, failure=None, run_id="run_1"):
    provider = {"http_status": http, **({"error": error} if error is not None else {})}
    return {"id": run_id, "status": status, "charge_usd": charge, "provider_response": provider, "output": output,
            "failure": failure, "charge_basis": {"clause": "rule"}}


def dfs(result, code=20000, message="Ok."):
    """A DataForSEO answer as Glasser hands it back: the provider's own envelope."""
    return {"status_code": 20000, "status_message": "Ok.", "tasks": [{"status_code": code, "status_message": message, "result": result}]}


def stub_runs(monkeypatch, *answers):
    """Glasser answers these Runs in turn; returns what was posted: [(provider, endpoint, version, input)]."""
    sent, left = [], list(answers)

    def request(url, body=None, request_id=""):
        if body is not None:
            sent.append((body["provider"], body["endpoint"], body["endpoint_version"], body["input"]))
        return left.pop(0)
    monkeypatch.setattr(glasser, "_request", request)
    monkeypatch.setattr(glasser, "POLL_SECONDS", 0)
    return sent


# --- which calls go where -------------------------------------------------------------------------------------

def test_glasser_answers_what_it_sells_when_its_key_is_set_else_aisa(monkeypatch):
    asked = []
    monkeypatch.setattr(backend.glasser, "call", lambda calls: (asked.append(("glasser", [c["call_id"] for c in calls])),
                                                               ({c["call_id"]: {"g": 1} for c in calls}, 10))[1])
    monkeypatch.setattr(backend.aisa, "call", lambda calls: (asked.append(("aisa", [c["call_id"] for c in calls])),
                                                            ({c["call_id"]: {"a": 1} for c in calls}, 20))[1])
    trends = {"call_id": "t", "tool": review.TOOL, "arguments": {"body": [{"keywords": ["photocraft"]}]}}
    x = {"call_id": "x", "tool": "get_twitter_user_last_tweets", "arguments": {"userName": "OpenAI"}}
    other = {"call_id": "o", "tool": "get_aisa_only_tool", "arguments": {}}                  # a tool Glasser does not sell
    with pytest.raises(aisa.AisaError, match="set GLASSER_API_KEY, or AISA_API_KEY"):      # neither key: nothing bought
        backend.call([trends])
    assert asked == [] and backend.name() == "AIsa"
    monkeypatch.setattr(backend, "_aisa_key", lambda: "a")
    assert backend.call([trends, x]) == ({"t": {"a": 1}, "x": {"a": 1}}, 20)                # AIsa alone: as before
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    assert backend.call([trends, x]) == ({"t": {"g": 1}, "x": {"g": 1}}, 10)                # X too: AnyAPI on Glasser
    assert asked[-1] == ("glasser", ["t", "x"]) and backend.name() == "Glasser + AIsa"
    assert backend.call([trends, other]) == ({"o": {"a": 1}, "t": {"g": 1}}, 30)             # what Glasser does not sell
    assert asked[-2:] == [("aisa", ["o"]), ("glasser", ["t"])]
    monkeypatch.setattr(backend, "_aisa_key", lambda: "")
    assert backend.call([trends, x]) == ({"t": {"g": 1}, "x": {"g": 1}}, 10) and backend.name() == "Glasser"
    asked.clear()
    with pytest.raises(aisa.AisaError, match="no AIsa key for get_aisa_only_tool, which Glasser does not sell"):
        backend.call([trends, other])
    assert asked == []                                  # the clear error comes before Glasser bills the rest


def test_a_search_with_an_operator_goes_to_aisa_never_glasser(monkeypatch):
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    monkeypatch.setattr(backend, "_aisa_key", lambda: "a")
    monkeypatch.setattr(backend.glasser, "call", lambda calls: pytest.fail("Glasser sells no site: searches"))
    sent = []
    monkeypatch.setattr(backend.aisa, "call", lambda calls: (sent.extend(calls), ({}, 2000))[1])
    feeds.google_new({"google_new": ["figma.com/community/plugin"]})
    assert sent[0]["arguments"]["body"][0]["keyword"] == "site:figma.com/community/plugin"
    assert not glasser.sells(competition.SERP_TOOL, {"body": [{"keyword": "photocraft inurl:pricing"}]})
    assert glasser.sells(competition.SERP_TOOL, {"body": [{"keyword": "photo to anime"}]})
    assert not glasser.sells(markets.VOLUME_TOOL, {"body": [{"keywords": ["ok", "allintitle:tool"]}]})


def test_every_paid_call_of_the_radar_is_mapped_to_glasser():
    tools = {competition.SERP_TOOL, competition.KD_TOOL, feeds.SERP, markets.VOLUME_TOOL, markets.REGION_TOOL, review.TOOL,
             sellers.TOOL, sellers.DOMAINS, social.REDDIT_TOOL, social.COMMENTS_TOOL, usecases.VOLUME_TOOL,
             *(v[0] for v in feeds.TRENDING.values())}
    assert tools <= set(glasser.ENDPOINTS)
    assert {new_sites.PATH, sidelights.WHOIS, site_words.PATH, *ai_answers.PATHS.values()} <= set(glasser.REST)
    x = {usecases.SEARCH_TOOL, "get_twitter_user_last_tweets"}
    assert x <= set(glasser.ENDPOINTS) and not glasser.UNVERIFIED    # X: through AnyAPI since one paid Run of each, 2026-11-26
    # Every tool the code names is one of these: a new one must be mapped or left on AIsa on purpose.
    named = set()
    for path in (ROOT / "radar").glob("*.py"):
        named |= set(re.findall(r'"((?:get|post)_(?:dataforseo|tikhub|twitter|reddit)_\w+)"', path.read_text()))
    assert named == (tools | x)


# The input each endpoint takes (inspect, 2026-11-26): its schema refuses anything else.
ALLOWED = {
    "/v3/serp/google/organic/live/advanced": {"keyword", "location_code", "language_code", "device", "depth", "os", "search_param"},
    "/v3/serp/google/organic/live/regular": {"keyword", "location_code", "language_code", "device", "depth", "search_param"},
    "/v3/dataforseo_labs/google/keyword_overview/live": {"keywords", "location_code", "language_code"},
    "/v3/keywords_data/google_ads/search_volume/live": {"keywords", "location_code", "language_code", "date_from", "date_to"},
    "/v3/keywords_data/dataforseo_trends/explore/live": {"keywords", "date_from", "date_to", "time_range", "location_code", "type"},
    "/v3/keywords_data/dataforseo_trends/subregion_interests/live": {"keywords", "date_from", "date_to", "time_range", "location_code", "type"},
    "/v3/backlinks/backlinks/live": {"target", "mode", "limit", "order_by", "backlinks_status_type", "search_after_token", "filters"},
    "/v3/backlinks/referring_domains/live": {"target", "limit", "order_by", "backlinks_status_type", "filters"},
    "/v3/domain_analytics/whois/overview/live": {"limit", "order_by", "filters", "offset"},
    "/v3/dataforseo_labs/google/ranked_keywords/live": {"target", "limit", "order_by", "filters", "location_code", "language_code"},
    "/v3/ai_optimization/chat_gpt/llm_scraper/live/advanced": {"keyword", "location_code", "language_code"},
    "/v3/serp/google/ai_mode/live/advanced": {"keyword", "location_code", "language_code"},
    "/v1/reddit/search": {"query", "sort", "timeframe", "trim", "after", "filter"},
    "/api/v1/youtube/web_v2/get_video_comments": {"video_id", "need_format", "sort_by", "continuation_token"},
    "/api/v1/douyin/app/v3/fetch_hot_search_list": {"board_type", "board_sub_type"},
    "/api/v1/tiktok/web/fetch_trending_searchwords": set(),
    "/api/v1/tiktok/ads/get_trends_hashtag_list": {"page", "limit", "time_range", "industry_id", "country_code"},
    "/api/v1/reddit/app/fetch_trending_searches": {"language", "need_format"},
}


def test_what_the_radar_sends_fits_each_endpoints_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    sent = []

    def fake_run(provider, endpoint, version, task):
        sent.append((endpoint, task))
        return ({"posts": []} if provider == "scrapecreators" else {"data": {}} if provider == "tikhub" else dfs([])), 1000
    monkeypatch.setattr(glasser, "run", fake_run)
    competition.page_one("photo to anime", 2840)
    competition.difficulty(["photo to anime"], 2840)
    markets.volumes(["photo to anime"], 2276)
    usecases.volumes(["photo to anime"], 2840)
    markets._regions([{"phrase": "photo to anime"}], {"US"})
    usecases.fetch_series(["photo to anime"], "gpts", date(2026, 11, 26))
    wordage.assess([{"phrase": "photo to anime"}], {"word_age": {"enabled": True, "time_range": "past_5_years", "new_days": 180}}, date(2026, 11, 26))
    sellers.links("stripe", 100)
    sellers.started("stripe", 100)
    seller_history.page("polar", "")
    social.reddit('"is there an ai tool"', time="week", sort="top")
    social.youtube_comments("dQw4w9WgXcQ")
    feeds.trending({"trending": list(feeds.TRENDING)})
    for path, task in ((new_sites.PATH, new_sites._task({"window_days": 90, "min_etv": 1000}, date(2026, 11, 26), "%ai%")),
                       (site_words.PATH, {"target": "a.dev", "limit": 5, "filters": [["x", "<=", 20]]}),
                       (ai_answers.PATHS["chatgpt"], {"keyword": "q", "location_code": 2840, "language_code": "en"})):
        backend.rest(path, task)
    assert len({e for e, _ in sent}) >= 16
    for endpoint, task in sent:
        assert set(task) <= ALLOWED[endpoint], (endpoint, set(task) - ALLOWED[endpoint])
    assert ("/v1/reddit/search", {"query": '"is there an ai tool"', "sort": "top", "timeframe": "week"}) in sent


# --- one Run ------------------------------------------------------------------------------------------------------

def test_a_run_is_polled_by_its_id_never_created_twice(monkeypatch):
    seen = []
    answers = iter([run_of("QUEUED"), run_of("RUNNING"), run_of("COMPLETED", dfs([{"items": [1]}]), charge="0.0012")])

    def request(url, body=None, request_id=""):
        seen.append((url, body is not None))
        return next(answers)
    monkeypatch.setattr(glasser, "_request", request)
    monkeypatch.setattr(glasser, "POLL_SECONDS", 0)
    data, cost = glasser.call([{"call_id": "a", "tool": review.TOOL, "arguments": {"body": [{"keywords": ["x"]}]}}])
    assert data == {"a": dfs([{"items": [1]}])} and cost == 1200
    assert [posted for _, posted in seen] == [True, False, False] and seen[1][0].endswith("/v1/runs/run_1")


def answer_with(seen, run):
    def answer(req, timeout):
        seen.append(req)
        return io.BytesIO(json.dumps(run).encode())
    return answer


def test_a_run_carries_the_key_the_endpoint_the_task_and_its_own_idempotency_key(monkeypatch):
    monkeypatch.setenv("GLASSER_API_KEY", "gl_test")
    seen = []
    monkeypatch.setattr(glasser._OPENER, "open", answer_with(seen, run_of("COMPLETED", dfs([{"items": []}]))))
    task = {"keyword": "photo to anime", "location_code": 2840, "language_code": "en", "device": "desktop", "depth": 10}
    glasser.call([{"call_id": "s", "tool": competition.SERP_TOOL, "arguments": {"body": [task]}},
                  {"call_id": "t", "tool": competition.SERP_TOOL, "arguments": {"body": [dict(task, keyword="b")]}}])
    assert json.loads(seen[0].data) == {"provider": "dataforseo", "endpoint": "/v3/serp/google/organic/live/advanced",
                                        "endpoint_version": 17, "input": task}
    assert seen[0].full_url == "https://api.glasser.ai/v1/runs" and seen[0].get_header("Authorization") == "Bearer gl_test"
    ids = [r.get_header("Idempotency-key") for r in seen]
    assert all(ids) and ids[0] != ids[1]


def refusing(code, body, headers=None):
    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, code, "refused", headers or {}, io.BytesIO(json.dumps(body).encode()))
    return refuse


def test_a_rate_limit_or_a_broken_connection_is_sent_once_more_with_the_same_key(monkeypatch):
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    waits, keys = [], []
    monkeypatch.setattr(glasser.time, "sleep", waits.append)
    ok = run_of("COMPLETED", dfs([{"items": []}]))

    def broken(req, timeout):
        raise urllib.error.URLError("connection reset")
    for first in (refusing(429, {"error": {"code": "rate_limited", "message": "Slow down", "retry_after_ms": 1500}}),
                  refusing(503, {"error": {"message": "busy"}}), broken):
        tries = iter([first, lambda req, timeout: io.BytesIO(json.dumps(ok).encode())])

        def opener(req, timeout, tries=tries):
            keys.append(req.get_header("Idempotency-key"))
            return next(tries)(req, timeout)
        monkeypatch.setattr(glasser._OPENER, "open", opener)
        assert list(glasser.call([{"call_id": "a", "tool": review.TOOL, "arguments": {"body": [{"keywords": ["x"]}]}}])[0]) == ["a"]
    assert keys[0] == keys[1] and keys[2] == keys[3] and keys[4] == keys[5]       # the same key: Glasser never bills it twice
    assert len({keys[0], keys[2], keys[4]}) == 3 and waits[0] == 1.5


def test_a_refusal_every_run_would_get_raises_with_glassers_message_never_the_key(monkeypatch):
    monkeypatch.setenv("GLASSER_API_KEY", "gl_secret")
    monkeypatch.setattr(glasser._OPENER, "open", refusing(402, {"error": {"code": "insufficient_balance",
                                                                          "message": "Top up to run this."}}))
    calls = [{"call_id": str(i), "tool": review.TOOL, "arguments": {"body": [{"keywords": [str(i)]}]}} for i in range(3)]
    with pytest.raises(glasser.GlasserError) as exc:
        glasser.call(calls)
    assert str(exc.value) == "Glasser HTTP 402: Top up to run this." and "gl_secret" not in str(exc.value)
    monkeypatch.setattr(glasser._OPENER, "open", refusing(400, {"error": {          # as Glasser answered a site: search, 2026-10-26
        "code": "validation_failed", "message": "Input does not match the endpoint's input schema",
        "details": {"issues": "SchemaError(search operators such as site: or inurl: are not sold on this Endpoint)"}}}))
    with pytest.raises(aisa.AisaError, match="search operators such as site: or inurl: are not sold"):
        glasser.call(calls[:1])


@pytest.mark.parametrize("run, says", [
    (run_of("FAILED", charge="0.00", failure={"kind": "TIMED_OUT", "message": "The provider did not respond within 50000ms"}),
     "ended FAILED: The provider did not respond"),                                   # as a ChatGPT answer timed out, 2026-11
    (run_of("COMPLETED", None, http=400, charge="0.00", error={"detail": {"code": 400, "message": "Request failed. Please retry."}}),
     "tikhub answered HTTP 400"),                                                     # TikHub's refusal, not billed
    (run_of("COMPLETED", dfs([{}]), charge=None), "no readable charge_usd"),
])
def test_a_failed_call_is_left_out_and_a_request_whose_calls_all_failed_raises(monkeypatch, run, says):
    provider = "tikhub" if "tikhub" in says else "dataforseo"
    tool = social.COMMENTS_TOOL if provider == "tikhub" else review.TOOL
    args = {"video_id": "v"} if provider == "tikhub" else {"body": [{"keywords": ["x"]}]}
    stub_runs(monkeypatch, run)
    with pytest.raises(aisa.AisaError, match=says):                     # an AisaError: the callers report it as before
        glasser.call([{"call_id": "a", "tool": tool, "arguments": args}])
    stub_runs(monkeypatch, run_of("COMPLETED", {"data": {"comments": []}} if provider == "tikhub" else dfs([{"items": []}]), charge="0.001"), run)
    data, cost = glasser.call([{"call_id": "a", "tool": tool, "arguments": args}, {"call_id": "b", "tool": tool, "arguments": args}])
    assert list(data) == ["a"] and cost == 1000                          # as AIsa's router leaves out a failed call


def test_no_result_is_an_answer_and_billed_and_a_refused_task_comes_back_as_aisa_gave_it(monkeypatch):
    nothing = dfs([{"items": None, "items_count": 0, "total_count": 0, "search_after_token": None}])     # backlinks, 2026-11
    refused = dfs(None, 40501, "Invalid Field: 'search_after_token'.")
    sent = stub_runs(monkeypatch, run_of("COMPLETED", nothing, charge="0.024"),
                     run_of("COMPLETED", None, charge="0.00", error=refused))
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    assert seller_history.page("polar", "") == ([], "", 24000, "")
    assert seller_history.page("polar", "t1") == ([], "t1", 0, "Invalid Field: 'search_after_token'.")   # its message reaches the caller
    assert sent[1][3]["search_after_token"] == "t1"


def test_charges_are_read_as_exact_decimals():
    for usd, micros in (("0.024036", 24036), ("0.0012", 1200), ("0.00188", 1880), ("0.09", 90000), ("0.00", 0), (0.004, 4000)):
        assert glasser._charge({"charge_usd": usd}) == micros


def test_the_passthrough_paths_through_glasser(monkeypatch):
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    stub_runs(monkeypatch, run_of("COMPLETED", dfs([{"total_count": 3, "items": [{"domain": "ai-hailuo.com"}]}]), charge="0.1236"),
              run_of("COMPLETED", None, charge="0.00", error=dfs(None, 40501, "Invalid Field: 'filters'.")))
    assert backend.rest(sidelights.WHOIS, {"limit": 3}) == ({"total_count": 3, "items": [{"domain": "ai-hailuo.com"}]}, 123600)
    with pytest.raises(aisa.AisaError, match="Invalid Field: 'filters'"):            # as AIsa's passthrough raised it
        backend.rest(sidelights.WHOIS, {"limit": 3})
    monkeypatch.setattr(backend, "_aisa_key", lambda: "a")
    monkeypatch.setattr(backend.aisa, "rest", lambda path, task: ({"via": "aisa"}, 1))
    assert backend.rest("serp/google/organic/live/advanced", {}) == ({"via": "aisa"}, 1)   # a path not mapped


# --- the data each caller reads -----------------------------------------------------------------------------------

def test_page_one_reads_glassers_answer_as_it_read_aisas(monkeypatch):
    page = dfs([{"items": [{"type": "ai_overview"}, {"type": "paid"},
                           {"type": "organic", "rank_group": 1, "domain": "www.lookalike.com", "url": "https://www.lookalike.com/heygen",
                            "title": "9 Best HeyGen Alternatives"},
                           {"type": "organic", "rank_group": 2, "domain": "reddit.com", "url": "https://reddit.com/r/x/1", "title": "Any good one?"}]}])
    monkeypatch.setattr(backend.aisa, "call", lambda calls: ({"serp": page}, 2000))
    monkeypatch.setattr(backend, "_aisa_key", lambda: "a")
    from_aisa = competition.page_one("heygen alternative", 2840)
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    stub_runs(monkeypatch, run_of("COMPLETED", page, charge="0.002"))
    assert competition.page_one("heygen alternative", 2840) == from_aisa
    assert from_aisa[0]["ads"] and from_aisa[0]["features"] == ["ai_overview"] and from_aisa[0]["forums"][0]["domain"] == "reddit.com"


def test_reddit_posts_from_scrapecreators_read_as_from_aisa(monkeypatch):
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    trimmed = {"success": True, "after": "t3_x", "posts": [            # trim: true leaves out permalink and over_18 (a Run of 2026-11)
        {"id": "1wmpogt", "title": "Is there an AI tool that  turns slides into notes?", "selftext": "Asking for my  class",
         "score": 40, "num_comments": 11, "subreddit": "PhD", "created_utc": 1790025277,
         "url": "https://www.reddit.com/r/PhD/comments/1wmpogt/is_there/", "permalink": None},
        {"id": "nsfw1", "title": "x", "selftext": "", "score": 9, "num_comments": 0, "subreddit": "x", "created_utc": 1,
         "url": "https://www.reddit.com/r/x/comments/nsfw1/x/", "permalink": "/r/x/comments/nsfw1/x/", "over_18": True}]}
    stub_runs(monkeypatch, run_of("COMPLETED", trimmed, charge="0.00188"))
    posts, cost = social.reddit('"is there an ai tool"')
    assert cost == 1880 and posts == [{"id": "1wmpogt", "title": "Is there an AI tool that turns slides into notes?",
                                       "text": "Asking for my class", "score": 40, "comments": 11, "subreddit": "PhD",
                                       "url": "https://www.reddit.com/r/PhD/comments/1wmpogt/is_there/", "created": 1790025277.0}]


def test_tikhub_answers_come_back_whole_as_from_aisa(monkeypatch):
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    envelope = lambda data: {"code": 200, "router": "/api/v1/...", "params": {}, "data": data}   # noqa: E731
    stub_runs(monkeypatch, run_of("COMPLETED", envelope({"comments": [{"content": "Can this be  done in one click?", "like_count": 12}]}), charge="0.001"))
    assert social.youtube_comments("dQw4w9WgXcQ") == ([{"text": "Can this be done in one click?", "likes": 12}], 1000)
    stub_runs(monkeypatch, run_of("COMPLETED", envelope({"data": {"word_list": [{"word": "AI换脸特效"}], "trending_list": []}}), charge="0.001"),
              run_of("COMPLETED", envelope({"trending_search_words": [{"trendingSearchWord": "ai photo editor"}]}), charge="0.001"))
    items, cost = feeds.trending({"trending": ["douyin", "tiktok"]})
    assert [i["name"] for i in items] == ["AI换脸特效", "ai photo editor"] and cost == 2000


def test_trends_and_volumes_pass_through_for_the_review_and_the_table(monkeypatch):
    monkeypatch.setenv("GLASSER_API_KEY", "g")
    series = dfs([{"items": [{"type": "dataforseo_trends_graph", "data": [
        {"date_from": "2026-11-01", "date_to": "2026-11-07", "values": [0]}, {"date_from": "2026-11-08", "date_to": "2026-11-14", "values": [40]}]}]}])
    stub_runs(monkeypatch, run_of("COMPLETED", series, charge="0.0012"))
    rows = [{"phrase": "photo to anime"}]
    assert wordage.assess(rows, {"word_age": {"enabled": True, "time_range": "past_5_years", "new_days": 180}}, date(2026, 11, 26)) == (1200, [])
    assert rows[0]["age"]["first_seen"] == "2026-11-08" and rows[0]["is_new"]
    volume = {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [
        {"keyword": "photo to anime", "search_volume": 2900, "monthly_searches": [{"year": 2026, "month": 10, "search_volume": 2900}]}]}]}
    stub_runs(monkeypatch, run_of("COMPLETED", volume, charge="0.09"))
    assert usecases.volumes(["photo to anime"], 2840) == ({"photo to anime": {"volume": 2900, "month": "2026-10", "months": [["2026-10", 2900]]}}, 90000)


def test_x_through_anyapi_reads_as_twitterapi_io(monkeypatch):
    stamp = datetime(2026, 11, 25, 19, 56, 23, tzinfo=timezone.utc).timestamp()
    posts = {"provider": "AnyAPI", "items": 3, "output": {"found": True, "data": {"nextCursor": None, "tweets": [
        {"id": "1", "text": "Introducing Opus 5.5", "url": "https://x.com/AnthropicAI/status/1", "likes": 900, "createdUtc": stamp,
         "authorHandle": "AnthropicAI", "isPinned": False},
        {"id": "2", "text": "a repost of someone else", "url": "https://x.com/other/status/2", "likes": 5, "createdUtc": stamp,
         "authorHandle": "other"}]}}}
    stub_runs(monkeypatch, run_of("COMPLETED", posts, charge="0.00044"))
    data, cost = glasser.run_tool("get_twitter_user_last_tweets", {"userName": "AnthropicAI"})
    tweets = data["data"]["tweets"]
    assert cost == 440 and [t["id"] for t in tweets] == ["1"] and tweets[0]["likeCount"] == 900
    assert sources._posted(tweets[0]) == datetime(2026, 11, 25, 19, 56, 23, tzinfo=timezone.utc)
    found = {"output": {"found": True, "data": {"items": [{"id": "9", "text": "is there a tool that", "likeCount": 12, "createdUtc": stamp,
                                                           "url": "https://x.com/a/status/9", "authorUsername": "a"}]}}}
    stub_runs(monkeypatch, run_of("COMPLETED", found, charge="0.00049"))
    data, _ = glasser.run_tool(usecases.SEARCH_TOOL, {"query": '"opus 5.5" min_faves:50', "queryType": "Top"})
    assert data == {"tweets": [{"id": "9", "text": "is there a tool that", "url": "https://x.com/a/status/9", "likeCount": 12,
                                "createdAt": "Wed Nov 25 19:56:23 +0000 2026", "author": {"userName": "a"}}]}
    stub_runs(monkeypatch, run_of("COMPLETED", {"output": {"found": True, "data": {"posts": []}}}, charge="0.00049"))
    with pytest.raises(glasser.GlasserError, match="not the shape the radar reads"):     # a changed answer is an error, not zero posts
        glasser.run_tool(usecases.SEARCH_TOOL, {"query": "x"})
    # The real timeline answer of 2026-11-26: AnyAPI left every authorHandle blank and set authorId; the owner is
    # the authorId most posts share, a repost carries another one, and the handle asked for fills the author.
    blank = {"provider": "AnyAPI", "items": 3, "output": {"found": True, "data": {"nextCursor": "c", "tweets": [
        {"id": "21", "text": "For ordinary days", "url": "https://x.com/i/web/status/21", "likes": 1898, "createdUtc": stamp,
         "authorHandle": "", "authorId": "4398626122"},
        {"id": "22", "text": "another post", "url": "https://x.com/i/web/status/22", "likes": 7, "createdUtc": stamp,
         "authorHandle": "", "authorId": "4398626122"},
        {"id": "23", "text": "a repost", "url": "https://x.com/i/web/status/23", "likes": 3, "createdUtc": stamp,
         "authorHandle": "", "authorId": "999"}]}}}
    stub_runs(monkeypatch, run_of("COMPLETED", blank, charge="0.00045"))
    data, cost = glasser.run_tool("get_twitter_user_last_tweets", {"userName": "OpenAI"})
    tweets = data["data"]["tweets"]
    assert cost == 450 and [t["id"] for t in tweets] == ["21", "22"] and tweets[0]["author"]["userName"] == "OpenAI", tweets


# --- the model ----------------------------------------------------------------------------------------------------

def test_the_model_comes_from_radar_llm_first_then_aisa_and_without_either_the_judge_is_skipped(monkeypatch):
    rows = [{"term": "opus 5.5", "examples": ["@a: Opus 5.5 is out"]}]
    assert backend.llm() == "" and judge.score(rows, "gpt-4.1-mini") == {"judged": 0, "skipped": True, "no_llm": (
        "no model to ask: set RADAR_LLM_BASE_URL (any OpenAI-compatible endpoint, with RADAR_LLM_API_KEY and RADAR_LLM_MODEL) or AISA_API_KEY")}
    lines = report._extra_status({"judge": judge.score(rows, "gpt-4.1-mini")})
    assert lines == ["- name judge: skipped, no model to ask (set RADAR_LLM_BASE_URL for any OpenAI-compatible endpoint, or AISA_API_KEY): "
                     "every term is listed, unscored"]
    monkeypatch.setattr(backend, "_aisa_key", lambda: "a")
    monkeypatch.setattr(backend.aisa, "chat", lambda model, system, user, max_tokens=1500: '[{"i": 0, "score": 0.9}]')
    assert backend.llm() == "AIsa" and judge.score(rows, "gpt-4.1-mini")["judged"] == 1
    seen = []
    monkeypatch.setenv("RADAR_LLM_BASE_URL", "https://llm.example/v1/")
    monkeypatch.setenv("RADAR_LLM_API_KEY", "sk-local")
    monkeypatch.setenv("RADAR_LLM_MODEL", "qwen3-32b")
    monkeypatch.setattr(backend._OPENER, "open", answer_with(seen, {"choices": [{"message": {"content": '[{"i": 0, "score": 0.8}]'}}],
                                                                     "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}))
    backend.take_chat_cost()
    assert judge.score(rows, "gpt-4.1-mini") == {"judged": 1, "cost_micros": 560} and rows[0]["named"] == 0.8
    body = json.loads(seen[0].data)
    assert seen[0].full_url == "https://llm.example/v1/chat/completions" and seen[0].get_header("Authorization") == "Bearer sk-local"
    assert body["model"] == "qwen3-32b" and body["messages"][0]["role"] == "system"


def test_without_a_model_the_asks_say_so_once():
    from radar import asks
    posts = [{"title": f"is there a tool {i}", "text": "", "subreddit": "x"} for i in range(45)]      # three chunks
    kept, errors = asks.classify("gpt-4.1-mini", posts)
    assert kept == [] and len(errors) == 1 and errors[0].startswith("no model to ask")


def test_without_a_model_the_keyword_table_still_reads_the_names(monkeypatch, cfg, tmp_path):
    monkeypatch.setattr(usecases, "evidence", lambda names, repos, today, texts=None: ({n: {"posts": [{"text": "x", "likes": 1, "by": "a", "url": ""}],
                                                                                             "readme": "", "repo": "", "error": ""} for n in names}, 0))
    monkeypatch.setattr(usecases, "volumes", lambda phrases, loc: ({}, 0))
    monkeypatch.setattr(usecases, "fetch_series", lambda keywords, bench, today: ({}, 0))
    uc = usecases.run(cfg, tmp_path, ["photocraft"], {}, today=date(2026, 11, 26))
    assert [r["phrase"] for r in uc["rows"]] == ["photocraft"] and uc["no_model"].startswith("no model to ask")
    assert usecases.degraded(uc) and any("No model proposed generic terms" in line for line in report.keyword_table(cfg, uc))


# --- a review whose call failed -------------------------------------------------------------------------------------

def test_a_lead_whose_review_call_failed_stays_due_then_closes_as_no_data(monkeypatch, cfg, tmp_path):
    conn = store.connect(tmp_path)
    store.record(conn, [{"term": "photocraft", "kinds": ["x"], "signals": ["x"], "variants": []}], date(2026, 10, 1))
    monkeypatch.setattr(review.backend, "call_all", lambda calls: ({}, 0))      # the call failed: left out, as AIsa leaves it out
    got = review.review(cfg, conn, today=date(2026, 11, 26))                       # 56 days on: past the retry window
    assert got["checked"] == [] and conn.execute("SELECT verdict FROM leads WHERE term = 'photocraft'").fetchone() == ("no_data",)


# --- the docs -------------------------------------------------------------------------------------------------------

def test_glasser_is_the_first_key_everywhere_and_aisas_referral_links_are_gone():
    link = "https://link.glasser.ai/JasonZhuX"
    en, zh = (ROOT / "README.md").read_text(), (ROOT / "README.zh-CN.md").read_text()
    assert f"[Glasser]({link})" in en and "That link is a referral link" in en.split("## Keys: Glasser first")[1][:900]
    assert f"[Glasser]({link})" in zh and "上面是推荐链接" in zh.split("## Key：首选 Glasser")[1][:600]
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert list(plugin["userConfig"])[0] == "glasser_api_key" and link in plugin["userConfig"]["glasser_api_key"]["description"]
    assert list(plugin["mcpServers"]["new-term-radar"]["env"])[0] == "GLASSER_API_KEY"
    server = json.loads((ROOT / "server.json").read_text())
    assert server["packages"][0]["environmentVariables"][0]["name"] == "GLASSER_API_KEY"
    for name in ("new-term-radar", "ai-listening", "seo-new-keywords-research"):
        assert "GLASSER_API_KEY" in (ROOT / "skills" / name / "SKILL.md").read_text()
    assert "secrets.GLASSER_API_KEY" in (ROOT / ".github" / "workflows" / "radar.yml").read_text()
    shipped = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in (".md", ".json", ".py", ".yml", ".toml")
               and ".git" not in p.parts and p.name != "CHANGELOG.md" and "tests" not in p.parts]
    assert [p.name for p in shipped if re.search(r"go\.aisa\.one|aisa\.one/\S*(ref|aff|invite)", p.read_text())] == []
