"""The radar's pure logic: name extraction, signal scoring, the store, the judge's parsing,
the review's verdict, the reports. Network calls are not exercised here."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from radar import __main__ as cli
from radar import breakout, config, gsc, i18n, judge, new_sites, notify, report, review, sitemaps, store
from radar import page_age as _page_age
from radar.sources import base_name, novel_names
from radar.terms import names

REAL_ASK = _page_age.ask       # conftest stubs RDAP for every test; the RDAP test needs the real one

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


# --- names(): what a post can name -------------------------------------------------

def test_names_keeps_versioned_model_names_whole():
    found = names("Introducing Claude Opus 5.5, our best model.")
    assert "claude opus 5.5" in found and "opus 5.5" in found


def test_names_drops_phrases_made_only_of_stop_words():
    assert "claude code" not in names("Try it in Claude Code today")


def test_names_takes_a_name_that_starts_the_sentence():
    assert "strata" in names("Strata now runs Qwen3.8-Flash-Next on a 12GB RTX 5070. Big day.")


def test_names_stops_at_the_end_of_a_sentence():
    assert not any("big" in n and "5070" in n for n in names("on a RTX 5070. Big day."))


def test_names_ignores_urls_and_handles():
    assert names("see https://Example.com/Foo by @SomeOne") == set()


def test_names_breaks_on_chinese_text_between_words():
    found = names("用 Opus 5.5 配音（Gemini 3.8 Flash TTS，我提供了 API Key）")
    assert "gemini 3.8 flash" in found and not any("tts" in n and "api" in n for n in found)


# --- ① X: only names the accounts have not used before ------------------------------

def _post(account, text, likes=0, hours_ago=2):
    return {"account": account, "text": text, "likes": likes, "url": "", "created": NOW - timedelta(hours=hours_ago)}


def test_x_reports_a_new_name_two_accounts_used():
    rows = novel_names([_post("a", "Strata is fast"), _post("b", "Tried Strata today")], {}, NOW)
    assert [r["term"] for r in rows] == ["strata"] and rows[0]["sources"] == ["a", "b"]
    assert rows[0]["note"] == "X: 2 accounts, 0 likes"


def test_x_skips_names_already_in_the_vocabulary():
    rows = novel_names([_post("a", "Strata is fast"), _post("b", "Tried Strata today")], {"strata": "2026-09-01"}, NOW)
    assert rows == []


def test_x_one_account_needs_likes_and_an_unmistakable_name():
    assert novel_names([_post("a", "Qwen3.9 is fast", likes=20)], {}, NOW) == []
    assert [r["term"] for r in novel_names([_post("a", "Qwen3.9 is fast", likes=500)], {}, NOW)] == ["qwen3.9"]
    # a plain capitalised word may only be the start of a sentence: one account is not enough
    assert novel_names([_post("a", "Strata is fast", likes=500)], {}, NOW) == []


def test_x_one_post_yields_at_most_three_names():
    post = _post("a", "New: Alpha1 Beta2 Gamma3 Delta4 Eps5 Zeta6 models", likes=900)
    assert len(novel_names([post], {}, NOW)) <= 3


def test_x_ignores_old_posts_and_retweets():
    old = [_post("a", "Strata is fast", hours_ago=48), _post("b", "Strata rocks", hours_ago=48)]
    rts = [_post("a", "RT @x: Strata is fast"), _post("b", "RT @x: Strata rocks")]
    assert novel_names(old, {}, NOW) == [] and novel_names(rts, {}, NOW) == []


# --- ① HF: copies of one model group under its base name -----------------------------

def test_hf_base_name_strips_quantisation_and_size():
    assert base_name("abenzerps/Qwen-Image-2.1-Uncensored-GGUF") == "qwen image 2.1"
    assert base_name("Qwen/Qwen3.8-Flash-Next-A6B-Instruct") == "qwen3.8 flash next"
    assert base_name("Lightricks/LTX-2.5") == "ltx 2.5"


def test_hf_reads_trending_models_and_spaces_and_merges_a_model_with_its_demo(tmp_path, monkeypatch):
    import radar.sources as src
    lists = {"models": [{"id": "old/Old-Model"}], "spaces": [{"id": "old/old-demo"}]}
    monkeypatch.setattr(src, "_trending", lambda what: lists[what])
    first = src.hf_new({}, tmp_path)
    assert first["first_run"] and first["terms"] == [] and first["models"] == 1 and first["spaces"] == 1
    lists["models"] = [{"id": "krea/Krea-2", "pipeline_tag": "text-to-image", "likes": 900}, {"id": "old/Old-Model"}]
    lists["spaces"] = [{"id": "krea/Krea-2-demo", "sdk": "gradio", "likes": 300}, {"id": "lighton/LightOnOCR-3-webgpu", "sdk": "static"}]
    later = src.hf_new({}, tmp_path)
    terms = {r["term"]: r for r in later["terms"]}
    assert set(terms) == {"krea 2", "lightonocr 3"} and not later["first_run"]
    assert terms["krea 2"]["examples"] == ["krea/Krea-2 (text-to-image, ♥900)", "krea/Krea-2-demo (gradio, ♥300)"]
    assert terms["lightonocr 3"]["note"] == "Hugging Face Spaces: new at #2"


# --- ② breakout: one repo taking off --------------------------------------------------

def _repo(name, stars, days_old):
    created = (NOW - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
    return {"full_name": name, "stargazers_count": stars, "created_at": created, "description": ""}


def test_breakout_flags_a_fast_repo_and_skips_a_slow_one():
    rows = breakout.score([_repo("Niko1221/Strata", 7734, 9), _repo("a/slow", 900, 12)], None, NOW)
    assert [r["repo"] for r in rows] == ["Niko1221/Strata"]


def test_breakout_flags_a_jump_since_yesterday_even_when_the_average_is_low():
    rows = breakout.score([_repo("a/late", 1500, 13)], {"a/late": 900}, NOW)
    assert rows and rows[0]["delta"] == 600


def test_breakout_variants_include_handle_and_display_name():
    row = {"owner": "Niko1221", "name": "Strata"}
    assert breakout.variants(row, "Niko") == ["strata", "niko1221 strata", "niko strata"]
    assert breakout.variants(row, "") == ["strata", "niko1221 strata"]


# --- ③ sitemaps: one entry in 17 languages counts once ---------------------------------

def test_locale_prefix_is_stripped():
    urls = ["https://lobehub.com/skills/x", "https://lobehub.com/zh/skills/x", "https://lobehub.com/zh-TW/skills/x"]
    assert {re.sub(sitemaps._LOCALE, r"\1", u) for u in urls} == {"https://lobehub.com/skills/x"}


def test_paths_are_not_mistaken_for_locales():
    assert re.sub(sitemaps._LOCALE, r"\1", "https://lobehub.com/mcp/foo") == "https://lobehub.com/mcp/foo"


def test_sitemap_diff_reports_a_word_new_urls_share(cfg, tmp_path, monkeypatch):
    cfg["sitemaps"] = {"site": {"index": "https://example.com/sitemap.xml", "pause": 0}}
    old = {f"https://example.com/tools/thing-{i}" for i in range(40)}
    new = old | {f"https://example.com/tools/hyperframes-{w}" for w in ("starter", "captions", "studio")}
    answers = iter([old, new])
    monkeypatch.setattr(sitemaps, "site_urls", lambda *a: next(answers))
    first = sitemaps.diff(cfg, tmp_path)
    assert first["sites"]["site"]["first_run"] and first["terms"] == []
    second = sitemaps.diff(cfg, tmp_path)
    assert second["sites"]["site"]["new"] == 3 and second["terms"][0]["term"] == "hyperframes"


def test_a_site_that_fails_keeps_its_snapshot(cfg, tmp_path, monkeypatch):
    cfg["sitemaps"] = {"site": {"index": "https://example.com/sitemap.xml", "pause": 0}}
    monkeypatch.setattr(sitemaps, "site_urls", lambda *a: {"https://example.com/a"})
    sitemaps.diff(cfg, tmp_path)

    def down(*a):
        raise OSError("503")
    monkeypatch.setattr(sitemaps, "site_urls", down)
    assert "error" in sitemaps.diff(cfg, tmp_path)["sites"]["site"]
    monkeypatch.setattr(sitemaps, "site_urls", lambda *a: {"https://example.com/a"})
    assert sitemaps.diff(cfg, tmp_path)["sites"]["site"]["new"] == 0


# --- ③ new sites (whois overview, weekly) -----------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures/whois_new_ai_sites.json"   # a real answer, 2026-10-06


def test_new_site_name_terms():
    assert new_sites.name_terms("ai-hailuo.com") == ["hailuo"]
    assert new_sites.name_terms("livephoto.video") == ["livephoto"]
    assert new_sites.name_terms("ai-video-tool.com") == []   # only stop words left


def test_new_sites_drop_domains_ranking_for_a_handful_of_terms(cfg, tmp_path, monkeypatch):
    sent = []
    cfg["new_sites"]["words"] = 0                              # the words each site ranks for: their own test below
    monkeypatch.setattr(new_sites.aisa, "rest", lambda path, task: (sent.append((path, task)) or {"items": json.loads(FIXTURE.read_text())}, 121200))
    out = new_sites.new_sites(cfg, tmp_path, date(2026, 10, 6))
    domains = [r["domain"] for r in out["sites"]]
    assert "ai-hailuo.com" in domains and "neonwin386.ai" not in domains and "oyo88.ai" not in domains
    assert all(r["new"] for r in out["sites"]) and "hailuo" in [t["term"] for t in out["terms"]]
    assert sent[0][0] == "domain_analytics/whois/overview/live" and out["cost_micros"] == 121200 * len(cfg["new_sites"]["queries"])
    again = new_sites.new_sites(cfg, tmp_path, date(2026, 10, 13))
    assert not any(r["new"] for r in again["sites"]) and again["terms"] == []


def test_new_sites_query_needs_both_dates(cfg):
    body = new_sites._task(cfg["new_sites"], date(2026, 10, 6), "%ai%")
    fields = [f[0] for f in body["filters"] if isinstance(f, list)]
    assert fields[:2] == ["created_datetime", "first_seen"] and body["filters"][0][2].startswith("2026-07-08")


def ranked(word, pos, loc=2840, vol=30, kd=0, new=True):
    """One item of DataForSEO Labs' ranked keywords."""
    return {"keyword_data": {"keyword": word, "location_code": loc, "keyword_info": {"search_volume": vol},
                             "keyword_properties": {"keyword_difficulty": kd}},
            "ranked_serp_element": {"serp_item": {"rank_absolute": pos, "relative_url": "/" + word.replace(" ", "-"),
                                                  "rank_changes": {"is_new": new}},
                                    "previous_updated_time": "2026-08-26 03:00:00 +00:00", "last_updated_time": "2026-10-06 03:00:00 +00:00"}}


def test_a_new_site_comes_with_the_first_words_it_ranks_for(cfg, tmp_path, monkeypatch):
    from radar import site_words
    asked = []

    def rest(path, task):
        asked.append((path, task.get("target")))
        if path == new_sites.PATH:
            return {"items": json.loads(FIXTURE.read_text())}, 121200
        if len(asked) == 2:                                    # the first site asked ranks; the second does not yet
            return {"total_count": 7, "items": [ranked("typing test 15 seconds", 1), ranked("股价走势", 6, loc=2158, vol=1000, kd=None, new=False)]}, 12240
        return {"total_count": None, "items": None}, 12000
    monkeypatch.setattr(new_sites.aisa, "rest", rest)
    cfg["new_sites"]["words"] = 2
    out = new_sites.new_sites(cfg, tmp_path, date(2026, 10, 6))
    worded = [r for r in out["sites"] if "words" in r]
    assert len(worded) == 2 and [p for p, d in asked].count(site_words.PATH) == 2
    hailuo = next(r for r in out["sites"] if r["domain"] == asked[1][1])
    assert hailuo["top"] == 7 and hailuo["words"][0] == {"word": "typing test 15 seconds", "position": 1, "volume": 30, "kd": 0,
                                                         "country": "US", "url": "/typing-test-15-seconds",
                                                         "since": "2026-08-26..2026-10-06"}
    assert hailuo["words"][1]["country"] == "TW" and hailuo["words"][1]["since"] == ""
    assert out["cost_micros"] == 121200 * len(cfg["new_sites"]["queries"]) + 12240 + 12000
    lines = "\n".join(report._site_word_lines(worded))
    assert f"- {asked[1][1]}: ⏱ typing test 15 seconds #1 US, 30/mo, KD 0; 股价走势 #6 TW, 1000/mo, KD —" in lines
    assert "(of 7 in the top 20)" in lines and "none in the top 20 yet" in lines
    again = new_sites.new_sites(cfg, tmp_path, date(2026, 10, 13))       # seen before: no words asked again
    assert not any("words" in r for r in again["sites"])


def test_a_failed_words_call_is_named_and_the_sites_still_come(cfg, tmp_path, monkeypatch):
    def rest(path, task):
        if path == new_sites.PATH:
            return {"items": json.loads(FIXTURE.read_text())}, 121200
        raise new_sites.aisa.AisaError("Internal Server Error.")
    monkeypatch.setattr(new_sites.aisa, "rest", rest)
    cfg["new_sites"]["words"] = 1
    out = new_sites.new_sites(cfg, tmp_path, date(2026, 10, 6))
    assert out["sites"] and len(out["errors"]) == 1 and out["errors"][0].endswith("words: Internal Server Error.")


