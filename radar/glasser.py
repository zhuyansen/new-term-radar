"""Optional DataForSEO backend through Glasser's Run API (standard library only).

Only Google Trends and Google Ads volume are mapped. Endpoint contracts and limits
are documented in docs/glasser.md. No POST is retried and no other provider is used
as fallback. Async Runs are polled by id, never re-created.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from . import UA

BASE = "https://api.glasser.ai/v1/runs"
TIMEOUT = 90
MAX_POLLS = 30
POLL_SECONDS = 2
ENDPOINTS = {
    "trends": ("post_dataforseo_keywords_trends_explore_live",
               "/v3/keywords_data/google_trends/explore/live", 4),
    "volume": ("post_dataforseo_keywords_gads_search_volume_live",
               "/v3/keywords_data/google_ads/search_volume/live", 2),
}


class GlasserError(RuntimeError):
    """A failed or unverified Run; known charges may precede the failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GlasserError("Glasser redirect refused; no credentials forwarded")


_OPENER = urllib.request.build_opener(_NoRedirect())


def settings(options: dict) -> tuple[int, str]:
    allowed = {"trends_location_code", "trends_language_code"}
    if not isinstance(options, dict) or options.keys() - allowed:
        raise ValueError("glasser accepts only trends_location_code and trends_language_code")
    location = options.get("trends_location_code", 2840)
    language = options.get("trends_language_code", "en")
    if type(location) is not int or not 1 <= location <= 99999999:
        raise ValueError("glasser.trends_location_code must be a positive location code")
    if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}", language):
        raise ValueError("glasser.trends_language_code must be a two-letter language code")
    return location, language


def _request(url: str, key: str, body: dict | None = None, request_id: str = "", timeout: float = TIMEOUT) -> dict:
    headers = {"Authorization": f"Bearer {key}", "User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        headers.update({"Content-Type": "application/json", "Idempotency-Key": request_id})
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method="POST" if body is not None else "GET")
    try:
        with _OPENER.open(req, timeout=timeout) as res:
            answer = json.load(res)
    except urllib.error.HTTPError as exc:
        # Never copy provider bodies or request headers into reports (they may echo secrets).
        raise GlasserError(f"Glasser HTTP {exc.code}; no retry") from None
    except (OSError, ValueError) as exc:
        raise GlasserError(f"Glasser {type(exc).__name__}; result unknown, no retry") from None
    if not isinstance(answer, dict) or "error" in answer:
        raise GlasserError("Glasser returned an invalid Run or API error; no retry")
    return answer


def _run(endpoint: str, version: int, payload: dict, key: str) -> tuple[dict, int]:
    request_id = str(uuid.uuid4())
    context = f"idempotency={request_id}"
    try:
        run = _request(BASE, key, {"provider": "dataforseo", "endpoint": endpoint,
                                 "endpoint_version": version, "input": payload}, request_id)
        run_id = run.get("id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise GlasserError("missing or invalid Run id")
        context = f"run={run_id}"
        deadline = time.monotonic() + TIMEOUT
        for _ in range(MAX_POLLS):
            if run.get("status") not in ("QUEUED", "RUNNING"):
                break
            remaining = deadline - time.monotonic()
            if remaining <= POLL_SECONDS:
                break
            time.sleep(POLL_SECONDS)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            run = _request(BASE + "/" + urllib.parse.quote(run_id, safe=""), key, timeout=remaining)
            if run.get("id") != run_id:
                raise GlasserError("poll returned a different Run")
        status = run.get("status")
        if status in ("QUEUED", "RUNNING"):
            raise GlasserError("still pending; charge unknown; inspect this Run before rerunning")
        if status != "COMPLETED":
            state = status if status in ("FAILED", "STOPPED") else "unknown status"
            raise GlasserError(f"Run did not complete successfully ({state}); inspect its charge")
        try:
            if not isinstance(run["charge_usd"], str):
                raise ValueError
            charge = Decimal(run["charge_usd"])
            if not charge.is_finite() or charge < 0:
                raise InvalidOperation
            cost = int((charge * 1_000_000).to_integral_value())
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise GlasserError("invalid or missing charge_usd") from None
        context += f" charge_usd={charge}"
        response = run.get("provider_response")
        if (not isinstance(response, dict) or type(response.get("http_status")) is not int
                or not 200 <= response["http_status"] < 300 or response.get("error")):
            raise GlasserError("provider HTTP error or missing provider status")
        output = run.get("output")
        _check_output(output)
        return output, cost
    except GlasserError as exc:
        raise GlasserError(f"{context}: {exc}") from None


def _check_output(output: dict) -> None:
    """A COMPLETED Run can contain an HTTP-200 DataForSEO task failure."""
    if not isinstance(output, dict) or output.get("status_code") != 20000 or output.get("tasks_error", 0):
        raise GlasserError("DataForSEO response failed or has no successful status")
    tasks = output.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        raise GlasserError("expected one DataForSEO task")
    task = tasks[0]
    if task.get("status_code") != 20000:
        raise GlasserError("DataForSEO task failed; not an empty result")
    if "result" not in task or (task["result"] is not None and not isinstance(task["result"], list)):
        raise GlasserError("invalid DataForSEO result")


def _input(call: dict, capability: str, options: dict) -> tuple[str, int, dict]:
    tool, endpoint, version = ENDPOINTS[capability]
    if call.get("tool") != tool:
        raise GlasserError(f"unsupported tool for Glasser {capability}")
    args = call.get("arguments", {})
    tasks = args.get("body") if isinstance(args, dict) else None
    if (not isinstance(args, dict) or set(args) != {"body"} or not isinstance(tasks, list) or len(tasks) != 1
            or not isinstance(tasks[0], dict)):
        raise GlasserError("Glasser requires exactly one DataForSEO task per call")
    payload = dict(tasks[0])  # Glasser takes a task object, not the AIsa body array.
    allowed = {"keywords", "date_from", "date_to"} if capability == "trends" else {
        "keywords", "location_code", "language_code"}
    if payload.keys() - allowed:
        raise GlasserError("unsupported Glasser input fields; no fields were dropped")
    keywords = payload.get("keywords")
    limit, max_length = (5, 2048) if capability == "trends" else (100, 700)
    if (not isinstance(keywords, list) or not 1 <= len(keywords) <= limit
            or any(not isinstance(k, str) or not 1 <= len(k) <= max_length for k in keywords)):
        raise GlasserError(f"Glasser {capability} requires 1..{limit} non-empty keywords (max {max_length} characters)")
    if capability == "trends":
        try:
            start, end = (date.fromisoformat(payload[k]) for k in ("date_from", "date_to"))
            if start > end:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise GlasserError("Glasser Trends needs a valid date_from/date_to window") from None
        location, language = settings(options)
        payload.update(location_code=location, language_code=language, item_types=["google_trends_graph"])
    else:
        location, language = payload.get("location_code"), payload.get("language_code")
        if type(location) is not int or not 1 <= location <= 99999999:
            raise GlasserError("Glasser volume needs location_code in 1..99999999")
        if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}", language):
            raise GlasserError("Glasser volume needs a two-letter language_code")
        if any(re.search(r"(?:site|inurl|intitle|allintitle|allinurl|filetype|related|cache):", k) for k in keywords):
            raise GlasserError("Glasser volume does not accept search operators")
    return endpoint, version, payload


