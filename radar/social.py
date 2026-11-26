"""Where people ask for things in their own words: Reddit posts and YouTube comments, through Glasser or AIsa.

reddit(): a Reddit search (ScrapeCreators': $0.00188 through Glasser, $0.0019 through AIsa on 2026-10-14), with title, text, score, comments,
subreddit, link and creation time. youtube_comments(): the top-level comments of a video through
TikHub through Glasser or AIsa, $0.001 a page of 20. Both are read for research: quote them, do not republish them.
"""
from __future__ import annotations

from . import backend

REDDIT_TOOL = "get_reddit_search"
COMMENTS_TOOL = "get_tikhub_youtube_web_v2_get_video_comments"


def reddit(query: str, time: str = "week", sort: str = "top", limit: int = 25) -> tuple[list[dict], int]:
    data, cost = backend.call([{"call_id": "r", "tool": REDDIT_TOOL, "arguments": {
        "query": query, "sort": sort, "time": time, "limit": str(limit)}}])
    posts = []
    for p in (data.get("r") or {}).get("posts") or []:
        if p.get("over_18"):
            continue
        posts.append({"id": p.get("id") or p.get("post_id") or "", "title": " ".join(str(p.get("title") or "").split()),
                      "text": " ".join(str(p.get("selftext") or "").split())[:600], "score": int(p.get("score") or 0),
                      "comments": int(p.get("num_comments") or 0), "subreddit": p.get("subreddit") or "",
                      "url": "https://www.reddit.com" + (p.get("permalink") or ""), "created": float(p.get("created_utc") or 0)})
    return posts, cost


def youtube_comments(video_id: str) -> tuple[list[dict], int]:
    data, cost = backend.call([{"call_id": "y", "tool": COMMENTS_TOOL, "arguments": {"video_id": video_id, "need_format": True}}])
    rows = ((data.get("y") or {}).get("data") or {}).get("comments") or []
    return [{"text": " ".join(str(c.get("content") or "").split()), "likes": int(c.get("like_count") or 0)} for c in rows], cost
