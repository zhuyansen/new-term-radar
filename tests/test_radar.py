"""The radar's pure logic: name extraction, signal scoring, the store, the judge's parsing,
the review's verdict, the reports. Network calls are not exercised here."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from radar import __main__ as cli
from radar import breakout, config, gsc, i18n, judge, new_sites, notify, report, review, sitemaps, store
from radar.sources import base_name, novel_names
from radar.terms import names

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
    answer = {"tasks": [{"result": [{"items": json.loads(FIXTURE.read_text())}]}]}
    monkeypatch.setattr(new_sites.aisa, "call_all", lambda calls: ({c["call_id"]: answer for c in calls}, 127000))
    out = new_sites.new_sites(cfg, tmp_path, date(2026, 10, 6))
    domains = [r["domain"] for r in out["sites"]]
    assert "ai-hailuo.com" in domains and "neonwin386.ai" not in domains and "oyo88.ai" not in domains
    assert all(r["new"] for r in out["sites"]) and "hailuo" in [t["term"] for t in out["terms"]]
    again = new_sites.new_sites(cfg, tmp_path, date(2026, 10, 13))
    assert not any(r["new"] for r in again["sites"]) and again["terms"] == []


def test_new_sites_query_needs_both_dates(cfg):
    body = new_sites._calls(cfg["new_sites"], date(2026, 10, 6))[0]["arguments"]["body"][0]
    fields = [f[0] for f in body["filters"] if isinstance(f, list)]
    assert fields[:2] == ["created_datetime", "first_seen"] and body["filters"][0][2].startswith("2026-07-08")


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
    monkeypatch.setattr(review.data_backend.aisa, "call_all", lambda calls: ({c["call_id"]: answer for c in calls}, 1200))
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
    monkeypatch.setattr(usecases, "evidence", lambda names, repos, today: (
        {n: {"posts": [{"text": "check if any image is AI generated", "likes": 900, "by": "a", "url": ""}], "readme": "", "repo": ""} for n in names}, 3000))
    monkeypatch.setattr(usecases, "propose", lambda model, name, ev: [
        {"phrase": "ai image detector", "evidence": "check if any image is AI generated", "brand": False},
        {"phrase": "photoshop alternative", "evidence": "free clone", "brand": True},
        {"phrase": "nobody searches this", "evidence": "x", "brand": False}])
    monkeypatch.setattr(usecases, "screen", lambda model, name, ev, cands: cands)
    monkeypatch.setattr(usecases, "volumes", lambda phrases, loc, cfg=None: (
        {"ai image detector": {"volume": 60000, "month": "2026-08"}, "photoshop alternative": {"volume": 6000, "month": "2026-08"},
         "nobody searches this": {"volume": 0, "month": ""}}, 90000))
    monkeypatch.setattr(usecases, "fetch_series", lambda keywords, bench, today, cfg=None: (
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
    monkeypatch.setattr(review.data_backend.aisa, "call_all", lambda calls: ({c["call_id"]: answer for c in calls}, 1200))
    assert review.review(cfg, conn, TODAY)["checked"][0]["verdict"] == "miss"


def test_screen_drops_loosely_related_keywords_and_keeps_all_when_the_call_fails(monkeypatch):
    cands = [{"phrase": "ai image detector"}, {"phrase": "chatgpt alternative"}, {"phrase": "ai audio detector"}]
    ev = {"posts": [{"text": "check if any image is AI generated", "likes": 900, "by": "a"}], "readme": ""}
    monkeypatch.setattr(usecases.aisa, "chat", lambda *a, **k: '[{"i": 0, "score": 0.95}, {"i": 1, "score": 0.2}, {"i": 2, "score": 0.7}]')
    kept = usecases.screen("m", "synthid detector", ev, cands)
    assert [(c["phrase"], c["relevance"]) for c in kept] == [("ai image detector", 0.95), ("ai audio detector", 0.7)]

    def down(*a, **k):
        raise usecases.aisa.AisaError("HTTP 500")
    monkeypatch.setattr(usecases.aisa, "chat", down)
    assert usecases.screen("m", "synthid detector", ev, cands) == cands


def test_momentum_is_the_last_settled_week_against_the_week_before():
    assert usecases.shape(_daily([10] * 76 + [20] * 7 + [40] * 7 + [0, 100]), TODAY)["momentum"] == 2.0
    assert usecases.shape(_daily([0] * 83 + [40] * 7 + [0, 100]), TODAY)["momentum"] is None   # nothing the week before