def _normalise(output: dict, capability: str, payload: dict) -> dict:
    """Validate the fields consumed by the radar; never turn malformed data into zero."""
    result = output["tasks"][0]["result"]
    if not result:  # A successful, explicit empty result is different from a failed task.
        return output
    try:
        if capability == "trends":
            if len(result) != 1:
                raise ValueError
            graphs = [item for item in result[0]["items"] if item["type"] == "google_trends_graph"]
            if len(graphs) != 1 or graphs[0]["keywords"] != payload["keywords"]:
                raise ValueError
            points = graphs[0]["data"]
            if not isinstance(points, list):
                raise ValueError
            previous = None
            for p in points:
                day = date.fromisoformat(p["date_from"])
                # The radar's day/week calculations require daily data, in keyword order.
                if p["date_to"] != p["date_from"] or (previous is not None and (day - previous).days != 1):
                    raise ValueError
                if p.get("missing_data") or not isinstance(p["values"], list) or len(p["values"]) != len(payload["keywords"]):
                    raise ValueError
                if any(type(v) is not int or not 0 <= v <= 100 for v in p["values"]):
                    raise ValueError
                previous = day
            result[0]["items"] = graphs  # Existing consumers expect the graph as the first item.
        else:
            for row in result:
                if not isinstance(row["keyword"], str) or "search_volume" not in row:
                    raise ValueError
                volume = row["search_volume"]
                if volume is not None and (type(volume) is not int or volume < 0):
                    raise ValueError
                for month in row.get("monthly_searches") or []:
                    date(month["year"], month["month"], 1)
    except (KeyError, TypeError, ValueError):
        detail = "; expected contiguous daily, non-missing values in keyword order" if capability == "trends" else ""
        raise GlasserError(f"invalid {capability} data{detail}") from None
    return output


def call_all(calls: list[dict], capability: str, options: dict | None = None) -> tuple[dict[str, dict], int]:
    options = options or {}
    settings(options)
    if capability not in ENDPOINTS:
        raise GlasserError(f"Glasser does not support {capability}")
    # Validate the entire batch before any billable call. In particular, do not
    # silently split volume requests over the documented 100-keyword limit.
    prepared = [_input(c, capability, options) for c in calls]
    ids = [c.get("call_id") for c in calls]
    if any(not isinstance(cid, str) or not cid for cid in ids) or len(set(ids)) != len(ids):
        raise GlasserError("call_id values must be unique non-empty strings")
    if not calls:
        return {}, 0
    key = os.environ.get("GLASSER_API_KEY", "").strip()
    if not key:
        raise GlasserError("no Glasser key: set GLASSER_API_KEY")
    out, cost = {}, 0
    try:
        for cid, (endpoint, version, payload) in zip(ids, prepared):
            output, charge = _run(endpoint, version, payload, key)
            cost += charge
            out[cid] = _normalise(output, capability, payload)
    except GlasserError as exc:
        raise GlasserError(f"{exc}; known completed charges before this error: {cost} micro-USD") from None
    return out, cost