def test_rest_reads_a_dataforseo_answer_and_its_cost(monkeypatch):
    from radar import aisa
    monkeypatch.setattr(aisa, "_post", lambda url, body, timeout: {"status_code": 20000, "cost": 0.1212,
                                                                    "tasks": [{"status_code": 20000, "result": [{"total_count": 38912}]}]})
    assert aisa.rest("domain_analytics/whois/overview/live", {"limit": 1}) == ({"total_count": 38912}, 121200)
    monkeypatch.setattr(aisa, "_post", lambda url, body, timeout: {"status_code": 20000, "tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]})
    try:
        aisa.rest("x", {})
        raise AssertionError("a refused task must raise")
    except aisa.AisaError as exc:
        assert "Invalid Field" in str(exc)


# --- ④ Search Console ---------------------------------------------------------------------

def test_gsc_keeps_queries_the_earlier_window_never_saw():
    recent = [{"query": "hyperframes skill", "page": "/a", "impressions": 4, "clicks": 1},
              {"query": "hyperframes skill", "page": "/b", "impressions": 3, "clicks": 0},
              {"query": "ppt skill", "page": "/c", "impressions": 90, "clicks": 9}]
    fresh = gsc.fresh_queries(recent, [{"query": "ppt skill"}])
    assert fresh == [{"query": "hyperframes skill", "impressions": 7, "clicks": 1, "page": "/a"}]


def test_gsc_is_off_without_a_site(cfg):
    assert gsc.gsc_new(cfg) == {"off": True}


# --- the name judge ------------------------------------------------------------------------

def test_judge_parses_scores_and_drops_what_it_cannot_use():
    answer = 'Here you go:\n[{"i": 0, "score": 0.9}, {"i": 1, "score": "0.1"}, {"i": 7, "score": 1}, {"i": 2, "score": 3}, {"x": 1}]'
    assert judge.parse(answer, 3) == {0: 0.9, 1: 0.1}
    assert judge.parse("sorry", 3) == {}


def test_judge_scores_rows_in_chunks(monkeypatch):
    rows = [{"term": f"t{i}", "examples": []} for i in range(judge.CHUNK + 2)]
    sizes = []

    def chat(model, system, user):
        n = len(json.loads(user))
        sizes.append(n)
        return json.dumps([{"i": i, "score": 0.8} for i in range(n)])
    monkeypatch.setattr(judge.aisa, "chat", chat)
    assert judge.score(rows, "m") == {"judged": judge.CHUNK + 2} and sizes == [judge.CHUNK, 2]
    assert all(r["named"] == 0.8 for r in rows)


def test_judge_failure_leaves_rows_unscored(monkeypatch):
    def chat(*a):
        raise judge.aisa.AisaError("HTTP 402: no credit")
    monkeypatch.setattr(judge.aisa, "chat", chat)
    rows = [{"term": "strata", "examples": []}]
    assert "error" in judge.score(rows, "m") and "named" not in rows[0]


# --- merging the signals ---------------------------------------------------------------------

def _no_judge(monkeypatch):
    monkeypatch.setattr(report.judge, "score", lambda rows, model: {"judged": 0})


def test_leads_merge_a_term_seen_by_two_signals(cfg, monkeypatch):
    _no_judge(monkeypatch)
    blocks = {"x": {"terms": [{"term": "strata", "recent": 2, "note": "X: 2 accounts"}]},
              "breakout": {"terms": [{"term": "strata", "recent": 1, "note": "GitHub breakout",
                                      "variants": ["strata", "niko1221 strata"]}]},
              "sitemap": {"terms": [{"term": "other", "recent": 3, "lift": 4.0}]}}
    rows, _ = report.leads(cfg, blocks)
    assert rows[0]["term"] == "strata" and rows[0]["kinds"] == ["x", "breakout"]
    assert rows[0]["variants"] == ["strata", "niko1221 strata"]
    assert rows[1]["signals"] == ["③ sitemaps: 3 places (×4.0)"]


def test_names_lead_and_known_terms_are_not_recorded(cfg, monkeypatch):
    cfg["known_terms"] = ["Remotion"]

    def score(rows, model):
        for r in rows:
            r["named"] = {"dashboard": 0.1, "remotion skill": 0.9, "hyperframes": 0.95}[r["term"]]
        return {"judged": len(rows)}
    monkeypatch.setattr(report.judge, "score", score)
    blocks = {"sitemap": {"terms": [{"term": t, "recent": 3, "lift": 2.0} for t in ("dashboard", "remotion skill", "hyperframes")]}}
    rows, _ = report.leads(cfg, blocks)
    assert [r["term"] for r in report.named(cfg, rows)] == ["hyperframes", "remotion skill"]
    assert [r["term"] for r in report.to_record(cfg, rows)] == ["hyperframes"]


def test_known_terms_match_either_way(cfg):
    cfg["known_terms"] = ["ppt skills"]
    assert config.known(cfg, "ppt") == "ppt skills" and config.known(cfg, "best ppt skills") == "ppt skills"
    assert config.known(cfg, "hyperframes") == ""


# --- the store ---------------------------------------------------------------------------------

def _lead(term, kinds, **more):
    return {"term": term, "kinds": kinds, "signals": [f"note {term}"], **more}


def test_store_upserts_and_merges(tmp_path):
    conn = store.connect(tmp_path)
    store.record(conn, [_lead("strata", ["x"], named=0.9, variants=["strata"])], date(2026, 9, 25))
    store.record(conn, [_lead("strata", ["breakout"], variants=["strata", "niko strata"])], date(2026, 9, 26))
    row = conn.execute("SELECT first_seen, last_seen, signals, named, variants FROM leads").fetchone()
    assert row[:2] == ("2026-09-25", "2026-09-26") and json.loads(row[2]) == ["breakout", "x"]
    assert row[3] == 0.9 and json.loads(row[4]) == ["strata", "niko strata"]


def test_store_due_verdict_and_summary(tmp_path):
    conn = store.connect(tmp_path)
    store.record(conn, [_lead("strata", ["x", "breakout"]), _lead("flop", ["sitemap"])], date(2026, 9, 25))
    store.record(conn, [_lead("young", ["hf"])], date(2026, 10, 8))
    today = date(2026, 10, 10)
    assert [d[0] for d in store.due(conn, today, 14, 5)] == ["flop", "strata"]
    store.save_verdict(conn, "strata", "hit", 2, {}, today)
    store.save_verdict(conn, "flop", "miss", None, {}, today)
    assert store.due(conn, today, 14, 5) == []
    summ = store.summary(conn, today)
    assert summ["pending"] == 1 and summ["by_signal"]["x"] == {"checked": 1, "hit": 1, "median_lead": 2}
    assert summ["by_signal"]["sitemap"] == {"checked": 1, "hit": 0, "median_lead": None}


def test_store_recent_sources_are_only_x_and_hf(tmp_path):
    conn = store.connect(tmp_path)
    store.record(conn, [_lead("qwen3.9", ["x"]), _lead("strata", ["breakout"])], date(2026, 10, 10))
    assert [r["term"] for r in store.recent_sources(conn, date(2026, 10, 10))] == ["qwen3.9"]


# --- ⑤ review: did it rise, judged against its own baseline ----------------------------------

SEEN = date(2026, 9, 25)


def _series(values_by_day):
    start = date(2026, 9, 19)
    return [(start + timedelta(days=i), v) for i, v in enumerate(values_by_day)]


def test_review_a_word_with_an_older_meaning_does_not_count_as_rising():
    # 'strata' alone, 2026-09-19..10-02, as DataForSEO returned it: high baseline throughout
    assert not review.rose(_series([40, 67, 70, 67, 76, 80, 68, 62, 70, 100, 89, 98, 36, 33]), SEEN)["rose"]


def test_review_a_new_name_rising_from_zero_counts_and_dates_the_rise():
    out = review.rose(_series([0, 0, 0, 0, 0, 0, 0, 0, 0, 30, 30, 100, 0, 0]), SEEN)
    assert out["rose"] and out["first_rise"] == "2026-09-28"


def test_review_all_zero_is_no_data():
    assert review.rose(_series([0] * 14), SEEN)["no_data"]


def test_review_verdict_takes_the_earliest_variant_that_rose():
    per = {"strata": {"rose": False, "no_data": False},
           "niko1221 strata": {"rose": True, "first_rise": "2026-09-27"},
           "niko strata": {"rose": True, "first_rise": "2026-09-30"}}
    assert review.verdict(per, SEEN) == ("hit", 2)


def test_review_verdict_miss_and_no_data():
    assert review.verdict({"a": {"rose": False, "no_data": False}}, SEEN) == ("miss", None)
    assert review.verdict({"a": {"rose": False, "no_data": True}}, SEEN) == ("no_data", None)


def test_review_sends_one_trends_task_per_call():
    # DataForSEO's live endpoint runs only the first task of a body
    due = [("strata", date(2026, 9, 25), ["strata", "niko1221 strata", "niko strata", "a fourth"]), ("veo 3 free", date(2026, 10, 3), [])]
    calls, plan = review.plan_calls(due, date(2026, 10, 17))
    assert len(calls) == 4 and all(len(c["arguments"]["body"]) == 1 for c in calls)
    assert [c["arguments"]["body"][0]["keywords"] for c in calls[:3]] == [["strata"], ["niko1221 strata"], ["niko strata"]]
    assert calls[3]["arguments"]["body"][0]["keywords"] == ["veo 3 free"]
    assert calls[0]["arguments"]["body"][0]["date_to"] == "2026-10-09" and calls[3]["arguments"]["body"][0]["date_to"] == "2026-10-17"


def test_review_stores_the_verdict(cfg, tmp_path, monkeypatch):
    conn = store.connect(tmp_path)
    store.record(conn, [_lead("niko strata", ["breakout"])], SEEN)
    points = [{"date_from": (date(2026, 9, 19) + timedelta(days=i)).isoformat(), "values": [v]}
              for i, v in enumerate([0, 0, 0, 0, 0, 0, 0, 0, 0, 30, 30, 100, 0, 0])]
    answer = {"tasks": [{"result": [{"items": [{"data": points}]}]}]}
    monkeypatch.setattr(review.aisa, "call_all", lambda calls: ({c["call_id"]: answer for c in calls}, 1200))
    out = review.review(cfg, conn, date(2026, 10, 10))
    assert out["checked"] == [{"term": "niko strata", "first_seen": "2026-09-25", "verdict": "hit", "lead_days": 3}]
    assert conn.execute("SELECT verdict, lead_days FROM leads").fetchone() == ("hit", 3)


# --- the two commands ---------------------------------------------------------------------------

def _write_config(tmp_path, **more):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"x_accounts": {"labs": ["a"]}, **more}))
    return path


def test_sources_run_writes_no_report_when_nothing_is_new(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sources, "x_new", lambda cfg, state: {"posts": 400, "terms": []})
    monkeypatch.setattr(cli.sources, "hf_new", lambda cfg, state: {"models": 100, "terms": []})
    out = tmp_path / "sources.md"
    assert cli.main(["sources", "--config", str(_write_config(tmp_path)), "--state", str(tmp_path / "s"), "--out", str(out)]) == 0
    assert not out.exists()


def test_sources_run_reports_and_keeps_a_new_name(tmp_path, monkeypatch):
    _no_judge(monkeypatch)
    monkeypatch.setattr(cli.sources, "x_new", lambda cfg, state: {"posts": 400, "cost_micros": 72000, "terms": [
        {"term": "qwen3.9", "recent": 2, "note": "X: 2 accounts, 900 likes", "examples": ["@Alibaba_Qwen: Qwen3.9 is out"],
         "posts": [{"account": "Alibaba_Qwen", "text": "Qwen3.9 is out", "url": "https://x.com/p/1", "likes": 900}]}]})
    monkeypatch.setattr(cli.sources, "hf_new", lambda cfg, state: {"models": 100, "terms": []})
    out, state = tmp_path / "sources.md", tmp_path / "s"
    cli.main(["sources", "--config", str(_write_config(tmp_path)), "--state", str(state), "--out", str(out)])
    text = out.read_text()
    assert "**qwen3.9**" in text and "$0.072" in text and "[post](https://x.com/p/1)" in text
    assert store.connect(state).execute("SELECT term FROM leads").fetchall() == [("qwen3.9",)]


def test_daily_run_writes_a_report_when_every_signal_is_down(tmp_path, monkeypatch):
    def down(*a, **k):
        raise OSError("network is down")
    for mod, fn in ((cli.sources, "x_new"), (cli.sources, "hf_new"), (cli.breakout, "breakouts"), (cli.sitemaps, "diff")):
        monkeypatch.setattr(mod, fn, down)
    out = tmp_path / "report.md"
    assert cli.main(["daily", "--config", str(_write_config(tmp_path)), "--state", str(tmp_path / "s"), "--out", str(out),
                     "--skip", "newsites,review"]) == 0
    text = out.read_text()
    assert text.count("network is down") >= 4 and out.with_suffix(".json").exists()


def test_daily_report_in_chinese(tmp_path, monkeypatch):
    _no_judge(monkeypatch)
    monkeypatch.setattr(cli.breakout, "breakouts", lambda cfg, state: {"searched": 120, "first_run": False, "terms": [], "repos": [
        {"repo": "Niko1221/Strata", "stars": 7734, "velocity": 859, "delta": 600, "age_days": 9.0, "description": "Qwen3.8", "new": True}]})
    out = tmp_path / "report.md"
    cli.main(["daily", "--config", str(_write_config(tmp_path, lang="zh")), "--state", str(tmp_path / "s"), "--out", str(out),
              "--skip", "x,hf,sitemaps,newsites,review"])
    text = out.read_text()
    assert "## 新词雷达" in text and "🆕 [Niko1221/Strata]" in text and "| +600 |" in text


# --- topics and mail -----------------------------------------------------------------------------

def test_topic_section_lists_matching_leads_repos_and_queries(cfg):
    cfg["topics"] = {"AI video": r"\bvideo"}
    lead = [{"term": "reelmimic", "examples": ["edenfunf/reelmimic: Show it a video you love"], "signals": ["② breakout"]},
            {"term": "strata", "examples": ["Niko1221/Strata: Qwen3.8 on a gaming PC"], "signals": ["② breakout"]}]
    blocks = {"breakout": {"repos": [{"repo": "a/motion-video-kit", "description": "video kit", "stars": 900, "velocity": 300},
                                     {"repo": "Niko1221/Strata", "description": "Qwen3.8", "stars": 7700, "velocity": 870}]}}
    found = {"queries": [{"query": "claude video skills", "impressions": 6, "page": "/best/x/"},
                         {"query": "best ai scraping tools", "impressions": 3, "page": "/best/web-scraping/"}]}
    text = "\n".join(report.topic_lines(cfg, lead, blocks, found))
    assert "reelmimic" in text and "motion-video-kit" in text and "claude video skills" in text
    assert "strata" not in text.lower() and "scraping" not in text


def test_every_text_has_both_languages():
    assert all(len(pair) == 2 and all(pair) for pair in i18n.T.values())


def test_mail_html_renders_tables():
    out = notify.html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table style=" in out and ">1</td>" in out
    # Gmail drops a <style> block outside <head>: every tag carries its own style.
    assert "<style>" not in out and '<td style="border:1px solid' in out


def test_mail_without_recipient_is_skipped(monkeypatch):
    monkeypatch.delenv("RADAR_EMAIL_TO", raising=False)
    assert notify.send("s", "x").startswith("mail skipped")


def test_mail_recipients_tolerate_how_people_type_them():
    assert notify.recipients('"a@b.com"') == ["a@b.com"]
    assert notify.recipients("a@b.com，c@d.cn") == ["a@b.com", "c@d.cn"]
    assert notify.recipients("<a@b.com>; c@d.cn\n") == ["a@b.com", "c@d.cn"]


# --- the generic terms behind the names ---------------------------------------------------------

from radar import usecases  # noqa: E402

TODAY = date(2026, 10, 12)


def _daily(values, end=TODAY - timedelta(days=1)):
    """A daily series ending yesterday, as Trends returns it; its last two days are unsettled."""
    start = end - timedelta(days=len(values) - 1)
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_usecases_reads_new_names_first_then_those_due_again():
    seen = {"old": {"first": "2026-10-08", "runs": 1}, "done": {"first": "2026-10-01", "runs": 2},
            "fresh": {"first": "2026-10-11", "runs": 1}}
    assert usecases.due(["a", "old", "b"], seen, TODAY, 5, 3) == ["a", "b", "old"]
    assert usecases.due(["a", "b", "c"], seen, TODAY, 2, 3) == ["a", "b"]


def test_usecases_parse_drops_the_name_itself_sentences_and_repeats():
    answer = json.dumps({"use_cases": [
        {"phrase": "AI Image Detector", "evidence": "check if any image is AI generated", "brand": False},
        {"phrase": "synthid detector online", "evidence": "x", "brand": True},
        {"phrase": "detector", "evidence": "x"},
        {"phrase": "check whether a picture was made by a model today", "evidence": "x"},
        {"phrase": "ai image detector", "evidence": "again"},
        {"phrase": "photoshop alternative", "evidence": "free photoshop clone", "brand": True}]})
    out = usecases.parse("Sure:\n" + answer, "synthid detector")
    assert [(u["phrase"], u["brand"]) for u in out] == [("ai image detector", False), ("photoshop alternative", True)]
    assert usecases.parse("no json here", "x") == []


def test_trends_last_two_days_are_not_a_rise():
    # "ai image detector" on 2026-10-12: flat for 88 days, then 0 and 100 on the two unsettled days
    s = usecases.shape(_daily([5] * 88 + [0, 100]), TODAY)
    assert s["now"] == "flat" and not s["rose"]


def test_a_single_day_spike_on_a_small_term_is_not_a_rise():
    s = usecases.shape(_daily([0] * 80 + [100] + [0] * 9), TODAY)   # "debloat macos"
    assert not s["rose"]


def test_a_real_rise_is_dated_and_still_rising():
    # "synthid detector": nothing, then 9, 53, 40 on the last settled days, then the unsettled two
    s = usecases.shape(_daily([1] * 85 + [9, 53, 40, 59, 100]), TODAY)
    assert s["rose"] and s["new"] and s["now"] == "rising" and s["started"] == "2026-10-07" and s["peak_day"] == "2026-10-08"


def test_a_rise_that_came_back_down_is_fading():
    s = usecases.shape(_daily([1] * 70 + [60, 100, 70, 30, 10] + [5] * 15), TODAY)
    assert s["rose"] and s["now"] == "fading"


def test_searches_a_day_follow_the_terms_own_volume_along_its_curve():
    # 30,000 searches in August at level 10; level 20 in the last settled week: twice as many a day
    points = _daily([10] * 81 + [20] * 9)
    assert usecases.own_per_day(points, 30000, "2026-08", TODAY) == 2000
    assert usecases.own_per_day(points, 0, "2026-08", TODAY) is None


def test_the_benchmark_is_sized_from_terms_whose_volume_is_known():
    def row(volume, term_level, bench_level):
        return {"volume": volume, "month": "2026-08", "_pair": [(d, term_level, bench_level) for d, _ in _daily([0] * 90)]}
    # a term with 3,000 searches a day at a third of the benchmark's level puts the benchmark at 9,000
    assert usecases.bench_daily([row(90000, 10, 30), row(900, 1, 50)]) == 9000   # the 900/month term is too small to count
    assert usecases.bench_daily([row(900, 1, 50)]) is None


def test_decisions():
    up, flat, down = {"now": "rising"}, {"now": "flat"}, {"now": "fading"}
    assert usecases.decide(up, 5000, 300) == "prioritize" and usecases.decide(up, 40, 300) == "watch"
    assert usecases.decide(flat, 5000, 300) == "watch" and usecases.decide(flat, 40, 300) == "skip"
    assert usecases.decide(down, 5000, 300) == "skip" and usecases.decide({"no_data": True}, None, 300) == "watch"


def _fake_usecase_network(monkeypatch):
    rise = _daily([1] * 85 + [9, 53, 40, 59, 100])
    flat = _daily([20] * 90)
    monkeypatch.setattr(usecases, "evidence", lambda names, repos, today, texts=None: (
        {n: {"posts": [{"text": "check if any image is AI generated", "likes": 900, "by": "a", "url": ""}], "readme": "", "repo": ""} for n in names}, 3000))
    monkeypatch.setattr(usecases, "propose", lambda model, name, ev: [
        {"phrase": "ai image detector", "evidence": "check if any image is AI generated", "brand": False},
        {"phrase": "photoshop alternative", "evidence": "free clone", "brand": True},
        {"phrase": "nobody searches this", "evidence": "x", "brand": False}])
    monkeypatch.setattr(usecases, "screen", lambda model, name, ev, cands: cands)
    monkeypatch.setattr(usecases, "volumes", lambda phrases, loc: (
        {"ai image detector": {"volume": 60000, "month": "2026-08"}, "photoshop alternative": {"volume": 6000, "month": "2026-08"},
         "nobody searches this": {"volume": 0, "month": ""}}, 90000))
    monkeypatch.setattr(usecases, "fetch_series", lambda keywords, bench, today: (
        {k: {"solo": rise if k == "synthid detector" else flat,
             "pair": [(d, v, 40) for d, v in (rise if k == "synthid detector" else flat)]} for k in keywords}, 7200))


def test_usecases_run_builds_one_table_and_reads_a_name_twice(cfg, tmp_path, monkeypatch):
    _fake_usecase_network(monkeypatch)
    uc = usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY)
    rows = {r["phrase"]: r for r in uc["rows"]}
    assert uc["proposed"] == 3 and uc["relevant"] == 3 and uc["kept"] == 2 and "nobody searches this" not in rows
    assert rows["synthid detector"]["kind"] == "name" and rows["synthid detector"]["decision"] == "prioritize"
    assert rows["ai image detector"]["per_day"] == 2000 and rows["ai image detector"]["decision"] == "watch"
    assert rows["photoshop alternative"]["brand"] and rows["photoshop alternative"]["decision"] == "skip"
    assert uc["bench_daily"] == 2200 and uc["cost_micros"] == 100200   # the median of 4,000 and 400
    assert usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY + timedelta(days=1))["names"] == []
    assert usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY + timedelta(days=3))["names"] == ["synthid detector"]
    assert usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY + timedelta(days=9))["names"] == []


def test_keyword_table_marks_brands_and_generic_terms_become_leads(cfg, tmp_path, monkeypatch):
    _fake_usecase_network(monkeypatch)
    uc = usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY)
    text = "\n".join(report.keyword_table(cfg, uc))
    assert "| **synthid detector** | name |" in text and "**prioritize**" in text
    assert "| **photoshop alternative** ⚠ | generic | synthid detector |" in text and "someone's brand" in text
    assert "“check if any image is AI generated”" in text
    leads = report.usecase_leads(uc)
    assert [x["term"] for x in leads] == ["ai image detector", "photoshop alternative"] and leads[0]["kinds"] == ["usecase"]


def test_review_ignores_the_unsettled_days(cfg, tmp_path, monkeypatch):
    conn = store.connect(tmp_path)
    store.record(conn, [_lead("ai image detector", ["usecase"])], date(2026, 9, 28))
    points = [{"date_from": d.isoformat(), "values": [v]} for d, v in _daily([5] * 43 + [0, 100])]
    answer = {"tasks": [{"result": [{"items": [{"data": points}]}]}]}
    monkeypatch.setattr(review.aisa, "call_all", lambda calls: ({c["call_id"]: answer for c in calls}, 1200))
    assert review.review(cfg, conn, TODAY)["checked"][0]["verdict"] == "miss"


