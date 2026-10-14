"""The APIsRouter backend reshapes real answers (trimmed, fixtures/apisrouter) into what the
radar's AIsa readers expect. No network: buy() is replaced."""
import json
from datetime import date
from pathlib import Path

import pytest

from radar import aisa, apisrouter, competition, review, social, usecases

FIX = Path(__file__).parent / "fixtures" / "apisrouter"


@pytest.fixture
def bought(monkeypatch):
    seen = []

    def fake(ref, inp):
        seen.append((ref, inp))
        name = {"twitter-user-post-tweet.read.v1": "x-user", "twitter-search-timeline.search.v1": "x-search",
                "reddit-dynamic-search.search.v1": "reddit", "youtube-video-comments.read.v1": "yt",
                "search.search-trends.interest-history.v1": "trends", "search.google.result-page.v1": "serp",
                "search.google-organic.keyword-difficulty.v1": "kd"}[ref]
        return json.loads((FIX / f"{name}.json").read_text()), 2500
    monkeypatch.setattr(apisrouter, "buy", fake)
    monkeypatch.setenv("RADAR_BACKEND", "apisrouter")
    return seen


def test_user_timeline_reads_as_aisa(bought):
    data, cost = aisa.call([{"call_id": "OpenAI", "tool": "get_twitter_user_last_tweets", "arguments": {"userName": "OpenAI"}}])
    tweets = data["OpenAI"]["data"]["tweets"]
    assert tweets and cost == 2500
    assert tweets[0]["text"] and tweets[0]["url"].startswith("https://x.com/OpenAI/status/")
    assert tweets[0]["createdAt"].endswith("2026")      # the format sources._posted parses


def test_search_tweets_have_author_and_likes(bought):
    data, _ = aisa.call([{"call_id": "s", "tool": usecases.SEARCH_TOOL, "arguments": {"query": '"seedance"', "queryType": "Top"}}])
    t = data["s"]["tweets"][0]
    assert t["author"]["userName"] == "Scenario_gg" and t["likeCount"] == 91


def test_reddit_posts_filtered_to_window(bought):
    posts, _ = social.reddit("is there an AI tool for youtube", time="all")
    assert posts and posts[0]["url"].startswith("https://www.reddit.com/r/")
    assert all(p["title"] for p in posts)
    assert posts == sorted(posts, key=lambda p: -p["score"])
    week, _ = social.reddit("is there an AI tool for youtube", time="hour")
    assert week == []          # the sample is weeks old


def test_youtube_like_counts_are_ints(bought):
    rows, _ = social.youtube_comments("LuIL5JATZsc")
    assert rows and isinstance(rows[0]["likes"], int) and rows[0]["text"]


def test_counts_with_suffixes():
    assert apisrouter._count("1.2K") == 1200 and apisrouter._count("3M") == 3_000_000 and apisrouter._count(None) == 0


def test_trends_trimmed_to_window(bought):
    window = {"date_from": "2026-09-01", "date_to": "2026-10-14"}
    data, _ = aisa.call([{"call_id": "p", "tool": review.TOOL, "arguments": {"body": [{"keywords": ["capcut", "gpts"], **window}]}}])
    pairs = usecases._pairs(data["p"]["tasks"][0])
    assert pairs and pairs[0][0] >= date(2026, 9, 1)
    assert bought[0][1]["time_range"] in ("past_30_days", "past_90_days", "past_12_months")


def test_serp_and_difficulty(bought, monkeypatch):
    page, _ = competition.page_one("ai video editor", 2840)
    assert page["results"] and page["results"][0]["domain"] == "invideo.io"
    kd, _ = competition.difficulty(["ai video editor", "youtube script generator"], 2840)
    assert kd["ai video editor"] == 47


def test_unmapped_tool_fails_like_a_batch(bought):
    with pytest.raises(aisa.AisaError):
        aisa.call([{"call_id": "w", "tool": "post_dataforseo_domains_whois_overview_live", "arguments": {}}])