def test_screen_drops_loosely_related_keywords_and_keeps_all_when_the_call_fails(monkeypatch):
    cands = [{"phrase": p, "brand": False} for p in ("ai image detector", "chatgpt alternative", "ai audio detector")]
    ev = {"posts": [{"text": "check if any image is AI generated", "likes": 900, "by": "a"}], "readme": ""}
    monkeypatch.setattr(usecases.aisa, "chat", lambda *a, **k: '[{"i": 0, "score": 0.95}, {"i": 1, "score": 0.2}, {"i": 2, "score": 0.7}]')
    kept = usecases.relevant(usecases.screen("m", "synthid detector", ev, cands))
    assert [(c["phrase"], c["relevance"]) for c in kept] == [("ai image detector", 0.95), ("ai audio detector", 0.7)]

    def down(*a, **k):
        raise usecases.aisa.AisaError("HTTP 500")
    monkeypatch.setattr(usecases.aisa, "chat", down)
    assert usecases.screen("m", "synthid detector", ev, cands) == cands


def test_momentum_is_the_last_settled_week_against_the_week_before():
    assert usecases.shape(_daily([10] * 76 + [20] * 7 + [40] * 7 + [0, 100]), TODAY)["momentum"] == 2.0
    assert usecases.shape(_daily([0] * 83 + [40] * 7 + [0, 100]), TODAY)["momentum"] is None   # nothing the week before


# --- the optional second source: what people searched together with a name ----------------------

def test_related_queries_split_into_long_tails_and_candidates():
    related = {"rising": ["ai photo detector", "google synthid detector", "openai", "gpt image 2.5"],
               "top": ["ai detector", "synthid detector", "synthid detector online", "ai photo detector"]}
    tails, cands = usecases.split_related("synthid detector", related)
    assert tails == ["google synthid detector", "synthid detector online"]
    assert [c["phrase"] for c in cands] == ["ai photo detector", "gpt image 2.5", "ai detector"]   # "openai" is one word
    assert cands[0]["source"] == "searched" and "rising" in cands[0]["evidence"]


def test_screen_marks_brands_and_other_names(monkeypatch):
    cands = [{"phrase": "free up mac disk space", "brand": False, "source": "model"},
             {"phrase": "gpt image 2.5", "brand": False, "source": "searched"}]
    monkeypatch.setattr(usecases.aisa, "chat", lambda *a, **k:
                        '[{"i": 0, "score": 0.8, "brand": true, "name": false}, {"i": 1, "score": 0.1, "brand": true, "name": true}]')
    viewed = usecases.screen("m", "removemacai", {"posts": [], "readme": "frees 12 GB"}, cands)
    assert viewed[0]["brand"] and viewed[1]["is_name"]
    assert [c["phrase"] for c in usecases.relevant(viewed)] == ["free up mac disk space"]


def test_with_a_key_related_queries_join_the_table_and_other_names_become_leads(cfg, tmp_path, monkeypatch):
    _fake_usecase_network(monkeypatch)
    monkeypatch.setenv("GOANYAPI_API_KEY", "k")
    monkeypatch.setattr(usecases.goanyapi, "related_queries", lambda name, geo: (
        {"rising": ["ai photo detector", "gpt image 2.5", "synthid detector online"], "top": []}, 6000))
    monkeypatch.setattr(usecases, "screen", lambda model, name, ev, cands: [
        dict(c, relevance=0.9, is_name=c["phrase"] == "gpt image 2.5") for c in cands])
    monkeypatch.setattr(usecases, "volumes", lambda phrases, loc: (
        {p: {"volume": 9000, "month": "2026-08"} for p in phrases if p != "nobody searches this"}, 90000))
    cfg["usecases"]["country_trends"] = False
    uc = usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY)
    rows = {r["phrase"]: r for r in uc["rows"]}
    assert rows["ai photo detector"]["source"] == "searched" and "gpt image 2.5" not in rows
    assert uc["cosearch"] == [{"term": "gpt image 2.5", "with": "synthid detector"}]
    assert uc["tails"] == {"synthid detector": ["synthid detector online"]} and uc["cost_micros"] == 106200
    text = "\n".join(report.keyword_table(cfg, uc))
    assert "| **ai photo detector** 🖼 🔎 |" in text and "gpt image 2.5 (with synthid detector)" in text and "synthid detector online" in text
    assert "incomplete today" not in text
    assert {"term": "gpt image 2.5", "kinds": ["cosearch"]}.items() <= report.usecase_leads(uc)[-1].items()


def test_without_a_key_the_table_says_the_source_is_off_and_a_failure_is_reported(cfg, tmp_path, monkeypatch):
    _fake_usecase_network(monkeypatch)
    uc = usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY)
    assert not uc["related"] and "GOANYAPI_API_KEY" in "\n".join(report.keyword_table(cfg, uc))
    monkeypatch.setenv("GOANYAPI_API_KEY", "k")

    def down(name, geo):
        raise usecases.goanyapi.GoAnyError("HTTP 502")
    monkeypatch.setattr(usecases.goanyapi, "related_queries", down)
    cfg["usecases"]["country_trends"] = False
    uc = usecases.run(cfg, tmp_path / "b", ["synthid detector"], {}, TODAY)
    assert uc["related_errors"] == ["synthid detector: HTTP 502"] and uc["kept"] == 2
    assert "HTTP 502" in "\n".join(report.keyword_table(cfg, uc))


def test_other_names_leave_out_old_products_siblings_and_spelling_variants():
    # what the first real run kept beside filmcraft and pdfcraft (2026-10-13)
    viewed = [{"phrase": p, "is_name": True, "rising": True, "established": old} for p, old in (
        ("premiere pro", True), ("davinci resolve", True), ("photocraft github", False), ("gpt image 2.5", False),
        ("adobe artcraft", False), ("artcraft adobe", False))]
    assert [x["term"] for x in usecases.other_names("filmcraft", viewed, ["photocraft", "filmcraft"])] == ["gpt image 2.5", "adobe artcraft"]
    tails, cands = usecases.split_related("pdfcraft", {"rising": ["pdf craft", "pdf-craft review", "merge pdf files"], "top": []})
    assert tails == ["pdf craft", "pdf-craft review"] and [c["phrase"] for c in cands] == ["merge pdf files"]


def test_every_name_gets_the_country_series_and_the_cap_is_on_generic_terms(cfg, monkeypatch):
    rows = [{"phrase": f"name{i}", "kind": "name"} for i in range(5)] + \
           [{"phrase": f"g{v}", "kind": "generic", "volume": v} for v in (27100, 22200, 8100, 6600, 900)]
    cfg["usecases"]["country_max"] = 3
    asked = []
    monkeypatch.setattr(usecases.goanyapi, "daily_series", lambda term, geo: (asked.append(term) or [(TODAY, 1)], 6000))
    out, cost, errors = usecases.country_series(rows, cfg["usecases"])
    assert sorted(asked) == sorted([f"name{i}" for i in range(5)] + ["g27100", "g22200", "g8100"]) and cost == 48000 and not errors


def test_other_names_must_be_rising_and_not_the_same_family():
    viewed = [{"phrase": p, "is_name": True, "rising": r} for p, r in (
        ("gpt image 2.5", True), ("google nano banana", True), ("nano banana pro", True), ("google gemini", False), ("mistral large 4", True))]
    assert [x["term"] for x in usecases.other_names("nano banana 2.1", viewed)] == ["gpt image 2.5", "mistral large 4"]


# --- one country's settled series for the rows that matter -------------------------------------

def _month(values):
    """A 30-day series ending today, as goanyapi.daily_series returns it."""
    return [(TODAY - timedelta(days=len(values) - 1 - i), v) for i, v in enumerate(values)]


def test_an_established_term_80_percent_up_for_five_days_is_a_rise_in_the_country_series():
    # "photoshop alternative" in the US around PhotoCraft: about 50, then 77, 86, 83, 100, 94, and 79 on the partial day
    points = _month([52, 48, 50, 47, 55, 51, 49, 53, 50, 46, 52, 50, 48, 54, 49, 51, 50, 47, 53, 46, 47, 53, 57, 55, 77, 86, 83, 100, 94, 79])
    s = usecases.shape(points, TODAY, usecases.COUNTRY)
    assert s["rose"] and not s["new"] and s["now"] == "rising" and s["started"] == "2026-10-07" and s["momentum"] >= 1.4


def test_a_flat_country_series_stays_flat():
    assert usecases.shape(_month([30, 34, 28, 31] * 7 + [33, 29]), TODAY, usecases.COUNTRY)["now"] == "flat"


def test_names_and_big_generic_terms_take_their_shape_from_the_country(cfg, tmp_path, monkeypatch):
    _fake_usecase_network(monkeypatch)
    monkeypatch.setenv("GOANYAPI_API_KEY", "k")
    cfg["usecases"]["related"] = False
    up = _month([50] * 24 + [77, 86, 83, 100, 94, 79])
    asked = []

    def daily_series(term, geo):
        asked.append(term)
        if term == "synthid detector":
            raise usecases.goanyapi.GoAnyError("HTTP 502")
        return up, 6000
    monkeypatch.setattr(usecases.goanyapi, "daily_series", daily_series)
    uc = usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY)
    rows = {r["phrase"]: r for r in uc["rows"]}
    # The lookups run three at a time, so the order they are recorded in is the threads' own.
    assert sorted(asked) == ["ai image detector", "photoshop alternative", "synthid detector"]
    assert rows["ai image detector"]["trend_geo"] and rows["ai image detector"]["now"] == "rising"
    assert rows["ai image detector"]["decision"] == "prioritize"      # flat worldwide, up in the US
    assert "trend_geo" not in rows["synthid detector"] and uc["country_errors"] == ["synthid detector: HTTP 502"]
    text = "\n".join(report.keyword_table(cfg, uc))
    assert "still rising (US)" in text and "worldwide data used instead: synthid detector" in text
    assert "This table is incomplete today" in text


def test_small_generic_terms_are_not_worth_a_country_lookup(cfg):
    rows = [{"phrase": "a", "kind": "generic", "volume": 300}, {"phrase": "b", "kind": "generic", "volume": 5000}, {"phrase": "n", "kind": "name"}]
    asked = []
    import radar.goanyapi as g
    orig = g.daily_series
    g.daily_series = lambda term, geo: (asked.append(term) or [], 0)
    try:
        usecases.country_series(rows, cfg["usecases"])
    finally:
        g.daily_series = orig
    assert asked == ["n", "b"]


def test_started_rising_is_the_run_that_holds_the_peak_not_an_earlier_blip():
    # a one-day blip on 10-01, then the real rise from 10-07
    points = _month([20] * 19 + [45] + [20] * 5 + [70, 100, 68, 51, 42])
    s = usecases.shape(points, TODAY, usecases.COUNTRY)
    assert s["rose"] and s["started"] == "2026-10-08" and s["peak_day"] == "2026-10-09"


def test_only_things_are_read_for_generic_terms_not_words_that_surged():
    today = [{"term": "mcd", "kinds": ["github"], "named": 0.75}, {"term": "kr.co.klisten.api", "kinds": ["sitemap"], "named": 0.78},
             {"term": "photocraft", "kinds": ["breakout"], "named": 0.95}, {"term": "dots", "kinds": ["breakout"], "named": 0.65},
             {"term": "hailuo", "kinds": ["newsite", "sitemap"]}]
    recent = [{"term": "qwen image 2.1", "kinds": ["hf"], "named": 0.96}, {"term": "use rea", "kinds": ["x"], "named": 0.72},
              {"term": "photocraft", "kinds": ["x"], "named": 0.9}]
    assert usecases.names_to_read(today, recent) == ["photocraft", "hailuo", "qwen image 2.1"]


def test_a_failed_post_search_costs_one_name_its_posts_not_the_step(monkeypatch):
    def call(calls):
        if "photocraft" in calls[0]["arguments"]["query"]:
            raise usecases.aisa.AisaError("HTTP 504: <html><h1>504 Gateway Time-out</h1></html>")
        return {"s": {"tweets": [{"text": "check any image", "likeCount": 900, "author": {"userName": "a"}}]}}, 3000
    monkeypatch.setattr(usecases.aisa, "call", call)
    monkeypatch.setattr(usecases, "_readme", lambda repo: "PhotoCraft opens real PSD files")
    ev, cost = usecases.evidence(["photocraft", "synthid detector"], {"photocraft": "storytold/photocraft"}, TODAY)
    assert cost == 3000 and ev["photocraft"]["readme"] and ev["photocraft"]["posts"] == []
    assert ev["photocraft"]["error"] == "HTTP 504: 504 Gateway Time-out" and len(ev["synthid detector"]["posts"]) == 1


# --- names someone asks about: the `keywords` command and the MCP tool ---------------------------

def test_keywords_on_request_reads_every_name_now_and_leaves_the_record_alone(cfg, tmp_path, monkeypatch):
    _fake_usecase_network(monkeypatch)
    uc, text = cli.keyword_table(cfg, tmp_path, ["SynthID Detector ", "synthid detector", ""], {})
    assert uc["names"] == ["synthid detector"] and "| **ai image detector** 🖼 |" in text
    again, _ = cli.keyword_table(cfg, tmp_path, ["synthid detector"], {})
    assert again["names"] == ["synthid detector"]                 # asked again, read again
    assert not (tmp_path / "usecases.json.gz").exists()           # the daily run's read-twice record is untouched
    many, _ = cli.keyword_table(cfg, tmp_path, [f"name {i}" for i in range(20)], {})
    assert len(many["names"]) == usecases.ASKED_MAX


def test_leads_command_lists_what_was_kept(tmp_path, capsys):
    conn = store.connect(tmp_path)
    store.record(conn, [_lead("strata", ["breakout"], named=0.9)], date.today() - timedelta(days=2))
    store.record(conn, [_lead("old one", ["x"])], date.today() - timedelta(days=30))
    data = cli.kept_leads(tmp_path, 7)
    assert [r["term"] for r in data["leads"]] == ["strata"] and data["hit_rates"]["pending"] == 2
    assert cli.main(["leads", "--state", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "strata  [breakout]" in out and "1 leads in the last 7 days" in out


# --- the MCP server -----------------------------------------------------------------------------

def test_mcp_tools_answer_and_never_print(cfg, tmp_path, monkeypatch, capsys):
    import pytest
    pytest.importorskip("mcp")
    from radar import mcp_server
    (tmp_path / "config.json").write_text(json.dumps({"x_accounts": {"labs": ["a"]}}))
    monkeypatch.setenv("RADAR_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("RADAR_STATE", str(tmp_path / "state"))
    store.record(store.connect(tmp_path / "state"), [_lead("photocraft", ["breakout"], named=0.95)], date.today())
    assert [r["term"] for r in mcp_server.recent_leads(7)["leads"]] == ["photocraft"]

    _fake_usecase_network(monkeypatch)
    out = mcp_server._keyword_table(["synthid detector"], {})
    assert "| **ai image detector** 🖼 |" in out["table_markdown"] and out["incomplete"] is False and out["cost_usd"] == 0.1

    monkeypatch.setattr(cli.sources, "x_new", lambda cfg, state: {"posts": 400, "cost_micros": 3000, "terms": [
        {"term": "qwen3.9", "recent": 2, "note": "X: 2 accounts", "examples": ["@a: Qwen3.9 is out"]}]})
    monkeypatch.setattr(cli.sources, "hf_new", lambda cfg, state: {"models": 100, "terms": []})
    monkeypatch.setattr(report.judge, "score", lambda rows, model: {"judged": 0})
    ran = mcp_server._run("sources", [])
    assert ran["wrote_report"] and "**qwen3.9**" in ran["report_markdown"]
    assert capsys.readouterr().out == ""        # stdout is the protocol channel


def test_mcp_tools_say_why_they_failed(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip("mcp")
    from radar import aisa, mcp_server
    # mcp 2.x shows the agent only "Error executing tool keyword_table" for any exception but a ToolError
    monkeypatch.setenv("RADAR_STATE", str(tmp_path / "state"))

    def no_key(*args):
        raise aisa.AisaError("no AIsa key: set AISA_API_KEY, or log in with the AIsa CLI (`aisa login`)")
    monkeypatch.setattr(cli, "keyword_table", no_key)
    with pytest.raises(mcp_server.ToolError, match="no AIsa key"):
        mcp_server._keyword_table(["photocraft"], {})

    def read_only(state, days):                 # RADAR_STATE unset, and a client that starts the server in "/"
        raise OSError(30, "Read-only file system", "state")
    monkeypatch.setattr(cli, "kept_leads", read_only)
    with pytest.raises(mcp_server.ToolError, match="Read-only file system.*RADAR_STATE sets the folder"):
        mcp_server.recent_leads(7)


def test_skills_have_a_name_and_a_description():
    import radar
    root = Path(__file__).resolve().parents[1] / "skills"
    for name in ("new-term-radar", "ai-listening", "seo-new-keywords-research"):
        text = (root / name / "SKILL.md").read_text()
        head = re.match(r"---\nname: (\S+)\ndescription: (.+)\n---\n", text)
        assert head and head.group(1) == name and 100 < len(head.group(2)) < 1024
        assert "AISA_API_KEY" in text
        # What buildwithclaude asked of a listed skill (2026-10-23): run the pinned release, not a clone
        # of the default branch; say the cost and wait for a yes before a paid run; treat what the
        # radar reads as data that cannot change the workflow.
        assert f"uvx new-term-radar=={radar.__version__}" in text and "git clone" not in text
        assert "python -m radar" not in text and "a yes" in text and "never to follow" in text
        # And (2026-10-28): nothing goes out on the user's behalf without their yes to that one.
        assert "## Nothing goes out without a yes" in text and "is not that yes" in text
        # Claude Code replaces $0, $1 ... in a skill's text with the user's arguments: "$0.05" came out
        # as "<what the user typed>.05" on the first real run. Amounts are written "0.05 USD".
        assert not re.search(r"\$\d", text)
    assert "wait for their yes to that one" in (root.parent / "radar/mcp_server.py").read_text()


def test_the_aisa_key_comes_from_the_environment_or_the_cli_login(tmp_path, monkeypatch):
    from radar import aisa
    monkeypatch.setattr(aisa.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AISA_API_KEY", raising=False)
    import pytest
    with pytest.raises(aisa.AisaError, match="aisa login"):
        aisa.key()
    (tmp_path / ".aisa").mkdir()
    (tmp_path / ".aisa" / "key").write_text("from-cli\n")
    assert aisa.key() == "from-cli"
    monkeypatch.setenv("AISA_API_KEY", "from-env")
    assert aisa.key() == "from-env"


def test_every_manifest_carries_the_same_version():
    # A release is one number in five places; the registry rejects a server.json whose
    # version PyPI does not have.
    import radar
    root = Path(__file__).resolve().parents[1]
    server = json.loads((root / "server.json").read_text())
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    copilot = json.loads((root / "plugin.json").read_text())        # GitHub Copilot reads the root manifest
    pyproject = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M).group(1)
    assert {pyproject, server["version"], server["packages"][0]["version"], plugin["version"],
            market["metadata"]["version"], copilot["version"]} == {radar.__version__}
    pinned = f"new-term-radar=={radar.__version__}"                   # the plugin and the skills run this release
    assert pinned in plugin["mcpServers"]["new-term-radar"]["args"]
    assert all(pinned in (root / s / "SKILL.md").read_text() for s in copilot["skills"])
    assert len(server["description"]) <= 100                                  # the registry's limit
    assert f"mcp-name: {server['name']} -->" in (root / "README.md").read_text()   # how the registry checks who owns the PyPI package


def test_without_a_config_file_the_radar_runs_on_the_example_it_ships_with(tmp_path, monkeypatch, capsys):
    # An installed radar (uvx, pip) has no clone to copy config.example.json from.
    import pytest
    root = Path(__file__).resolve().parents[1]
    assert (root / "radar" / "config.example.json").read_text() == (root / "config.example.json").read_text()
    monkeypatch.chdir(tmp_path)
    cfg = config.load()
    assert cfg["x_accounts"]["labs"] and cfg["_dir"].resolve() == tmp_path.resolve()
    assert "example list" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="not found"):
        config.load(tmp_path / "mine.json")                          # a path you name must exist
    (tmp_path / "config.json").write_text(json.dumps({"x_accounts": {"labs": ["a"]}}))
    assert config.load()["x_accounts"]["labs"] == ["a"]


def test_the_mcp_command_says_what_is_missing(monkeypatch, capsys):
    import builtins
    real = builtins.__import__

    def without_mcp(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    import sys
    import radar
    monkeypatch.delitem(sys.modules, "radar.mcp_server", raising=False)   # an earlier test may have loaded it;
    monkeypatch.delattr(radar, "mcp_server", raising=False)               # the package keeps its own reference
    monkeypatch.setattr(builtins, "__import__", without_mcp)
    assert cli.main(["mcp"]) == 1
    assert "new-term-radar[mcp]" in capsys.readouterr().err


# --- page one: can a new site get in? ------------------------------------------------------------

from radar import competition  # noqa: E402


def _serp(results, ads=False):
    items = [{"type": "ai_overview"}] + [{"type": "organic", "domain": d, "url": u, "title": t, "rank_group": i + 1}
                                         for i, (d, u, t) in enumerate(results)]
    return {"tasks": [{"result": [{"items": items + ([{"type": "paid"}] if ads else [])}]}]}


def test_page_one_verdicts():
    assert competition.verdict(3, None) == "open" and competition.verdict(0, 15) == "open"
    assert competition.verdict(0, 55) == "crowded" and competition.verdict(None, None) == "crowded"
    assert competition.verdict(1, 30) == "mixed" and competition.verdict(None, 30) == "mixed"
    assert competition.page_kind("youtube.com", "https://youtube.com/watch?v=1", "x") == "video"
    assert competition.page_kind("headshotpro.com", "https://headshotpro.com/", "Free AI Headshot Generator") == "tool"
    assert competition.page_kind("headshots.com", "https://headshots.com/best", "16 Best AI Headshot Generators") == "list"


def test_page_one_sites_are_dated_by_registration_and_compared_across_readings(tmp_path, monkeypatch):
    from radar import page_age
    born = {"tool-a.example": "2026-08-01", "old.example": "2015-03-03", "example.co.uk": "2026-07-01"}
    monkeypatch.setattr(page_age, "ask", lambda d: (born[d], "ok") if d in born else ("", "none"))
    first = {"results": [{"domain": "tool-a.example", "url": "https://tool-a.example/", "position": 2},
                         {"domain": "old.example", "url": "https://old.example/x", "position": 1},
                         {"domain": "docs.example.co.uk", "url": "https://docs.example.co.uk/a", "position": 5},
                         {"domain": "nodate.example", "url": "https://nodate.example/", "position": 9}]}
    rows = [{"phrase": "new term", "decision": "prioritize"}, {"phrase": "unread", "decision": "watch"}]
    page_age.record(rows, {"new term": first}, tmp_path, date(2026, 10, 20))
    got = rows[0]["page_age"]
    assert (got["dated"], got["young"], got["newest"]) == (3, 2, "2026-08-01") and "page_age" not in rows[1]
    assert [s["domain"] for s in got["young_sites"]] == ["tool-a.example", "docs.example.co.uk"]
    assert got["entered"] == {"since": "", "urls": []} and rows[0]["decision"] == "prioritize"   # recorded, not used
    second = {"results": first["results"][:2] + [{"domain": "fresh.example", "url": "https://fresh.example/", "position": 4}]}
    page_age.record(rows, {"new term": second}, tmp_path, date(2026, 10, 23))
    assert rows[0]["page_age"]["entered"] == {"since": "2026-10-20", "urls": ["https://fresh.example/"]}
    late = rows[:1]
    page_age.record(late, {"new term": second}, tmp_path, date(2026, 11, 20))              # over a week after: not compared
    assert late[0]["page_age"]["entered"] == {"since": "", "urls": []}


def test_rdap_is_asked_once_more_and_a_registry_without_it_says_none(monkeypatch):
    from radar import page_age
    import io
    calls, answers = [], []

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        calls.append(req.full_url)
        got = answers.pop(0)
        if isinstance(got, int):
            raise page_age.urllib.error.HTTPError(req.full_url, got, "no", {}, None)
        return Reply(json.dumps(got).encode())
    monkeypatch.setattr(page_age.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(page_age, "RDAP_GAP", 0)
    monkeypatch.setattr(page_age, "RETRY_PAUSE", 0)
    answers[:] = [429, {"events": [{"eventAction": "registration", "eventDate": "2026-08-03T00:00:00Z"}]}]
    assert REAL_ASK("codonic.example") == ("2026-08-03", "ok") and len(calls) == 2
    answers[:] = [404]
    assert REAL_ASK("tool.example") == ("", "none")
    answers[:] = [503, 503]
    assert REAL_ASK("busy.example") == ("", "failed")


def test_a_crowded_rising_term_is_watched_not_prioritized(cfg, tmp_path, monkeypatch):
    cfg["competition"]["max_rows"] = 8
    monkeypatch.setenv("GOANYAPI_API_KEY", "k")
    strong = [("canva.com", "https://canva.com/ai", "Canva AI"), ("adobe.com", "https://adobe.com/x", "Adobe")]
    weak = [("tiny1.ai", "https://tiny1.ai/", "Free Tool"), ("tiny2.ai", "https://tiny2.ai/", "Free Tool"),
            ("tiny3.ai", "https://tiny3.ai/x", "A guide")]
    pages = {"crowded term": _serp(strong, ads=True), "open term": _serp(strong + weak)}

    def call(calls):
        c = calls[0]
        if c["call_id"] == "serp":
            return {"serp": pages[c["arguments"]["body"][0]["keyword"]]}, 2000
        items = [{"keyword": k, "keyword_properties": {"keyword_difficulty": 60 if k == "crowded term" else 25}}
                 for k in c["arguments"]["body"][0]["keywords"]]
        return {"kd": {"tasks": [{"result": [{"items": items}]}]}}, 12000
    monkeypatch.setattr(competition.aisa, "call", call)
    rated = {"canva.com": 94, "adobe.com": 96, "tiny1.ai": 8, "tiny2.ai": 12, "tiny3.ai": 21}
    asked = []

    def goany(path, **p):
        if path == "dr":
            asked.append(p["domain"])
            return {"domain_rating": rated[p["domain"]]}, 2000
        if path == "google-intitle":
            return {"intitleCount": 19500}, 4000
        return {"domains": ["paid.example"] if p["keyword"] == "crowded term" else [], "advertisers": []}, 4000
    monkeypatch.setattr(competition.goanyapi, "call", goany)
    rows = [{"phrase": "crowded term", "decision": "prioritize"}, {"phrase": "open term", "decision": "prioritize"},
            {"phrase": "faded", "decision": "skip"}]
    cost, errors = competition.assess(rows, cfg, tmp_path)
    crowded, opened, faded = rows
    assert crowded["decision"] == "watch" and crowded["demoted"] and crowded["competition"]["ads"]
    assert crowded["competition"]["verdict"] == "crowded" and crowded["competition"]["weak"] == 0
    assert opened["decision"] == "prioritize" and opened["competition"]["weak"] == 3
    assert opened["competition"]["weakest"] == {"domain": "tiny1.ai", "dr": 8, "position": 3, "url": "https://tiny1.ai/"}
    assert "competition" not in faded and errors == [] and cost == 2 * 2000 + 12000 + 5 * 2000 + 2 * (4000 + 4000)
    assert crowded["competition"]["built"] == 19500 and crowded["competition"]["paid"] == ["paid.example"]
    competition.assess([{"phrase": "open term", "decision": "watch"}], cfg, tmp_path)
    assert len(asked) == 5                                    # ratings are kept 30 days, not bought again
    uc = {"names": ["x"], "rows": [dict(r, kind="generic", names=["x"], brand=False, new=True, now="rising", peak_day="2026-10-10",
                                       evidence="") for r in rows[:2]],
          "proposed": 2, "relevant": 2, "kept": 2, "benchmark": "gpts", "volume_month": "2026-08", "related": True}
    text = "\n".join(report.keyword_table(cfg, uc))
    assert "crowded · 0 weak · KD 60 · ads · 19,500 pages built · 1 advertisers" in text and "watch ↓" in text
    assert "open · 3 weak · KD 25 · 19,500 pages built · AI overview |" in text and "advertising on it: paid.example" in text
    assert "tiny1.ai (rated 8) at #3" in text and "Page one: open = three or more" in text
    assert "**crowded term**: no easy slot, even the weakest, canva.com, is rated 94 (#1)" in text



# --- ⑥ revenue: products people pay for ----------------------------------------------------------

from radar import revenue  # noqa: E402


def _product(slug, name, usd, growth=None, website="https://x.example", stealth=False, about="Turns photos into headshots."):
    return {"slug": slug, "name": name, "website": website, "description": about, "category": "ai",
            "revenue": {"last30Days": usd * 100}, "growth30d": growth, "stealthMode": stealth}


def test_revenue_reports_growth_then_a_product_that_left_zero(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TRUSTMRR_API_KEY", "k")
    monkeypatch.setattr(revenue.time, "sleep", lambda s: None)
    pages = {"growth-desc": [_product("headshot", "HeadshotKit", 4200, 4500.0, "https://headshotkit.ai"),
                             _product("anon", "Anonymous startup", 9000, 2.0, website="", stealth=True),
                             _product("gpt", "Talent Tech Global Limited", 840, 1.6, "https://gptproto.com")],
             "listed-desc": [_product("baby", "AI Baby Generator", 0, None, "https://aibabygenerator.ai")]}
    monkeypatch.setattr(revenue, "fetch", lambda sort, page, c: pages[sort] if page == 1 else [])
    first = revenue.products(cfg, tmp_path)
    assert first["first_run"] and [r["term"] for r in first["products"]] == ["headshotkit", "gptproto"]
    assert first["products"][0]["growth_text"] == "up from near $0" and first["products"][1]["growth_text"] == "+160%"
    assert first["terms"][0]["about"] == ("https://headshotkit.ai", "Turns photos into headshots.")
    pages["growth-desc"] = []
    pages["listed-desc"] = [_product("baby", "AI Baby Generator", 900, None, "https://aibabygenerator.ai")]
    later = revenue.products(cfg, tmp_path)
    assert [r["term"] for r in later["products"]] == ["ai baby generator"] and later["products"][0]["from_zero"]
    assert later["terms"][0]["note"] == "TrustMRR: from $0 to $900 in 30 days"
    text = "\n".join(report._revenue_lines(later, cfg))
    assert "**$0 last time, earning now**" in text and "forbid republishing" in text


def test_revenue_without_a_key_says_it_is_off(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("TRUSTMRR_API_KEY", raising=False)
    rv = revenue.products(cfg, tmp_path)
    assert rv == {"skipped": True, "no_key": True}
    assert "No TRUSTMRR_API_KEY" in "\n".join(report._revenue_lines(rv, cfg))



# --- what a lead makes, and one-click chances ----------------------------------------------------

from radar import opportunity  # noqa: E402


def _posts(*texts_likes):
    return [{"text": x, "likes": n} for x, n in texts_likes]


def test_format_reads_the_name_first_then_what_most_posts_say():
    assert opportunity.fmt({"term": "veo 3.1", "posts": []}) == "video"
    assert opportunity.fmt({"term": "elevenlabs v4", "posts": _posts(
        ("give your characters a voice", 1500), ("tts that whispers", 500), ("audio and sound effects", 400),
        ("made a video with it", 300))}) == "voice"                           # voice 3, video 1
    assert opportunity.fmt({"term": "claude opus 5.5", "posts": _posts(
        ("it made an animation", 100000), ("great at code", 6000), ("writes well", 6000), ("fast", 3000),
        ("cheap", 3000), ("smart", 1000), ("solid", 900), ("ok", 800))}) == "text"   # one post in eight is not a format
    assert opportunity.fmt({"term": "headshotkit", "about": ("https://headshotkit.ai", "AI headshots from selfies")}) == "image"


def test_one_click_needs_a_tutorial_people_shared():
    assert opportunity.one_click(_posts(("here's how to build this with Opus + Blender + Unreal", 1699))) == "tutorial"
    assert opportunity.one_click(_posts(("here's how I did it", 120))) is None              # nobody shared it
    assert opportunity.one_click(_posts(("8 denoising steps, open weights", 5301))) is None   # an announcement
    assert opportunity.one_click(_posts(("everyone is making these figurines", 4200))) == "trend"


def test_leads_carry_format_marks_and_the_legend(cfg, monkeypatch):
    monkeypatch.setattr(report.judge, "score", lambda rows, model: {"judged": len(rows)})
    blocks = {"x": {"terms": [{"term": "clipforge", "recent": 2, "note": "X", "examples": ["@a: clipforge animates any photo"],
                               "posts": [{"text": "here's how: step 1 upload, step 2 prompt", "likes": 2400}]}]}}
    lead, _ = report.leads(cfg, blocks)
    assert lead[0]["format"] == "video" and lead[0]["one_click"] == "tutorial"
    assert report._marks(lead[0]) == " 🎬 ⚡"



# --- ⑦ asks, pains and tutorial comments ---------------------------------------------------------

from radar import asks, pains  # noqa: E402


def _reddit_post(pid, title, score=12, sub="VideoEditors"):
    return {"id": pid, "title": title, "text": "", "score": score, "comments": 3, "subreddit": sub,
            "url": f"https://www.reddit.com/r/{sub}/{pid}", "created": 0}


def test_asks_turns_reddit_requests_into_sized_jobs(cfg, tmp_path, monkeypatch):
    cfg["asks"]["patterns"] = ['"is there an ai tool"', '"how do i automate"']
    posts = {'"is there an ai tool"': [_reddit_post("a", "Is there an AI tool to add b-roll automatically?", 45),
                                       _reddit_post("b", "Any AI that turns slides into notes?", 35, "college"),
                                       _reddit_post("c", "Rate my setup", 1)],
             '"how do i automate"': [_reddit_post("a", "Is there an AI tool to add b-roll automatically?", 45)]}
    monkeypatch.setattr(asks.social, "reddit", lambda q, **k: (posts[q], 1880))
    monkeypatch.setattr(asks.aisa, "chat", lambda model, system, user: json.dumps(
        [{"i": i, "kind": "request", "job": job, "ai": True} for i, job in enumerate(
            ["add b roll automatically", "convert slides to notes"])]))
    monkeypatch.setattr(asks.usecases, "volumes", lambda phrases, loc: (
        {"add b roll automatically": {"volume": 880}, "convert slides to notes": {"volume": 10}}, 90000))
    first = asks.asks(cfg, tmp_path)
    assert first["read"] == 2 and first["asks"] == 2 and first["unsized"] == 1          # "rate my setup" scored under the floor
    assert [r["phrase"] for r in first["rows"]] == ["add b roll automatically"] and first["cost_micros"] == 2 * 1880 + 90000
    assert first["terms"][0]["note"] == "Reddit: asked 1× (r/VideoEditors)"
    assert [q["phrase"] for q in first["quiet"]] == ["convert slides to notes"]           # asked, too few searches yet
    text = "\n".join(report._asks_lines(first, cfg))
    assert "| **add b roll automatically** | 1 (45) | r/VideoEditors | 880 |" in text and "do not republish" in text
    assert "| convert slides to notes | 1 (35) | r/college |" in text and "ideas rather than keywords" in text
    again = asks.asks(cfg, tmp_path)
    assert again["rows"][0]["again"] and again["terms"] == []                            # reported once


def test_pains_groups_reviews_posts_and_comments_into_gaps(cfg, monkeypatch):
    monkeypatch.setenv("GOANYAPI_API_KEY", "k")
    ext = "a" * 32

    def serp(q, gl="US"):
        if q.startswith("site:chromewebstore"):
            return [{"url": f"https://chromewebstore.google.com/detail/headshot-ai/{ext}", "title": "x", "rank": 1}], 2000
        return [{"url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "tutorial", "rank": 1}], 2000
    monkeypatch.setattr(pains.goanyapi, "serp", serp)
    monkeypatch.setattr(pains.goanyapi, "chrome_reviews", lambda e, c="": (
        [{"rating": 1, "text": "Needs a photo of your face, no way to upload a group shot", "date": "2026-10-13"},
         {"rating": 5, "text": "Great!", "date": "2026-10-12"}], "", 3000))
    monkeypatch.setattr(pains.social, "reddit", lambda q, **k: ([_reddit_post("r1", "Headshot apps all look plastic")], 1880))
    monkeypatch.setattr(pains.social, "youtube_comments", lambda vid: ([{"text": "Is there a way to do this for a whole team at once?", "likes": 40},
                                                                        {"text": "nice", "likes": 0}], 1000))
    monkeypatch.setattr(pains.aisa, "chat", lambda model, system, user, max_tokens=2500: json.dumps([
        {"gap": "No team or group headshots", "kind": "missing", "items": [0, 2], "phrase": "ai headshots for teams"},
        {"gap": "Results look plastic", "kind": "quality", "items": [1]}]))
    result = pains.run(cfg, "ai headshot")
    assert result["by_source"] == {"chrome": 1, "reddit": 1, "youtube": 1, "appstore": 0}   # 5 stars, "nice" and repeats are left out
    assert result["gaps"][0]["count"] == 2 and result["gaps"][0]["sources"] == ["chrome", "youtube"]
    text = report.pains(result)
    assert "| No team or group headshots | missing | 2 | ai headshots for teams |" in text and "Trustpilot" in text


def test_tutorial_asks_only_for_leads_marked_one_click(cfg, monkeypatch):
    cfg["tutorials"]["max_leads"] = 3
    monkeypatch.setenv("GOANYAPI_API_KEY", "k")
    monkeypatch.setattr(pains.goanyapi, "serp", lambda q, gl="US": ([{"url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "t", "rank": 1}], 2000))
    monkeypatch.setattr(pains.social, "youtube_comments", lambda vid: ([{"text": "Can someone make this one click? Too many steps", "likes": 12}], 1000))
    monkeypatch.setattr(pains.aisa, "chat", lambda model, system, user, max_tokens=2500: json.dumps(
        [{"gap": "Too many manual steps", "kind": "wants a tool", "items": [0], "phrase": "one click blender export"}]))
    out = pains.tutorial_asks(cfg, [{"term": "opus blender game", "one_click": "tutorial"}, {"term": "plain", "one_click": None}])
    assert [x["term"] for x in out["leads"]] == ["opus blender game"] and out["cost_micros"] == 2000 + 1000
    text = "\n".join(report._tutorial_lines(out))
    assert "Too many manual steps (wants a tool, 1) → one click blender export" in text


# --- ⑧ sellers: sites that just put up a payment link ---------------------------------------------

from radar import sellers  # noqa: E402


def _backlinks(rows):
    def where(d):          # a forum thread is not the forum selling anything
        return f"https://{d}/t/freelancer-invoice-inbox/185482" if d.startswith("community.") else f"https://{d}/pricing"
    return {"b": {"tasks": [{"result": [{"items": [{"domain_from": d, "url_to": u, "url_from": where(d),
                                                     "first_seen": "2026-10-14 03:00:00 +00:00", "domain_from_rank": 10}
                                                    for d, u in rows]}]}]}}


def _sites(names):
    return {"d": {"tasks": [{"result": [{"items": [{"domain": d, "first_seen": "2026-10-13 21:00:00 +00:00"} for d in names]}]}]}}


def test_sellers_keep_payment_links_and_leave_out_the_rest(cfg, tmp_path, monkeypatch):
    cfg["sellers"]["platforms"] = ["stripe", "lemonsqueezy", "gumroad", "creem"]
    answers = {
        "buy.stripe.com": [("pestora.framer.website", "https://buy.stripe.com/aFa28rbpG9"), ("github.com", "https://buy.stripe.com/x"),
                           ("permit-records-demo.com", "https://buy.stripe.com/old"), ("try.demotools.dev", "https://buy.stripe.com/t")],
        "lemonsqueezy.com": [("meeting-demo.app", "https://merit.lemonsqueezy.com/checkout/buy/c0b375"), ("seo.shop", "https://lemonsqueezy.com/"),
                             ("x.app", "https://www.lemonsqueezy.com/checkout")],
        "gumroad.com": [("janemaker.com", "https://janemaker.gumroad.com/l/toxins-in-textiles"), ("spam-network-demo.com", "https://spamseller.gumroad.com/"),
                        ("community.n8n.io", "https://x.gumroad.com/l/invoice-flow")],
        "creem.io": [("launch-demo.app", "https://www.creem.io/payment/prod_59Tt3"), ("docs.example", "https://docs.creem.io/api-reference")],
    }
    # the sites whose first link is new; permit-records-demo.com has sold for years, demotools.dev is named by its parent domain
    new = {target: [d for d, _ in rows if d not in ("permit-records-demo.com", "try.demotools.dev")] for target, rows in answers.items()}
    new["buy.stripe.com"].append("demotools.dev")

    def call(calls):
        target = calls[0]["arguments"]["body"][0]["target"]
        return (_sites(new[target]) if calls[0]["tool"] == sellers.DOMAINS else _backlinks(answers[target])), 27600
    monkeypatch.setattr(sellers.aisa, "call", call)
    monkeypatch.setattr(sellers, "page_words", lambda host: ("Pestora — pest control software", "Book and bill pest jobs") if host.startswith("pestora")
                        else ("", ""))
    kinds = {"pestora.framer.website": "ai", "meeting-demo.app": "ai", "janemaker.com": "content", "launch-demo.app": "ai", "try.demotools.dev": "ai"}

    def chat(model, system, user, max_tokens=1500):
        return json.dumps([{"i": x["i"], "kind": kinds[x["domain"]]} for x in json.loads(user)])
    monkeypatch.setattr(sellers.aisa, "chat", chat)
    first = sellers.sellers(cfg, tmp_path)
    hosts = {s["host"]: s for s in first["sellers"]}
    assert [s["kind"] for s in first["sellers"]] == ["ai", "ai", "ai", "ai", "content"]                 # AI and software first
    assert set(hosts) == {"pestora.framer.website", "meeting-demo.app", "janemaker.com", "launch-demo.app", "try.demotools.dev"}
    assert first["old"] == 1                                                                            # an old seller's new page
    assert hosts["pestora.framer.website"]["first_seen"] == "2026-10-13"                                # the day the site first linked
    assert hosts["pestora.framer.website"]["term"] == "pestora" and hosts["janemaker.com"]["term"] == "janemaker"
    assert first["first_run"] and first["cost_micros"] == 8 * 27600
    lead = [x for x in first["terms"] if x["term"] == "pestora"][0]
    assert lead["about"] == ("https://pestora.framer.website/", "Pestora — pest control software. Book and bill pest jobs")
    text = "\n".join(report._seller_lines(first))
    assert "| [meeting-demo.app](https://meeting-demo.app/) | AI / software | Lemon Squeezy |" in text and "not that it sells well" in text
    again = sellers.sellers(cfg, tmp_path)
    assert again["sellers"] == [] and not again["first_run"]                                              # each site reported once


def test_a_seller_is_new_only_when_the_site_first_linked_lately():
    sites = {"demotools.dev": "2026-10-13", "pestora.framer.website": "2026-10-12"}
    assert sellers.first_day("try.demotools.dev", sites) == "2026-10-13"           # named by its parent domain
    assert sellers.first_day("www.pestora.framer.website", sites) == "2026-10-12"
    assert sellers.first_day("other.framer.website", sites) == ""                  # a sibling on the same host is another site
    assert sellers.first_day("permit-records-demo.com", sites) == ""                    # a new page of an old seller


def test_a_seller_link_sits_on_the_sites_own_page():
    assert sellers.own_page("https://vouch-demo.com/") and sellers.own_page("https://write-demo.com/pricing")
    assert not sellers.own_page("https://community.n8n.io/t/freelancer-invoice/185482")
    assert not sellers.own_page("https://www.framer.com/marketplace/templates/aurius/")
    assert not sellers.own_page("https://flathub.org/apps/uk.co.weather")


def test_seller_kinds_fall_back_to_keyword_rules():
    rows = [{"host": "captions-demo.app", "title": "NotchLive — Live Captions for Mac", "description": ""},
            {"host": "themes-demo.com", "title": "Astro Themes & Tailwind CSS Templates", "description": ""},
            {"host": "news-demo.com", "title": "Murcia Today News", "description": ""}]
    got, error = sellers.kinds("gpt-4.1-mini", rows)          # the call is blocked in tests: rules take over
    assert got == {"captions-demo.app": "ai", "themes-demo.com": "template", "news-demo.com": "donation"} and error


# --- history: every seller back to 2019 ------------------------------------------------------------

from radar import seller_history  # noqa: E402


def _history_page(items, token):
    return {"h": {"tasks": [{"status_code": 20000, "result": [{"items": items, "search_after_token": token}]}]}}


def test_history_pulls_every_page_and_picks_up_where_it_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(seller_history, "PAGE", 2)
    item = lambda d, url, day: {"domain_from": d, "url_from": f"https://{d}/", "url_to": url, "first_seen": f"{day} 08:00:00 +00:00",  # noqa: E731
                                "domain_from_rank": 40}
    pages = {"": ([item("janemaker.com", "https://janemaker.gumroad.com/l/toxins", "2019-03-02"),
                   item("spam-network-demo.com", "https://spamseller.gumroad.com/", "2019-04-01")], "t1"),
             "t1": ([item("nocode.school", "https://nocode.gumroad.com/l/course", "2024-05-06")], "")}
    sent = []

    def call(calls):
        body = calls[0]["arguments"]["body"][0]
        sent.append(body)
        assert body["order_by"] == ["first_seen,asc"] and body["mode"] == "one_per_domain"
        return _history_page(*pages[body.get("search_after_token", "")]), 60000
    monkeypatch.setattr(seller_history.aisa, "call", call)
    db = seller_history.connect(tmp_path)
    first = seller_history.pull(db, ["gumroad"], 50000)                        # the budget runs out after one page
    assert first["platforms"]["gumroad"] == {"rows": 2, "done": False, "cost_micros": 60000, "spent_micros": 60000, "error": ""}
    rest = seller_history.pull(db, ["gumroad"], 1_000_000)
    assert sent[-1]["search_after_token"] == "t1" and rest["platforms"]["gumroad"]["done"]   # it went on from the saved token
    assert seller_history.pull(db, ["gumroad"], 1_000_000)["cost_micros"] == 0               # done: nothing more to buy
    got = {h: (day, pays) for h, day, pays in db.execute("SELECT host, first_seen, pays FROM sites")}
    assert got == {"janemaker.com": ("2019-03-02", 1), "spam-network-demo.com": ("2019-04-01", 0), "nocode.school": ("2024-05-06", 1)}



def test_history_update_adds_the_new_sellers_back_to_the_newest_day(tmp_path, monkeypatch):
    monkeypatch.setattr(seller_history, "PAGE", 2)
    item = lambda d, url, day: {"domain_from": d, "url_from": f"https://{d}/", "url_to": url, "first_seen": f"{day} 08:00:00 +00:00",  # noqa: E731
                                "domain_from_rank": 40}
    db = seller_history.connect(tmp_path)
    db.executemany("INSERT INTO sites (platform, host, first_seen, page, link, rank, pays) VALUES (?, ?, ?, '', '', 0, 1)",
                   [("polar", "old.app", "2026-10-10"), ("stripe", "moveddemo.app", "2025-01-01")])
    pages = {"": ([item("new.app", "https://buy.polar.sh/polar_cl_1", "2026-10-14"),
                   item("old.app", "https://buy.polar.sh/polar_cl_2", "2026-10-13")], "t1"),     # an old seller's new page
             "t1": ([item("moveddemo.app", "https://buy.polar.sh/polar_cl_3", "2026-10-12"),
                     item("older.app", "https://buy.polar.sh/polar_cl_4", "2026-10-02")], "t2"),   # past 10-07: the last page read
             "t2": ([item("never.app", "https://buy.polar.sh/polar_cl_5", "2026-09-01")], "")}
    sent = []

    def call(calls):
        body = calls[0]["arguments"]["body"][0]
        sent.append(body.get("search_after_token", ""))
        assert body["order_by"] == ["first_seen,desc"]
        return _history_page(*pages[body.get("search_after_token", "")]), 60000
    monkeypatch.setattr(seller_history.aisa, "call", call)
    got = seller_history.update(db, ["polar"], 1_000_000, date(2026, 10, 15))["platforms"]["polar"]
    assert sent == ["", "t1"] and got["since"] == "2026-10-07"
    assert got["new"] == 3 and got["paying"] == 3 and got["elsewhere"] == 1 and got["cost_micros"] == 120000   # moveddemo.app sold via Stripe
    hosts = {h for (h,) in db.execute("SELECT host FROM sites WHERE platform = 'polar'")}
    assert hosts == {"old.app", "new.app", "moveddemo.app", "older.app"}
    assert db.execute("SELECT day, new, elsewhere FROM updates").fetchall() == [("2026-10-15", 3, 1)]

def test_history_stops_on_a_refused_page(tmp_path, monkeypatch):
    refused = {"h": {"tasks": [{"status_code": 40501, "status_message": "Invalid Field: 'search_after_token'."}]}}
    monkeypatch.setattr(seller_history.aisa, "call", lambda calls: (refused, 0))
    db = seller_history.connect(tmp_path)
    got = seller_history.pull(db, ["polar"], 1_000_000)["platforms"]["polar"]
    assert not got["done"] and "Invalid Field" in got["error"]


def test_history_reads_sorts_and_counts(tmp_path, monkeypatch):
    db = seller_history.connect(tmp_path)
    db.executemany("INSERT INTO sites (platform, host, first_seen, page, link, rank, pays) VALUES (?, ?, ?, '', '', 0, ?)",
                   [("stripe", "captions-demo.app", "2025-02-03", 1), ("polar", "captions-demo.app", "2025-03-01", 1),
                    ("stripe", "gone.example", "2021-07-01", 1), ("gumroad", "spam-network-demo.com", "2019-04-01", 0)])
    monkeypatch.setattr(seller_history.sellers, "page_words",
                        lambda host, limit: ("NotchLive — Live Captions for Mac", "") if host == "captions-demo.app" else ("", ""))
    assert seller_history.read(db, 2) == {"step": "read", "read": 2, "answered": 1}            # spam-network-demo.com is no seller: not read
    monkeypatch.setattr(seller_history.sellers.aisa, "chat", lambda model, system, user, max_tokens=1500: '[{"i": 0, "kind": "ai"}]')
    sorted_ = seller_history.sort(db, "gpt-4.1-mini", 1_000_000)
    assert sorted_["sorted"] == 1 and sorted_["by_rules"] == 0                                 # the dead site is left unsorted
    out = tmp_path / "sellers.csv"
    s = seller_history.summary(db, out)
    assert ["2025", "ai", 2] in s["by_year_kind"] and ["2021", "no answer", 1] in s["by_year_kind"]
    assert ["2025-02", "stripe", 1] in s["by_month"] and out.read_text(encoding="utf-8-sig").count("captions-demo.app") == 2
    assert "| Stripe | 2 | 2 |" in seller_history.text(s)


def test_builder_defaults_are_not_the_sellers_words(tmp_path):
    notion = ("Notion", "A collaborative AI workspace, built on your company context. Build and orchestrate agents")
    assert sellers.own_words(*notion) == ("", "")
    assert sellers.own_words("TalentDemo - Recruitment Tracker", "Made with Framer") == ("TalentDemo - Recruitment Tracker", "")
    assert sellers.own_words("", "Discover Switchy, the easiest retargeting url shortner.") == ("", "")
    assert sellers.own_words("Hōm", "Home") == ("Hōm", "") and sellers.own_words("Hōm", "Homeowner intelligence")[1]
    assert sellers.own_words("NotchLive — Home of live captions", "Captions for your Mac") == ("NotchLive — Home of live captions",
                                                                                              "Captions for your Mac")
    db = seller_history.connect(tmp_path)
    db.executemany("INSERT INTO sites (platform, host, first_seen, page, link, rank, pays, title, description, read_on, kind, kind_by) "
                   "VALUES ('gumroad', ?, '2026-01-02', '', '', 0, 1, ?, ?, '2026-10-14', 'ai', 'jev')",
                   [("shop.notion.site", *notion), ("talentdemo.com", "TalentDemo - Recruitment Tracker", "Made with Framer")])
    assert seller_history.clean_defaults(db) == 2 and seller_history.clean_defaults(db) == 0
    got = {h: (t, d, k) for h, t, d, k in db.execute("SELECT host, title, description, kind FROM sites")}
    assert got == {"shop.notion.site": ("", "", None), "talentdemo.com": ("TalentDemo - Recruitment Tracker", "", "ai")}



def test_forums_are_not_sellers(tmp_path):
    assert sellers.shared("community.obsidian.md") and sellers.shared("forum.cursor.com") and not sellers.shared("help.captions-demo.app")
    db = seller_history.connect(tmp_path)
    db.executemany("INSERT INTO sites (platform, host, first_seen, page, link, rank, pays) VALUES ('gumroad', ?, '2026-10-12', '', '', 0, 1)",
                   [("community.shopify.com",), ("captions-demo.app",)])
    assert seller_history.drop_shared(db) == 1
    assert dict(db.execute("SELECT host, pays FROM sites")) == {"community.shopify.com": 0, "captions-demo.app": 1}

def test_page_words_reads_a_gzipped_head_cut_short(monkeypatch):
    import gzip
    import random
    head = "<html><head><title>NotchLive — Live Captions</title><meta name='description' content='Captions for your Mac'>".encode()
    body = gzip.compress(head + random.Random(1).randbytes(50_000))      # noise: the stream is long and cut after 400 bytes

    class Response:
        headers = {"Content-Encoding": "gzip"}

        def read(self, n):
            return body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(sellers.urllib.request, "urlopen", lambda req, timeout: Response())
    assert sellers.page_words("captions-demo.app", 400) == ("NotchLive — Live Captions", "Captions for your Mac")


def test_history_sorts_by_rules_for_free_and_the_model_redoes_them(tmp_path, monkeypatch):
    db = seller_history.connect(tmp_path)
    db.executemany("INSERT INTO sites (platform, host, first_seen, page, link, rank, pays, title, description, read_on) "
                   "VALUES ('stripe', ?, '2026-01-02', '', '', 0, 1, ?, '', '2026-10-14')",
                   [("captions-demo.app", "NotchLive — Live Captions for Mac"), ("themes-demo.com", "Astro Themes & Tailwind CSS Templates")])
    free = seller_history.sort(db, "", 0)                                            # no model: rules, whatever the budget
    assert free["sorted"] == 2 and free["by_rules"] == 2 and free["cost_micros"] == 0
    monkeypatch.setattr(seller_history.sellers.aisa, "chat",
                        lambda model, system, user, max_tokens=1500: '[{"i": 0, "kind": "ai"}, {"i": 1, "kind": "template"}]')
    paid = seller_history.sort(db, "gpt-4.1-mini", 1_000_000)
    assert paid["sorted"] == 2 and paid["by_rules"] == 0
    assert dict(db.execute("SELECT host, kind_by FROM sites")) == {"captions-demo.app": "model", "themes-demo.com": "model"}


# --- 0.5 local: ⑨ feeds, ⑧ builders, ⑩ money, side evidence ------------------------------------

from radar import feeds, money, sidelights  # noqa: E402

PH = ('<feed><entry><id>1</id><link rel="alternate" href="https://www.producthunt.com/products/dashrendr"/>'
      '<title>Dashrendr</title><content type="html">&lt;p&gt;Dashboards from a sentence&lt;/p&gt;</content></entry></feed>')


def test_feeds_take_a_snapshot_first_then_report_what_is_new(cfg, tmp_path, monkeypatch):
    cfg["feeds"]["enabled"] = ["producthunt", "hn", "openrouter", "awesome", "trending"]
    cfg["feeds"]["awesome"] = ["https://raw.example/README.md"]
    cfg["feeds"]["trending"] = ["tiktok"]
    now = int(feeds.time.time())
    pages = {"https://www.producthunt.com/feed": PH,
             "https://openrouter.ai/api/v1/models": json.dumps({"data": [{"id": "x/pareto", "name": "Unbiased: Pareto 26.9", "created": now}]}),
             "https://raw.example/README.md": "- [Notchlive](https://captions-demo.app) - live captions\n"}

    def get(url):
        if "hn.algolia" in url:
            return json.dumps({"hits": [{"objectID": "9", "title": "Show HN: Quarry – SQL for spreadsheets", "points": 80}]}).encode()
        return pages[url].encode()
    monkeypatch.setattr(feeds, "get", get)
    monkeypatch.setattr(feeds.aisa, "call", lambda calls: ({"tiktok": {"data": {"trending_search_words": [
        {"trendingSearchWord": "AI yearbook filter"}, {"trendingSearchWord": "Mayor Arrested"}]}}}, 1000))
    first = feeds.feeds(cfg, tmp_path, today=date(2026, 10, 14))
    assert set(first["first_run"]) == {"producthunt", "hn", "openrouter", "awesome", "trending"}
    # a first run lists what the source itself dates as recent; the undated awesome list and the undated PH entry wait
    assert sorted(x["term"] for x in first["terms"]) == ["ai yearbook filter", "pareto 26.9", "quarry"]
    pages["https://raw.example/README.md"] += "- [Pixdemo](https://pixdemo.app) - prompts behind images\n"
    again = feeds.feeds(cfg, tmp_path, today=date(2026, 10, 15))
    assert [x["term"] for x in again["terms"]] == ["pixdemo"] and again["new"]["awesome"][0]["url"] == "https://pixdemo.app"
    assert again["cost_micros"] == 1000 and not again["new"].get("trending")       # the filter word was seen yesterday
    assert feeds.TOOLISH.search("AI yearbook filter") and not feeds.TOOLISH.search("Mayor Arrested")
    assert feeds.TOOLISH.search("AI换脸特效") and not feeds.TOOLISH.search("Chair")


def test_feeds_deduplicate_shared_links_before_applying_limit(cfg, tmp_path, monkeypatch):
    cfg["feeds"]["enabled"] = ["awesome"]
    cfg["feeds"]["awesome"] = ["https://raw.example/one", "https://raw.example/two"]
    cfg["feeds"]["max_per_feed"] = 2
    pages = {url: "- [Oldtool](https://old.example) - existing tool\n" for url in cfg["feeds"]["awesome"]}
    monkeypatch.setattr(feeds, "get", lambda url: pages[url].encode())
    first = feeds.feeds(cfg, tmp_path, today=date(2026, 11, 12))
    assert first["terms"] == []

    pages["https://raw.example/one"] += "- [Newtool](https://new.example) - new tool\n"
    pages["https://raw.example/two"] += (
        "- [Newtool](https://new.example) - the same tool\n"
        "- [Nexttool](https://next.example) - another tool\n"
    )
    result = feeds.feeds(cfg, tmp_path, today=date(2026, 11, 13))

    assert [item["key"] for item in result["new"]["awesome"]] == [
        "https://new.example", "https://next.example"
    ]
    assert [item["term"] for item in result["terms"]] == ["newtool", "nexttool"]
    assert feeds.feeds(cfg, tmp_path, today=date(2026, 11, 14))["terms"] == []


def test_weekly_feeds_wait_for_their_day(cfg, tmp_path, monkeypatch):
    cfg["feeds"]["enabled"], cfg["feeds"]["weekly"], cfg["feeds"]["weekday"] = ["rising"], ["rising"], 0
    asked = []
    monkeypatch.setattr(feeds.goanyapi, "related_queries", lambda seed: (asked.append(seed) or {"rising": [f"{seed} free"]}, 6000))
    assert feeds.feeds(cfg, tmp_path, today=date(2026, 10, 14))["read"] == {} and asked == []          # a Wednesday
    assert feeds.feeds(cfg, tmp_path, today=date(2026, 10, 14), weekly=True)["read"]["rising"] == len(cfg["feeds"]["seeds"])


def test_money_finds_the_owners_other_sites_and_new_advertisers(cfg, tmp_path, monkeypatch):
    cfg["money"]["ad_terms"] = ["ai headshot"]
    monkeypatch.setattr(money.goanyapi, "key", lambda: "k")

    def call(path, **p):
        if "domain" in p:
            return {"publisherIds": {"direct": ["pub-42"] if p["domain"] == "tooldirdemo.ai" else []}}, 3000
        return {"domains": ["coloringdemo.ai", "lyricsdemo.io", "tooldirdemo.ai"]}, 3000
    monkeypatch.setattr(money.goanyapi, "call", call)
    ads = iter([(["Headshot Pro"], 4000), (["Headshot Pro", "Aragon AI"], 4000)])
    monkeypatch.setattr(money.goanyapi, "advertisers", lambda term: next(ads))
    first = money.money(cfg, tmp_path, ["tooldirdemo.ai", "photoroom.com"])
    assert first["portfolios"] == [{"site": "tooldirdemo.ai", "pub": "pub-42", "sisters": ["coloringdemo.ai", "lyricsdemo.io"]}]
    assert first["advertisers"] == [] and {x["term"] for x in first["terms"]} == {"coloringdemo", "lyricsdemo"}   # first look: no "new" ads
    again = money.money(cfg, tmp_path, ["tooldirdemo.ai"])
    assert again["portfolios"] == [] and again["advertisers"] == [{"term": "ai headshot", "advertisers": ["Aragon AI"]}]
    text = "\n".join(report._money_lines(first))
    assert "tooldirdemo.ai (AdSense pub-42): coloringdemo.ai, lyricsdemo.io" in text


def test_builders_keep_the_badge_and_leave_posts_about_the_tool(cfg, tmp_path, monkeypatch):
    cfg["builders"]["platforms"] = ["lovable", "beehiiv"]
    rows = {"lovable.dev": [("formdemo.app", "https://lovable.dev/projects/lovp_1", "Edit with"),
                            ("blog.example", "https://lovable.dev/", "Lovable")],
            "beehiiv.com": [("radar.newsletter-demo.com", "https://www.beehiiv.com/powered-by?publication_name=Radar", "Powered by beehiiv")]}

    def call(calls):
        target = calls[0]["arguments"]["body"][0]["target"]
        if calls[0]["tool"] == sellers.DOMAINS:
            return _sites([d for d, _, _ in rows[target]]), 27600
        return {"b": {"tasks": [{"result": [{"items": [{"domain_from": d, "url_to": u, "anchor": a, "url_from": f"https://{d}/deep/page",
                                                         "first_seen": "2026-10-14 03:00:00 +00:00", "domain_from_rank": 5}
                                                        for d, u, a in rows[target]]}]}]}}, 27600
    monkeypatch.setattr(sellers.aisa, "call", call)
    monkeypatch.setattr(sellers, "page_words", lambda host: ("FormDemo — forms that fill themselves", "") if host == "formdemo.app"
                        else ("Radar", "A weekly letter on climate startups"))
    got = sellers.builders(cfg, tmp_path)
    assert {s["host"] for s in got["sites"]} == {"formdemo.app", "radar.newsletter-demo.com"}      # the post about Lovable is out
    assert "| [formdemo.app](https://formdemo.app/) | Lovable |" in "\n".join(report._builder_lines(got))


def test_seller_traffic_details_come_from_the_visits_answer():
    info = sellers.traffic_info({"EstimatedMonthlyVisits": {"2026-08-01": 10, "2026-09-01": 924}, "DateData": {"registration": "2026-08-07T11:54Z"},
                                 "TrafficSources": {"Direct": 0.36, "Search": 0.32, "SearchPaid": 0.01, "GenAI": 0.02}})
    assert info == {"visits": 924, "registered": "2026-08-07", "top_source": "direct", "ai_share": 0.02}


def test_side_evidence_reads_certificates_wikipedia_and_google(monkeypatch):
    answers = {
        "crt.sh": [{"common_name": "nanobanana-pro-demo.app", "not_before": "2026-10-12T00:00:00"},
                   {"common_name": "old-nanobanana-demo.com", "not_before": "2026-09-01T00:00:00"}],
        "opensearch": ["nano banana", ["Nano Banana"]],
        "pageviews": {"items": [{"views": v} for v in [10] * 7 + [40] * 7]},
        "trends": ')]}\',\n{"default": {"topics": [{"title": "Nano Banana", "type": "AI model"}]}}',
    }

    def get(url):
        key = next(k for k in ("crt.sh", "opensearch", "pageviews", "trends") if k in url)
        value = answers[key]
        return value.encode() if isinstance(value, str) else json.dumps(value).encode()
    monkeypatch.setattr(sidelights, "_get", get)
    got = sidelights.evidence({"names": 5, "whois_names": 0, "whois_days": 30}, ["nano banana"], today=date(2026, 10, 14))
    row = got["rows"][0]
    assert row["sites"] == (1, ["nanobanana-pro-demo.app"]) and row["wiki"] == ("Nano Banana", 280, 70) and row["entity"] == {"type": "AI model"}
    assert "| nano banana | 1 (nanobanana-pro-demo.app) | 280 / 70 | AI model |" in "\n".join(report._side_lines(got))
    assert sidelights.new_sites("jev", date(2026, 10, 14)) == (0, [])                 # too short to search for


def test_tails_size_the_searches_around_paying_products(monkeypatch):
    monkeypatch.setattr(sidelights.usecases, "volumes", lambda phrases, loc: ({"photoroom alternative": {"volume": 1900},
                                                                                "photoroom free": {"volume": 30}}, 90000))
    got = sidelights.tails({"products": 8, "min_volume": 50, "location_code": 2840}, ["photoroom"])
    assert got["rows"] == [{"phrase": "photoroom alternative", "volume": 1900}] and got["cost_micros"] == 90000


def test_asks_also_read_x_software_recommendations_and_freelancer(monkeypatch):
    import gzip
    import io
    c = {"x_query": '"is there a tool"', "x_min_likes": 10, "stack_all": ["softwarerecs"], "stack_sites": ["superuser"],
         "stack_titles": ["automate"], "freelancer_queries": ["automate"], "min_score": 2}
    monkeypatch.setattr(asks.aisa, "call", lambda calls: ({"x": {"tweets": [
        {"id": "1", "text": "is there a tool that turns slides into notes?", "likeCount": 44, "url": "u"}]}}, 3000))
    stack = {"softwarerecs": {"items": [{"question_id": 7, "title": "Open-source tool to auto-generate API regression tests", "score": 0, "link": "l"}]},
             "superuser": {"items": [{"question_id": 8, "title": "Automate renaming 500 PDFs &amp; folders", "score": 3, "link": "m"}]}}
    jobs = {"result": {"projects": [{"id": 40767538, "title": "Claude AI & Canva Graphic Automation Expert Needed", "seo_url": "canva/x",
                                     "bid_stats": {"bid_count": 124}, "budget": {"minimum": 20.0, "maximum": 250.0},
                                     "currency": {"exchange_rate": 1.31512}, "preview_description": "Build an AI graphic system"}]}}

    class Res(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        url = req.full_url
        if "freelancer.com" in url:
            return Res(json.dumps(jobs).encode())
        site = "softwarerecs" if "softwarerecs" in url else "superuser"
        return Res(gzip.compress(json.dumps(stack[site]).encode()))
    monkeypatch.setattr(asks.urllib.request, "urlopen", urlopen)
    x, _ = asks.x_posts(c)
    se, _ = asks.stack_questions(c)
    fl, _ = asks.freelancer_jobs(c)
    assert x[0]["subreddit"] == "x" and x[0]["score"] == 44
    assert [q["title"] for q in se] == ["Open-source tool to auto-generate API regression tests", "Automate renaming 500 PDFs & folders"]
    assert se[0]["score"] == 2                       # every Software Recommendations question is a request, votes or not
    assert fl[0]["score"] == 124 and fl[0]["budget_usd"] == 329 and fl[0]["url"] == "https://www.freelancer.com/projects/canva/x"


def test_a_name_that_belongs_to_something_else_is_taken(monkeypatch):
    raw = ')]}\',\n' + json.dumps({"default": {"topics": [{"title": "Gemini 4", "type": "Space flight"},
                                                          {"title": "Gemini - 4", "type": "Auto repair shop in Sofia, Bulgaria"}]}})
    monkeypatch.setattr(sidelights, "_get", lambda url: raw.encode())
    assert sidelights.entity("gemini 4") == {"taken": "Space flight"}
    topic = ')]}\',\n' + json.dumps({"default": {"topics": [{"title": "Claude Code", "type": "Topic"}]}})
    monkeypatch.setattr(sidelights, "_get", lambda url: topic.encode())
    assert sidelights.entity("claude code") == {"type": "Topic"}                     # known, just not typed yet
    row = {"rows": [{"name": "gemini 4", "entity": {"taken": "Space flight"}, "discord": 0}]}
    assert "name taken: Google knows only a Space flight" in "\n".join(report._side_lines(row))


def test_recent_reads_iso_dates():
    now = datetime.now(timezone.utc)
    assert feeds.recent((now - timedelta(hours=30)).isoformat()) and not feeds.recent((now - timedelta(days=3)).isoformat())
    assert feeds.recent(now.strftime("%Y-%m-%dT%H:%M:%S.000Z")) and not feeds.recent("") and not feeds.recent("soon")


def test_dodo_and_waffo_links_take_money():
    dodo, waffo = sellers.PLATFORMS["dodo"][1], sellers.PLATFORMS["waffo"][1]
    assert dodo("checkout.dodopayments.com", "/buy/pdt_0NcEz5RotWnutLQWHmr") and not dodo("checkout.dodopayments.com", "/")
    assert waffo("checkout.waffo.ai", "/acme/pro-plan") and not waffo("checkout.waffo.ai", "/")


def test_side_evidence_asks_whois_for_the_first_names(monkeypatch):
    asked = []
    monkeypatch.setattr(sidelights.aisa, "rest", lambda path, task: (asked.append(task["filters"][0][2]) or
                        {"total_count": 4, "items": [{"domain": "nanobanana-demo.app"}, {"domain": "nano-banana-demo.ai"}]}, 121200))
    monkeypatch.setattr(sidelights, "_get", lambda url: b"[]" if "crt.sh" in url else (_ for _ in ()).throw(OSError("offline")))
    got = sidelights.evidence({"names": 2, "whois_names": 1, "whois_days": 30}, ["nano banana", "seedream five"], today=date(2026, 10, 15))
    assert asked == ["%nanobanana%"] and got["cost_micros"] == 121200
    assert got["rows"][0]["sites"] == (4, ["nanobanana-demo.app", "nano-banana-demo.ai"]) and got["rows"][0]["sites_from"] == "whois"
    assert got["rows"][1]["sites"] == (0, []) and "sites_from" not in got["rows"][1]           # crt.sh for the rest
    assert "4 (nanobanana-demo.app, nano-banana-demo.ai) · whois" in "\n".join(report._side_lines(got))


def test_rising_words_must_share_the_seed(cfg, tmp_path, monkeypatch):
    cfg["feeds"]["enabled"], cfg["feeds"]["weekly"], cfg["feeds"]["seeds"] = ["rising"], [], ["ai generator"]
    monkeypatch.setattr(feeds.goanyapi, "related_queries", lambda seed: (
        {"rising": ["hotel lobby ai generator", "best pizza near me", "ai rap video generator", "how to bake a cake"]}, 6000))
    got = feeds.feeds(cfg, tmp_path, today=date(2026, 10, 15))
    assert [x["term"] for x in got["terms"]] == ["hotel lobby ai generator", "ai rap video generator"]


def test_asks_drop_old_posts_and_name_their_source(cfg, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    old = {"id": "k7nsx7", "title": "Is there a website that compares prices", "text": "", "subreddit": "NoStupidQuestions",
           "score": 16656, "created": now - 5 * 365 * 86400}
    new = dict(old, id="n1", title="Is there an AI tool that turns slides into notes", created=now - 86400)
    monkeypatch.setattr(asks.social, "reddit", lambda q, **k: ([old, new], 1880))
    c = dict(cfg["asks"], patterns=["x"], extra_sources=[])
    posts, _, _ = asks.collect(c)
    assert [p["id"] for p in posts] == ["n1"]                    # Reddit's "week" let a 2020 post through
    assert [asks.where(s) for s in ("PhD", "x", "freelancer", "softwarerecs")] == ["r/PhD", "X", "Freelancer", "Stack Exchange · softwarerecs"]
    assert asks.x_time("Fri Jul 17 19:56:23 +0000 2026") > 0 and asks.x_time("") == 0.0


def test_only_software_built_with_a_builder_becomes_a_lead(cfg, tmp_path, monkeypatch):
    cfg["builders"]["platforms"] = ["lovable"]
    rows = [("formdemo.app", "Edit with"), ("pizzademo.it", "Edit with")]

    def call(calls):
        if calls[0]["tool"] == sellers.DOMAINS:
            return _sites([d for d, _ in rows]), 27600
        return {"b": {"tasks": [{"result": [{"items": [{"domain_from": d, "url_to": "https://lovable.dev/projects/x", "anchor": a,
                                                         "url_from": f"https://{d}/", "first_seen": "2026-10-15 03:00:00 +00:00",
                                                         "domain_from_rank": 5} for d, a in rows]}]}]}}, 27600
    monkeypatch.setattr(sellers.aisa, "call", call)
    monkeypatch.setattr(sellers, "page_words", lambda host: ("FormDemo — forms that fill themselves with AI", "") if host == "formdemo.app"
                        else ("Pizzeria Demo — Pizza napoletana a Roma", ""))
    got = sellers.builders(cfg, tmp_path)
    assert [x["term"] for x in got["terms"]] == ["formdemo"] and {s["host"] for s in got["sites"]} == {"formdemo.app", "pizzademo.it"}
    assert got["by_kind"]["ai"] == 1


def test_the_status_says_which_signals_were_slow():
    b = {"x": {"skipped": True}, "hf": {"skipped": True}, "breakout": {"skipped": True}, "sitemap": {},
         "feed": {"seconds": 1860, "new": {}, "errors": []}, "seller": {"skipped": True}}
    lines = report._status_lines(b, {"usecases": {"seconds": 45}, "sidelights": {"seconds": 900}})
    assert "- Time: 46 min in all; a minute or more: feed 31 min、sidelights 15 min" in lines


def test_money_asks_about_page_one_tools_and_whois_follows_searches():
    uc = {"rows": [{"phrase": "wordcraft", "kind": "name", "per_day": 10782, "decision": "prioritize",
                    "competition": {"tools": [{"domain": "wordcraftdemo.it", "dr": 1.5}, {"domain": "onlyoffice.com", "dr": 78}]}},
                   {"phrase": "digup", "kind": "name", "per_day": 86, "decision": "watch",
                    "competition": {"tools": [{"domain": "digupdemo.co.il", "dr": 0}, {"domain": "onlyoffice.com", "dr": 78}]}},
                   {"phrase": "old thing", "kind": "generic", "per_day": 5, "decision": "skip",
                    "competition": {"tools": [{"domain": "skip.me", "dr": 50}]}}]}
    assert cli.page_one_tools(uc, max_dr=100) == ["onlyoffice.com", "wordcraftdemo.it", "digupdemo.co.il"]   # strongest first, no skipped rows
    assert cli.page_one_tools(uc) == ["wordcraftdemo.it", "digupdemo.co.il"]          # a rating above 60 is a big site, not a portfolio
    assert cli.by_searches(["digup", "qwen21 nsfw pose skill", "wordcraft"], uc) == ["wordcraft", "digup", "qwen21 nsfw pose skill"]


def test_a_payment_link_on_several_new_sites_is_a_network(cfg, tmp_path, monkeypatch):
    cfg["sellers"]["platforms"] = ["stripe"]
    rows = [("dachecker-demo.shop", "https://buy.stripe.com/9B6a"), ("dachecker-demo2.space", "https://buy.stripe.com/9B6a"),
            ("formdemo.app", "https://buy.stripe.com/own1")]

    def call(calls):
        if calls[0]["tool"] == sellers.DOMAINS:
            return _sites([d for d, _ in rows]), 27600
        return _backlinks(rows), 27600
    monkeypatch.setattr(sellers.aisa, "call", call)
    got = sellers.fresh(cfg["sellers"], {})
    assert set(got["found"]) == {"formdemo.app"} and got["networked"] == 2


def test_an_adsense_id_on_dozens_of_sites_is_a_network():
    assert money.is_network([f"site{i}.com" for i in range(31)], {"max_sisters": 30})
    assert not money.is_network(["video-demo.online", "photo-demo.online", "logo-demo.io"], {"max_sisters": 30})


# --- 0.6: word age, markets, page-one shape ------------------------------------------------------

from radar import markets, serpshape, wordage  # noqa: E402


def _weeks(values, end=TODAY):
    """A weekly Trends task ending with the week still running (its period ends today), as DataForSEO returns it."""
    starts = [end - timedelta(days=7 * (len(values) - 1 - i)) for i in range(len(values))]
    data = [{"date_from": s.isoformat(), "date_to": min(s + timedelta(days=6), end).isoformat(), "values": [v]} for s, v in zip(starts, values)]
    return {"result": [{"items": [{"type": "dataforseo_trends_graph", "data": data}]}]}


def test_first_seen_is_the_first_week_with_interest():
    age = wordage.first_seen(wordage.series(_weeks([0] * 250 + [3, 20, 60, 100, 80, 70, 90])), TODAY)
    assert age["first_seen"] == (TODAY - timedelta(days=42)).isoformat() and age["is_new"] and not age["since_start"]
    old = wordage.first_seen(wordage.series(_weeks([0] * 200 + [5] + [0] * 56)), TODAY)
    assert old["first_seen"] == (TODAY - timedelta(days=56 * 7)).isoformat() and not old["is_new"]
    always = wordage.first_seen(wordage.series(_weeks([12] * 257)), TODAY)
    assert always["since_start"] and not always["is_new"]


def test_first_seen_ignores_the_unsettled_tail():
    # the week still running reads 100 for a term nobody searched: not a new word, no data at all
    assert wordage.first_seen(wordage.series(_weeks([0] * 256 + [100])), TODAY)["no_data"]
    # daily data: Google's "0, 100" on the last two days
    task = {"result": [{"items": [{"type": "dataforseo_trends_graph", "data": [
        {"date_from": d.isoformat(), "date_to": d.isoformat(), "values": [v]} for d, v in _daily([0] * 88 + [0, 100], end=TODAY - timedelta(days=1))]}]}]}
    assert wordage.first_seen(wordage.series(task), TODAY)["no_data"]
    # a period flagged missing_data is not read either
    task = _weeks([0] * 255 + [40, 0])
    task["result"][0]["items"][0]["data"][-2]["missing_data"] = True
    assert wordage.first_seen(wordage.series(task), TODAY)["no_data"]


def test_word_age_reads_the_acted_on_rows(cfg, monkeypatch):
    cfg["word_age"]["enabled"] = True
    asked = []

    def call_all(calls):
        asked.extend(c["arguments"]["body"][0] for c in calls)
        return {c["call_id"]: {"tasks": [_weeks([0] * 250 + [9] * 7)]} for c in calls}, 1200 * len(calls)
    monkeypatch.setattr(wordage.aisa, "call_all", call_all)
    rows = [{"phrase": "pdfcraft"}, {"phrase": "ai image detector"}]
    assert wordage.assess(rows, cfg, TODAY) == (2400, [])
    assert all(r["is_new"] for r in rows) and asked[0] == {"keywords": ["pdfcraft"], "time_range": "past_5_years"}


def test_best_market_divides_by_difficulty_only_when_every_market_has_one():
    by = {"US": {"volume": 5000, "kd": 50}, "DE": {"volume": 900, "kd": 5}, "JP": {"volume": 0, "kd": None}}
    assert markets.best_market(by) == "DE"                          # 180 a point against 100
    by["FR"] = {"volume": 300, "kd": None}
    assert markets.best_market(by) == "US"                          # a difficulty is missing: the most searches
    assert markets.best_market({"US": {"volume": 0, "kd": 10}}) is None
    assert markets.best_market({"US": {"volume": 400, "kd": 0}, "DE": {"volume": 300, "kd": 2}}) == "US"


def test_markets_reuse_the_tables_own_volumes_and_buy_the_rest(cfg, monkeypatch):
    cfg["markets"].update(enabled=True, codes=["US", "DE"], difficulty=True, top_region=True)
    bought = []

    def call(calls):
        body = calls[0]["arguments"]["body"][0]
        if calls[0]["tool"] == markets.VOLUME_TOOL:
            bought.append((body["location_code"], body["keywords"]))
            vol = {2840: {"pdfcraft": 8000}, 2276: {"pdfcraft": 2000, "ai image detector": 4000}}[body["location_code"]]
            return {"vol": {"tasks": [{"result": [{"keyword": k, "search_volume": vol.get(k, 0)} for k in body["keywords"]]}]}}, 90000
        kd = {"en": {"pdfcraft": 30, "ai image detector": 60}, "de": {"pdfcraft": 10, "ai image detector": 20}}[body["language_code"]]
        return {"kd": {"tasks": [{"result": [{"items": [{"keyword": k, "keyword_properties": {"keyword_difficulty": kd[k]}}
                                                        for k in body["keywords"]]}]}]}}, 12000
    regions = {"result": [{"items": [{"type": "subregion_interests", "interests": [{"keyword": "x", "values": [   # as DataForSEO answers
        {"geo_id": "US", "geo_name": "United States", "value": 40}, {"geo_id": "DE", "geo_name": "Germany", "value": 70},
        {"geo_id": "SX", "geo_name": "Sint Maarten", "value": 100}]}]}]}]}       # a small place tops the share: left out
    monkeypatch.setattr(markets.aisa, "call", call)
    monkeypatch.setattr(markets.aisa, "call_all", lambda calls: ({c["call_id"]: {"tasks": [regions]} for c in calls}, 2400 * len(calls)))
    rows = [{"phrase": "pdfcraft"}, {"phrase": "ai image detector"}]
    cost, errors = markets.assess(rows, cfg, {2840: {"ai image detector": {"volume": 60000, "month": "2026-08"}}})
    assert errors == [] and bought == [(2840, ["pdfcraft"]), (2276, ["pdfcraft", "ai image detector"])]
    assert cost == 2 * 90000 + 2 * 12000 + 2 * 2400
    pdf, det = rows
    assert pdf["markets"]["by"] == {"US": {"volume": 8000, "kd": 30}, "DE": {"volume": 2000, "kd": 10}}
    assert pdf["markets"]["best"] == "US" and det["markets"]["best"] == "US"   # 267 vs 200; 1000 vs 200
    assert det["markets"]["top_region"] == {"id": "DE", "name": "Germany", "value": 70}
    assert report._markets(pdf["markets"]) == "**best US** · US 8k/30 · DE 2k/10 · top Germany"


def test_homepages_inner_pages_and_platforms_from_urls():
    for url in ("https://pdfdemo.app", "https://pdfdemo.app/", "https://www.x.com/index.html", "https://x.com/en/", "https://x.com/zh-cn",
                "https://x.com/?ref=a"):
        assert serpshape.is_homepage(url), url
    for url in ("https://canva.com/ai", "https://x.com/blog/post", "https://github.com/a/b", "https://x.com/en/tools/pdf"):
        assert not serpshape.is_homepage(url), url
    plats = config.DEFAULTS["serp_shape"]["platforms"]
    assert serpshape.on_platform("en.wikipedia.org", plats) and serpshape.on_platform("someone.github.io", plats)
    assert serpshape.on_platform("github.com", plats) and not serpshape.on_platform("github.io", plats)
    assert not serpshape.on_platform("notgithub.com", plats) and not serpshape.on_platform("google.com", plats)


def _results(*urls_titles):
    return [{"url": u, "title": t, "domain": serpshape.domain_of(u)} for u, t in urls_titles]


def test_serp_shape_verdicts():
    plats = config.DEFAULTS["serp_shape"]["platforms"]
    inner = _results(*[(f"https://site{i}.com/blog/post-{i}", f"Post {i}") for i in range(7)],
                     ("https://tool1.com/", "Tool One"), ("https://tool2.com/", "Tool Two"), ("https://reddit.com/r/x/1", "thread"))
    s = serpshape.shape(inner, "pdf editor", plats)
    assert s["verdict"] == "beatable" and (s["homepages"], s["inner"], s["platforms"]) == (2, 8, 1)
    assert s["top"] == [{"url": "https://site0.com/blog/post-0", "title": "Post 0"}, {"url": "https://site1.com/blog/post-1", "title": "Post 1"}]
    # one homepage built on the exact term: contested, however many inner pages there are
    s = serpshape.shape(inner[:9] + _results(("https://pdfeditor.example/", "Free online")), "pdf editor", plats)
    assert s["exact_home"] and s["verdict"] == "contested"
    s = serpshape.shape(_results(("https://best.com/", "PDF Editor: edit PDFs free")) + inner[:9], "pdf editor", plats)
    assert s["exact_home"] and s["exact_titles"] == 1 and s["verdict"] == "contested"
    held = _results(*[(f"https://github.com/a/r{i}", "repo") for i in range(3)], ("https://youtube.com/watch?v=1", "v"),
                    ("https://me.github.io/pdf", "p"), *[(f"https://s{i}.com/", "home") for i in range(5)])
    assert serpshape.shape(held, "pdf editor", plats)["verdict"] == "platform-held"
    homes = _results(*[(f"https://s{i}.com/", "home") for i in range(6)]) + inner[:4]
    assert serpshape.shape(homes, "pdf editor", plats)["verdict"] == "contested"
    assert serpshape.shape([], "pdf editor", plats)["verdict"] is None
    assert not serpshape.has_phrase("PDF Editors compared", "pdf editor")


def test_a_new_term_reads_one_step_easier_on_page_one(cfg, tmp_path, monkeypatch):
    assert competition.verdict(0, 55, new=True) == "mixed" and competition.verdict(1, 30, new=True) == "open"
    assert competition.verdict(3, None, new=True) == "open" and competition.verdict(None, None, new=True) == "mixed"
    cfg["competition"]["max_rows"] = 8
    strong = [("canva.com", "https://canva.com/ai", "Canva AI"), ("adobe.com", "https://adobe.com/x", "Adobe")]

    def call(calls):
        c = calls[0]
        if c["call_id"] == "serp":
            return {"serp": _serp(strong)}, 2000
        return {"kd": {"tasks": [{"result": [{"items": [{"keyword": k, "keyword_properties": {"keyword_difficulty": 60}}
                                                        for k in c["arguments"]["body"][0]["keywords"]]}]}]}}, 12000
    monkeypatch.setattr(competition.aisa, "call", call)
    rows = [{"phrase": "old term", "decision": "prioritize"}, {"phrase": "new term", "decision": "prioritize", "is_new": True}]
    competition.assess(rows, cfg, tmp_path)
    old, new = rows
    assert old["decision"] == "watch" and old["demoted"] and old["competition"]["verdict"] == "crowded"
    assert new["decision"] == "prioritize" and new["competition"]["verdict"] == "mixed" and new["competition"]["eased"]
    assert new["competition"]["results"][0]["url"] == "https://canva.com/ai"
    # the SERP shape reuses the results the page-one check read: no second purchase
    cfg["serp_shape"]["enabled"] = True
    monkeypatch.setattr(competition.aisa, "call", lambda calls: (_ for _ in ()).throw(AssertionError("bought twice")))
    assert serpshape.assess([new], cfg) == (0, []) and new["serp"]["verdict"] == "beatable" and new["serp"]["inner"] == 2


def test_keyword_table_shows_age_markets_and_shape_in_both_languages(cfg, tmp_path, monkeypatch):
    for block in ("word_age", "markets", "serp_shape"):
        cfg[block]["enabled"] = True
    cfg["markets"].update(codes=["US", "DE"], difficulty=False, top_region=False)
    _fake_usecase_network(monkeypatch)
    page = _serp([("synthid-detector.example", "https://synthid-detector.example/", "Free AI image checker"),
                  ("medium.com", "https://medium.com/@a/synthid", "I tried it"), ("x.com", "https://x.com/blog/a", "News")])

    def call(calls):
        c = calls[0]
        if c["tool"] == markets.VOLUME_TOOL:
            return {"vol": {"tasks": [{"result": [{"keyword": k, "search_volume": 1500} for k in c["arguments"]["body"][0]["keywords"]]}]}}, 90000
        return {"serp": page}, 2000
    monkeypatch.setattr(usecases.aisa, "call", call)
    monkeypatch.setattr(usecases.aisa, "call_all", lambda calls: (
        {x["call_id"]: {"tasks": [_weeks([0] * 250 + [5] * 7)]} for x in calls}, 1200 * len(calls)))
    uc = usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY)
    rows = {r["phrase"]: r for r in uc["rows"]}
    name = rows["synthid detector"]
    assert name["decision"] == "prioritize" and name["is_new"] and name["serp"]["verdict"] == "contested"
    assert rows["ai image detector"]["markets"]["by"]["US"]["volume"] == 60000 and "serp" not in rows["ai image detector"]
    assert "photoshop alternative" in rows and "age" not in rows["photoshop alternative"]      # skip rows are not read
    assert uc["extra_errors"] == [] and uc["cost_micros"] == 100200 + 2 * 1200 + 2 * 90000 + 2000
    text = "\n".join(report.keyword_table(cfg, uc))
    week = (TODAY - timedelta(days=42)).isoformat()
    assert f"| {week} 🆕 | **best US** · US 1.5k · DE 1.5k |" in text
    assert "contested · 1 home / 2 inner · 1 platform · exact-match homepage | **prioritize** |" in text
    assert "#1 [Free AI image checker](https://synthid-detector.example/)" in text and "First searched = the first week" in text
    assert text.splitlines()[text.splitlines().index(next(x for x in text.splitlines() if x.startswith("| Keyword")))].count("|") == 17
    i18n.set_lang("zh")
    zh = "\n".join(report.keyword_table(cfg, uc))
    assert "| 首次有搜索 | 市场(月搜/KD) | 首页竞争 | 首页结构 | 决策 |" in zh and "有争夺 · 1 首页 / 2 内页 · 1 平台" in zh
    assert f"{week} 🆕 | **最佳 US**" in zh and "首次有搜索 = 近 5 年里" in zh


# ---- 0.7: the twelve months, your own rank, page-one features, the .com, AI answers ----
from radar import ai_answers, curve, dotcom, yours  # noqa: E402


def _months(values, last=(2026, 8)):
    """[[YYYY-MM, searches], ...] oldest first, ending at `last`."""
    y, m = last
    out = []
    for v in reversed(values):
        out.append([f"{y}-{m:02d}", v])
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def test_twelve_months_read_as_a_word():
    new = curve.shape(_months([0, 0, 0, 0, 10, 30, 60, 90, 140, 200, 260, 320]))
    assert new["label"] == "rising" and new["since"] == "2026-03" and new["first"] == "2025-09" and new["last"] == "2026-08"
    # new within the year and already past its top: "best claude skills" as read on 2026-10-20
    best = curve.shape(_months([140, 170, 320, 880, 1300, 3600, 3600, 3600, 2900, 2900, 2400, 1900], last=(2026, 9)))
    assert best["since"] == "2026-01" and best["label"] == "falling" and best["c6"] == -47
    assert report._year(best) == "▁▁▂▃▄███▇▇▆▅ new since 2026-01 · falling, -47% in 6 mo"
    launch = curve.shape(_months([33100, 22200, 27100, 60500, 60500, 90500, 60500, 49500, 33100, 27100, 22200, 18100], last=(2026, 9)))
    assert launch["label"] == "spiked" and launch["peak"] == "2026-03" and launch["from_peak"] == -80 and "since" not in launch
    spike = curve.shape(_months([100, 100, 110, 100, 1000, 600, 300, 200, 150, 120, 110, 100]))
    assert spike["label"] == "spiked" and spike["peak"] == "2026-01" and spike["from_peak"] == -90
    rise = curve.shape(_months([100] * 6 + [120, 140, 160, 180, 200, 220]))
    assert rise["label"] == "rising" and rise["c6"] == 120 and rise["c3"] == 38
    assert curve.shape(_months([500] * 6 + [400, 350, 300, 250, 200, 150]))["label"] == "falling"
    assert curve.shape(_months([300] * 12))["label"] == "steady"
    assert curve.shape(_months([10, 20, 30, 20, 10, 20, 30, 40, 30, 20, 10, 20]))["label"] == "thin"   # Google's rounding, not a trend
    assert curve.shape(_months([5, 6, 7, 8, 9, 10])) is None
    assert curve.spark([0, 50, 100]) == "▁▅█" and curve.spark([0, 0]) == ""
    # a peak repeated in the newest month is not a spike that passed
    assert curve.shape(_months([100, 100, 100, 100, 900, 100, 100, 100, 100, 100, 100, 900]))["label"] == "rising"
    assert report._year(rise).endswith("rising, +120% in 6 mo") and report._year(None) == "—"
    i18n.set_lang("zh")
    assert report._year(spike).endswith("2026-01 冲高,现在 -90%")


def test_volumes_carry_twelve_months_oldest_first(monkeypatch):
    newest_first = [{"year": 2026, "month": m, "search_volume": 100 * m} for m in range(8, 0, -1)] + \
                   [{"year": 2025, "month": m, "search_volume": None} for m in range(12, 8, -1)]
    monkeypatch.setattr(usecases.aisa, "call", lambda calls: ({"vol": {"tasks": [{"result": [
        {"keyword": "pdf editor", "search_volume": 400, "monthly_searches": newest_first}]}]}}, 90000))
    vol, cost = usecases.volumes(["pdf editor"], 2840)
    got = vol["pdf editor"]
    assert cost == 90000 and got["volume"] == 400 and got["month"] == "2026-08"
    assert got["months"][0] == ["2025-09", 0] and got["months"][-1] == ["2026-08", 800] and len(got["months"]) == 12


def test_your_domains_come_from_the_config_or_the_search_console_property(cfg):
    assert yours.site_domain("sc-domain:Example.com") == "example.com" and yours.site_domain("https://www.example.com/blog") == "example.com"
    assert yours.own_domains(cfg) == []
    cfg["gsc"] = {"site": "sc-domain:mysite.com"}
    assert yours.own_domains(cfg) == ["mysite.com"]
    cfg["yours"]["domains"] = ["https://www.other.io/"]
    assert yours.own_domains(cfg) == ["other.io"]
    assert yours.is_mine("blog.other.io", ["other.io"]) and not yours.is_mine("notother.io", ["other.io"])


def test_your_rank_turns_the_decision_into_an_action(cfg, monkeypatch):
    cfg["gsc"] = {"site": "sc-domain:mysite.com"}
    asked = []

    def positions(c, phrases, today, days):
        asked.append((phrases, days))
        return {"ai image detector": {"position": 14.2, "impressions": 120, "clicks": 3, "page": "https://mysite.com/detector"}}
    monkeypatch.setattr(yours.gsc, "positions", positions)
    page = lambda *hosts: {"results": [{"domain": h, "url": f"https://{h}/x", "position": i + 1} for i, h in enumerate(hosts)]}  # noqa: E731
    on_top = {"phrase": "synthid detector", "competition": page("a.com", "blog.mysite.com")}
    lower = {"phrase": "AI image detector"}                                      # not page-one checked: Search Console alone
    absent = {"phrase": "photoshop alternative", "competition": page("a.com", "b.com")}
    assert yours.assess([on_top, lower, absent], cfg, TODAY) == (0, [])
    assert asked == [(["synthid detector", "AI image detector", "photoshop alternative"], 28)]
    assert on_top["yours"] == {"serp": {"position": 2, "url": "https://blog.mysite.com/x"}, "console": None, "action": "update"}
    assert lower["yours"]["action"] == "improve" and absent["yours"]["action"] == "new"
    assert yours.action(None, {"position": 89.0, "impressions": 1}) == "new"                  # far down: not the page to build on
    assert yours.action(None, {"position": 6.0, "impressions": 0}) == "update"
    assert report._yours(on_top["yours"]) == " · you #2 → update yours"
    assert report._yours(lower["yours"]) == " · you ~14.2 (GSC) → improve yours" and report._yours(absent["yours"]) == " → new page"
    # Search Console failing: the page-one rows still read, the unread row gets nothing
    monkeypatch.setattr(yours.gsc, "positions", lambda *a: (_ for _ in ()).throw(RuntimeError("no Search Console token")))
    rows = [{"phrase": "synthid detector", "competition": page("a.com")}, {"phrase": "unread"}]
    assert yours.assess(rows, cfg, TODAY) == (0, ["Search Console positions: no Search Console token"])
    assert rows[0]["yours"]["action"] == "new" and "yours" not in rows[1]
    i18n.set_lang("zh")
    assert report._yours(lower["yours"]) == " · 你约第 14.2(GSC) → 改进现有页"


def test_search_console_positions_ask_once_for_the_exact_queries(cfg, monkeypatch):
    cfg["gsc"] = {"site": "sc-domain:mysite.com"}
    seen = {}

    def query(access, site, start, end, dimensions, fresh, filters=None):
        seen.update(site=site, days=(end - start).days + 1, dimensions=dimensions, filters=filters)
        return [{"query": "c++ tool", "page": "https://mysite.com/a", "impressions": 5, "clicks": 0, "position": 30.0},
                {"query": "c++ tool", "page": "https://mysite.com/b", "impressions": 40, "clicks": 2, "position": 8.5}]
    monkeypatch.setattr(gsc, "_token", lambda c: "token")
    monkeypatch.setattr(gsc, "_query", query)
    got = gsc.positions(cfg, ["C++ tool", "ai image detector"], TODAY, 28)
    assert got == {"c++ tool": {"position": 8.5, "page": "https://mysite.com/b", "impressions": 45, "clicks": 2}}
    assert seen["filters"] == [{"dimension": "query", "operator": "includingRegex", "expression": r"^(c\+\+ tool|ai image detector)$"}]
    assert seen["days"] == 28 and seen["dimensions"] == ["query", "page"] and seen["site"] == "sc-domain:mysite.com"


def test_page_one_keeps_forum_threads_and_marks_what_takes_clicks(cfg, tmp_path, monkeypatch):
    cfg["competition"]["max_rows"] = 8
    items = [{"type": "ai_overview"}, {"type": "people_also_ask"},
             {"type": "organic", "domain": "www.reddit.com", "url": "https://www.reddit.com/r/x/1", "title": "Best PDF editor?", "rank_group": 1},
             {"type": "organic", "domain": "tool.com", "url": "https://tool.com/", "title": "Tool", "rank_group": 2},
             {"type": "discussions_and_forums", "items": [
                 {"url": "https://www.reddit.com/r/x/1", "title": "Best PDF editor?", "domain": "www.reddit.com"},     # the same thread
                 {"url": "https://www.quora.com/q", "title": "Which PDF editor", "domain": "www.quora.com"}, {"title": "no url"}]}]

    def call(calls):
        if calls[0]["call_id"] == "serp":
            return {"serp": {"tasks": [{"result": [{"items": items}]}]}}, 2000
        return {"kd": {"tasks": [{"result": [{"items": []}]}]}}, 12000
    monkeypatch.setattr(competition.aisa, "call", call)
    rows = [{"phrase": "pdf editor", "decision": "watch"}, {"phrase": "best pdf editor", "decision": "watch"}]
    competition.assess(rows, cfg, tmp_path)
    comp = rows[0]["competition"]
    assert [f["url"] for f in comp["forums"]] == ["https://www.reddit.com/r/x/1", "https://www.quora.com/q"]
    assert comp["features"] == ["ai_overview", "discussions_and_forums", "people_also_ask"]
    assert report._page_one(comp).endswith("· AI overview · forums")
    lines = "\n".join(report._thread_lines(rows))
    assert "half as often" in lines and "Pew Research Center" in lines
    first, second = [x for x in lines.splitlines() if x.startswith("- **")]
    assert first.startswith("- **best pdf editor** (a best-of search") and "[Best PDF editor?](https://www.reddit.com/r/x/1)" in second
    assert report._page_one({"verdict": "open", "features": ["video", "featured_snippet"]}) == "open · answer box · videos"


def test_dot_com_asks_the_registry(cfg, monkeypatch):
    assert dotcom.names("synthid detector") == ["synthiddetector.com", "synthid-detector.com"]
    assert dotcom.names("PhotoCraft") == ["photocraft.com"] and dotcom.names("café ai") == []
    answers = {"synthiddetector.com": (404, {}), "photocraft.com": (500, {}),
               "synthid-detector.com": (200, {"events": [{"eventAction": "expiration", "eventDate": "2027-09-30T00:00:00Z"},
                                                         {"eventAction": "registration", "eventDate": "2026-09-30T12:00:00Z"}]})}
    monkeypatch.setattr(dotcom, "_get", lambda url: answers[url.rsplit("/", 1)[1]])
    cfg["dot_com"].update(enabled=True, max_rows=2)
    rows = [{"phrase": "ai image detector", "kind": "generic"}, {"phrase": "synthid detector", "kind": "name"},
            {"phrase": "PhotoCraft", "kind": "name"}]
    cost, errors = dotcom.assess(rows, cfg)
    assert cost == 0 and errors == ["photocraft.com (.com): RDAP answered 500"] and "dot_com" not in rows[0]   # names first, two rows
    assert rows[1]["dot_com"] == [{"domain": "synthiddetector.com", "taken": False},
                                  {"domain": "synthid-detector.com", "taken": True, "registered": "2026-09-30"}]
    text = "\n".join(report._more_lines(cfg, {"rows": rows}))
    assert "- **synthid detector**: **synthiddetector.com not registered** · synthid-detector.com taken (2026-09-30)" in text
    assert "sell them as premium" in text
    cfg["dot_com"]["enabled"] = False
    assert dotcom.assess(rows, cfg) == (0, [])


CHATGPT = {"brand_entities": [{"title": "AI assistant", "markdown": "ChatGPT"}, {"title": "Quizlet", "markdown": "**Quizlet**"},
                              {"title": "Quizlet", "markdown": "Quizlet"}],
           "sources": [{"domain": "www.quizlet.com"}, {"domain": "app.mysite.com"}, {"domain": None}]}
AI_MODE = {"items": [{"type": "ai_overview", "references": [{"source": "NoteGPT", "domain": "notegpt.io"}],
                      "markdown": "Yes, several tools do.[[1]](https://notegpt.io/a)\n\nOptions:\n\n"
                                  "- **[NoteGPT AI PPT Summarizer](https://notegpt.io/ppt-summary)** : extracts text.\n"
                                  "* **Knowt** : flashcards too.\n1. **`SlidesAI`**: also.\n\n"
                                  "Opinions on **Reddit** are mixed. Prefer a **free web tool**?",
                      "items": [{"references": [{"source": "Knowt", "domain": "knowt.com"}, {"source": "NoteGPT", "domain": "notegpt.io"}]},
                                {"references": None}]}]}


def test_ai_answers_name_tools_and_cite_sites(cfg, monkeypatch):
    rows = [{"phrase": "convert slides to notes", "posts": [], "subreddits": []}, {"phrase": "add b roll", "posts": [], "subreddits": []}]
    assert ai_answers.assess(rows, cfg) == (0, []) and "ai" not in rows[0]                      # off by default: nothing bought
    cfg["ai_answers"].update(enabled=True, engines=["chatgpt", "google", "bing"], max_rows=1)
    cfg["yours"]["domains"] = ["mysite.com"]
    asked = []

    def rest(path, task):
        asked.append((path, task))
        return (CHATGPT if "chat_gpt" in path else AI_MODE), 4000
    monkeypatch.setattr(ai_answers.aisa, "rest", rest)
    cost, errors = ai_answers.assess(rows, cfg)
    assert cost == 8000 and errors == ["unknown engine: bing"] and "ai" not in rows[1]
    assert sorted(p for p, _ in asked) == sorted(ai_answers.PATHS.values())
    assert asked[0][1] == {"keyword": "What is the best tool to convert slides to notes?", "location_code": 2840, "language_code": "en"}
    ai = rows[0]["ai"]
    assert ai["by"]["chatgpt"] == {"names": ["ChatGPT", "Quizlet"], "sites": ["quizlet.com", "app.mysite.com"]} and ai["yours"]
    assert ai["by"]["google"] == {"names": ["NoteGPT AI PPT Summarizer", "Knowt", "SlidesAI"], "sites": ["notegpt.io", "knowt.com"]}
    assert report._ai_cell(ai) == "ChatGPT, Quizlet, NoteGPT AI PPT Summarizer +2 ✅" and report._ai_cell(None) == "—"
    assert report._ai_cell({"by": {"chatgpt": {"names": [], "sites": []}}}) == "no tool named"
    lines = "\n".join(report._ai_lines(rows, cfg))
    assert "- **convert slides to notes**: ChatGPT names ChatGPT, Quizlet; cites quizlet.com, app.mysite.com" in lines
    assert "Google AI Mode names NoteGPT AI PPT Summarizer, Knowt, SlidesAI; cites notegpt.io, knowt.com · ✅ cites your site" in lines


def test_ai_answers_show_in_the_asks_table(cfg, monkeypatch):
    post = {"title": "Any AI that turns slides into notes?", "url": "https://reddit.com/r/college/1", "score": 35}
    ak = {"read": 3, "asks": 1, "unsized": 0, "quiet": [], "errors": [],
          "rows": [{"phrase": "convert slides to notes", "posts": [post], "score": 35, "subreddits": ["college"], "volume": 880,
                    "ai": {"prompt": "x", "by": {"chatgpt": {"names": [], "sites": ["quizlet.com"]}}, "yours": False}}]}
    text = "\n".join(report._asks_lines(ak, cfg))
    assert "| Top post | AI answers name |" in text and "|---|---|---|---|---|---|---|" in text
    assert text.count("| no tool named |") == 1 and "ChatGPT names no tool named; cites quizlet.com" in text
    del ak["rows"][0]["ai"]
    assert "AI answers" not in "\n".join(report._asks_lines(ak, cfg))                        # the column only when it was read


def test_keyword_table_shows_the_year_your_rank_threads_and_the_dot_com(cfg, tmp_path, monkeypatch):
    cfg["competition"]["max_rows"] = 8
    cfg["dot_com"]["enabled"] = True
    cfg["yours"]["domains"] = ["mysite.com"]
    _fake_usecase_network(monkeypatch)
    monkeypatch.setattr(usecases, "volumes", lambda phrases, loc: (
        {"ai image detector": {"volume": 60000, "month": "2026-08", "months": _months([100] * 6 + [120, 140, 160, 180, 200, 220])},
         "photoshop alternative": {"volume": 6000, "month": "2026-08", "months": _months([300] * 12)}}, 90000))
    pages = {"synthid detector": [("mysite.com", "https://mysite.com/synthid", "SynthID detector"), ("reddit.com", "https://reddit.com/r/a/1", "Is SynthID real?")],
             "ai image detector": [("big.com", "https://big.com/ai", "AI image detector")]}

    def call(calls):
        c = calls[0]
        if c["call_id"] == "serp":
            return {"serp": _serp(pages[c["arguments"]["body"][0]["keyword"]])}, 2000
        return {"kd": {"tasks": [{"result": [{"items": [{"keyword": k, "keyword_properties": {"keyword_difficulty": 25}}
                                                        for k in c["arguments"]["body"][0]["keywords"]]}]}]}}, 12000
    monkeypatch.setattr(usecases.aisa, "call", call)
    monkeypatch.setattr(dotcom, "_get", lambda url: (404, {}))
    uc = usecases.run(cfg, tmp_path, ["synthid detector"], {}, TODAY)
    rows = {r["phrase"]: r for r in uc["rows"]}
    assert rows["synthid detector"]["yours"]["action"] == "update" and rows["ai image detector"]["yours"]["action"] == "new"
    assert "yours" not in rows["photoshop alternative"] and rows["photoshop alternative"]["year"]["label"] == "steady"   # skip rows: the free curve only
    assert [d["domain"] for d in rows["synthid detector"]["dot_com"]] == ["synthiddetector.com", "synthid-detector.com"]
    assert "dot_com" not in rows["ai image detector"] and uc["extra_errors"] == []
    text = "\n".join(report.keyword_table(cfg, uc))
    assert "| ▄▄▄▄▄▄▅▅▆▇▇█ rising, +120% in 6 mo |" in text and "| ████████████ steady |" in text   # on the term's own scale, from zero
    assert "| **prioritize** · you #1 → update yours |" in text and "| watch → new page |" in text
    assert "- **synthid detector**: #1 on page one: https://mysite.com/synthid" in text and "Your sites: mysite.com." in text
    assert "[Is SynthID real?](https://reddit.com/r/a/1)" in text and "**synthiddetector.com not registered**" in text
    assert "12 months = Google Ads monthly searches" in text
    i18n.set_lang("zh")
    zh = "\n".join(report.keyword_table(cfg, uc))
    assert "| 现在 | 近 12 个月 | 首次有搜索 |" in zh and "| **优先** · 你在第 1 → 更新现有页 |" in zh and "| 观察 → 新建页面 |" in zh
    assert "上升,6 个月 +120%" in zh and "首页上的论坛帖" in zh and "**synthiddetector.com 未注册**" in zh
