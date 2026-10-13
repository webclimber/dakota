"""Source handling shared by the monitor compiler and the recurring research run.

- fetch_page():      fetch one URL and say plainly why it could not be used (paywall, blocked, ...)
- extract_facts():   pull the dated, topic-relevant facts out of one page (cloud small model)
- plan_sources():    ask a strong model which primary sources publish this topic's data, then
                     validate each one by actually fetching it
- CLI:               python scripts/dakota_sources.py <monitor_spec.json> [--dry-run]
                     writes preferred_sources / discarded_sources / tracked_metrics into the spec
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import trafilatura
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

PLANNER_MODEL = os.getenv("DAKOTA_SOURCE_PLANNER_MODEL", "gpt-5.6-sol")
EXTRACT_MODEL = os.getenv("DAKOTA_EXTRACT_MODEL", "gpt-5.6-luna")
USER_AGENT = "Mozilla/5.0 DakotaResearch/1.0"
MAX_PAGE_CHARS = 60000       # per-source text handed to the extractor
MIN_READABLE_CHARS = 500
STALE_AFTER_DAYS = 60        # a planned source whose newest date is older than this is discarded
USAGE_LOG = ROOT / "logs" / "usage.jsonl"

_PAYWALL_MARKERS = (
    "subscribe to continue", "subscribe to read", "subscribers only", "subscriber-only",
    "already a subscriber", "sign in to read", "log in to read", "log in to continue",
    "create a free account to", "become a member to", "this content is for members",
    "to continue reading", "unlock this article", "start your free trial",
)
_BOT_WALL_MARKERS = (
    "enable javascript", "verify you are human", "checking your browser",
    "access denied", "are you a robot", "captcha", "unusual traffic",
)

_client = None


def _openai() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def _log_usage(kind: str, model: str, usage, note: str = "") -> dict:
    record = {
        "timestamp": datetime.now().isoformat(),
        "kind": kind,
        "provider": "openai",
        "model": model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        "query": note[:200],
    }
    try:
        USAGE_LOG.parent.mkdir(exist_ok=True)
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
    return record


def parse_json_text(raw: str):
    """Parse model output that should be JSON but may be fenced or wrapped in prose."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def json_call(model: str, prompt: str, kind: str, note: str = "", web_search: bool = False):
    """One Responses API call returning (parsed_json, usage_record)."""
    kwargs = {"model": model, "input": prompt}
    if web_search:
        kwargs["tools"] = [{"type": "web_search"}]
    resp = _openai().responses.create(**kwargs)
    usage = _log_usage(kind, model, getattr(resp, "usage", None), note)
    return parse_json_text(resp.output_text), usage


# ── Fetching ───────────────────────────────────────────────────────────────

def fetch_page(url: str, timeout: int = 20) -> dict:
    """Fetch a URL. Returns ok=True with text, or ok=False with a human-readable reason."""
    result = {
        "url": url,
        "final_url": url,
        "domain": urlparse(url).netloc,
        "status_code": None,
        "content_type": "",
        "last_modified": "",
        "text": "",
        "text_len": 0,
        "ok": False,
        "reason": "",
    }
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    except httpx.TimeoutException:
        result["reason"] = "timed out"
        return result
    except Exception as e:
        result["reason"] = f"could not connect ({type(e).__name__})"
        return result

    result["status_code"] = r.status_code
    result["final_url"] = str(r.url)
    result["domain"] = urlparse(str(r.url)).netloc
    result["content_type"] = r.headers.get("content-type", "")
    result["last_modified"] = r.headers.get("last-modified", "")

    if r.status_code in (401, 402):
        result["reason"] = f"paywall or login required (HTTP {r.status_code})"
        return result
    if r.status_code == 403:
        result["reason"] = "blocked automated access (HTTP 403)"
        return result
    if r.status_code in (404, 410):
        result["reason"] = f"page not found (HTTP {r.status_code})"
        return result
    if r.status_code == 429:
        result["reason"] = "rate limited (HTTP 429)"
        return result
    if r.status_code >= 400:
        result["reason"] = f"server error (HTTP {r.status_code})"
        return result

    ctype = result["content_type"].lower()
    if "json" in ctype:
        text = r.text.strip()
    elif "pdf" in ctype or result["final_url"].lower().endswith(".pdf"):
        result["reason"] = "PDF, not readable by the fetcher"
        return result
    else:
        try:
            text = (trafilatura.extract(r.text, url=str(r.url), include_comments=False,
                                        include_tables=True) or "").strip()
        except Exception:
            text = ""

    result["text_len"] = len(text)
    low = text[:3000].lower()
    raw_low = r.text[:20000].lower()
    if len(text) < MIN_READABLE_CHARS:
        if any(m in raw_low for m in _BOT_WALL_MARKERS):
            result["reason"] = "bot check or JavaScript-only page"
        elif any(m in raw_low for m in _PAYWALL_MARKERS):
            result["reason"] = "paywall"
        else:
            result["reason"] = "no readable text (page needs JavaScript or a login)"
        return result
    if len(text) < 2500 and any(m in low for m in _PAYWALL_MARKERS):
        result["reason"] = "paywall (only a teaser is public)"
        return result

    result["text"] = text[:MAX_PAGE_CHARS]
    result["ok"] = True
    return result


# ── Per-source fact extraction ─────────────────────────────────────────────

def _fmt_metrics(tracked_metrics: list) -> str:
    if not tracked_metrics:
        return "(none defined)"
    return "\n".join(f"- {m.get('name')}: {m.get('description', '')}".rstrip(": ") for m in tracked_metrics)


def extract_facts(page: dict, spec: dict, title: str = "", today: str | None = None) -> dict:
    """Extract dated, topic-relevant facts from one fetched page.

    Returns {"relevant": bool, "latest_date": str|None, "facts": [{"fact","date"}],
             "metrics": [{"name","value","as_of"}], "usage": {...}} — or an "error" key."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    prompt = f"""You extract facts from ONE web page for a recurring monitor. Today is {today}.

Monitor request:
{spec.get("user_request", "")}

Watch axes:
{json.dumps(spec.get("watch_axes") or [], ensure_ascii=False)}

Tracked metrics (report a value only if this page states it):
{_fmt_metrics(spec.get("tracked_metrics") or [])}

Page title: {title}
Page URL: {page.get("final_url")}
Last-Modified header: {page.get("last_modified") or "unknown"}

Rules:
- Use ONLY what this page says. Never add outside knowledge, never guess, never fix what looks wrong.
- Copy names, numbers, ratings and dates exactly as written.
- Prefer the newest material on the page. Skip background that is not specific to the request.
- Each fact is one self-contained sentence including who/what/where and the number or rating.
- "date" is the date the page gives for that fact (YYYY-MM-DD), or null if it gives none.
- "latest_date" is the most recent date of any kind you can find on the page (publication, "updated",
  poll field date), or null.
- "relevant" is false if the page has nothing specific to the request.
- At most 25 facts.

Return JSON only:
{{"relevant": true, "latest_date": "YYYY-MM-DD", "facts": [{{"fact": "...", "date": "YYYY-MM-DD"}}],
  "metrics": [{{"name": "<exact tracked metric name>", "value": "...", "as_of": "YYYY-MM-DD"}}]}}

PAGE TEXT:
{page.get("text", "")}
"""
    data, err = None, ""
    for _attempt in range(2):  # the small model occasionally returns malformed JSON; one retry
        try:
            data, usage = json_call(EXTRACT_MODEL, prompt, "openai_monitor_extract", page.get("final_url", ""))
            break
        except Exception as e:
            err = str(e)[:200]
    if data is None:
        return {"relevant": False, "latest_date": None, "facts": [], "metrics": [], "error": err}
    latest = data.get("latest_date") or None
    if latest and str(latest)[:10] > today:  # a scheduled future date is not a freshness signal
        latest = None
    return {
        "relevant": bool(data.get("relevant")),
        "latest_date": latest,
        "facts": [f for f in (data.get("facts") or []) if isinstance(f, dict) and f.get("fact")][:25],
        "metrics": [m for m in (data.get("metrics") or []) if isinstance(m, dict) and m.get("name")],
        "usage": usage,
    }


def days_old(date_str: str | None, today: datetime | None = None) -> int | None:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
    except Exception:
        return None
    return ((today or datetime.now()) - d).days


def fetch_and_extract(source: dict, spec: dict) -> dict:
    """Fetch + extract one source. Always returns a dict carrying ok/reason for the report."""
    page = fetch_page(source["url"])
    out = {
        "title": source.get("title") or page["domain"],
        "url": source["url"],
        "final_url": page["final_url"],
        "domain": page["domain"],
        "text_len": page["text_len"],
        "ok": page["ok"],
        "reason": page["reason"],
        "latest_date": None,
        "facts": [],
        "metrics": [],
        "usage": None,
    }
    if not page["ok"]:
        return out
    ex = extract_facts(page, spec, title=out["title"])
    out["usage"] = ex.get("usage")
    if ex.get("error"):
        out["ok"] = False
        out["reason"] = f"could not be read by the extractor ({ex['error'][:80]})"
        return out
    out["latest_date"] = ex["latest_date"]
    out["facts"] = ex["facts"]
    out["metrics"] = ex["metrics"]
    if not ex["relevant"] or not (ex["facts"] or ex["metrics"]):
        out["ok"] = False
        out["reason"] = "nothing relevant to the monitor on the page"
    return out


def fetch_and_extract_many(sources: list, spec: dict, workers: int = 6) -> list:
    if not sources:
        return []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda s: fetch_and_extract(s, spec), sources))


# ── Source planning (once per monitor) ─────────────────────────────────────

def propose_sources(spec: dict) -> tuple[dict, dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""You are planning the sources for a recurring research monitor. Today is {today}.

Monitor request:
{spec.get("user_request", "")}

Title: {spec.get("title", "")}
Watch axes: {json.dumps(spec.get("watch_axes") or [], ensure_ascii=False)}
Cadence: {spec.get("publish_cadence") or "recurring"} (runs every {spec.get("check_frequency_minutes")} minutes)

Task 1 — sources. Name the 12 to 16 best sources for this request that are updated often enough for
this cadence. Think like a specialist who follows this beat every day. Cover every kind that applies:
- Originators of data: whoever publishes the numbers, ratings, forecasts, rulings, filings or
  official announcements (a ratings table, a tracker, a results page, an API endpoint returning JSON).
- Independent reporting: for anything driven by events and politics, the leading independent news
  outlets on this beat ARE primary sources — their dedicated section or tag page for the topic, in
  every language the reader accepts. Wire services and specialist outlets before general ones.
- Probabilities: if the reader asks about odds or likelihood, include prediction markets and
  forecasters that publish a number for it (for Polymarket use
  https://gamma-api.polymarket.com/public-search?q=<terms>&limit_per_type=5), plus pollsters.
- Independent monitors: NGOs, academic trackers, multilateral bodies.
Balance: where the topic has opposing sides, no side's official channels may make up more than a
quarter of the list, and each side that publishes gets represented. Government and party feeds are
sources for what that side claims, not for what is true.
Use web search to confirm each URL exists today and points at the specific page carrying the
content, not a homepage. Include sources you expect to be paywalled or JavaScript-only if they are
among the best — they will be tested and reported to the reader either way. Do not include
encyclopedias or explainer pages unless they carry a maintained data table nothing else provides.

Task 2 — tracked metrics. Name 3 to 8 values the reader wants to see every issue and compare with the
previous issue — start from what the reader literally asked for. A metric is ONE value or one short status that fits in a table cell (a number, a date, a rating,
a count, a yes/no with a date) — never a topic area or a bundle of things to track.
Each must be something the sources
above actually publish; list those sources' URLs in the metric's source_urls. Name the source in the
metric name when different sources publish different values for the same thing.

Return JSON only:
{{
  "sources": [
    {{"title": "...", "url": "https://...", "provides": "what data this page carries",
      "update_cadence": "daily|weekly|irregular", "access_expectation": "open|paywalled|javascript"}}
  ],
  "tracked_metrics": [
    {{"name": "...", "description": "what it is and which source publishes it", "source_urls": ["https://..."]}}
  ]
}}
"""
    return json_call(PLANNER_MODEL, prompt, "openai_monitor_source_plan", spec.get("monitor_id", ""), web_search=True)


def validate_sources(candidates: list, spec: dict) -> tuple[list, list]:
    """Fetch every candidate. Returns (usable, discarded); discarded entries carry a reason."""
    seen, unique = set(), []
    for c in candidates:
        url = (c.get("url") or "").strip()
        if url.startswith("http") and url not in seen:
            seen.add(url)
            unique.append({**c, "url": url})
    results = fetch_and_extract_many(unique, spec)
    usable, discarded = [], []
    checked = datetime.now().strftime("%Y-%m-%d")
    for cand, res in zip(unique, results):
        entry = {
            "title": cand.get("title") or res["domain"],
            "url": cand["url"],
            "domain": res["domain"] or urlparse(cand["url"]).netloc,
            "provides": cand.get("provides", ""),
            "update_cadence": cand.get("update_cadence", ""),
            "checked_at": checked,
        }
        reason = res["reason"]
        age = days_old(res["latest_date"])
        if res["ok"] and age is not None and age > STALE_AFTER_DAYS:
            reason = f"stale — newest date on the page is {res['latest_date']}"
        if res["ok"] and not reason:
            entry["latest_date_seen"] = res["latest_date"]
            entry["facts_found"] = len(res["facts"])
            usable.append(entry)
        else:
            entry["reason"] = reason or "unusable"
            discarded.append(entry)
    return usable, discarded


def propose_gap_fill(spec: dict, usable: list, discarded: list, metrics: list) -> tuple[dict, dict]:
    """Second planning round: review what survived testing against the request and find replacements."""
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""You are reviewing the source plan for a recurring research monitor after every source was
tested. Today is {today}.

Monitor request:
{spec.get("user_request", "")}

Watch axes: {json.dumps(spec.get("watch_axes") or [], ensure_ascii=False)}

SOURCES THAT WORK (will be read every run):
{json.dumps([{k: s.get(k) for k in ("title", "url", "provides")} for s in usable], ensure_ascii=False, indent=1)}

SOURCES THAT FAILED (cannot be used):
{json.dumps([{k: s.get(k) for k in ("title", "url", "provides", "reason")} for s in discarded], ensure_ascii=False, indent=1)}

TRACKED METRICS so far:
{json.dumps(metrics, ensure_ascii=False, indent=1)}

Judge the working list against the request, as the reader would:
- Is every part of the request covered by at least one working source? Name what is not.
- Is there enough independent, frequently updated reporting, in every language the reader accepts?
- Where the topic has opposing sides, is each side that publishes represented, with no side's
  official channels dominating?
- If the reader asked about probabilities or odds, is there a working source that publishes a number?
- Is each failed source's role now uncovered?

Then find replacements with web search: for each gap, a different page or a different publisher that
carries the same kind of content and is likely to be readable by a plain HTTP fetch (no login, not
JavaScript-only) — for example an RSS/Atom feed, a tag or section page, an official Telegram web
preview (https://t.me/s/<channel>), an API endpoint, or a comparable outlet. Do not repeat any URL
listed above. Up to 10 additions; none if the plan is already complete.

Also revise the tracked metrics so each one can be filled from a working or newly proposed source,
starting from what the reader literally asked for (3 to 8 metrics, each with source_urls).
A metric is ONE value or one short status that fits in a table cell (a number, a date, a rating,
a count, a yes/no with a date) — never a topic area or a bundle of things to track.

Return JSON only:
{{
  "gaps": ["what the working list does not cover"],
  "sources": [
    {{"title": "...", "url": "https://...", "provides": "...", "update_cadence": "daily|weekly|irregular",
      "fills_gap": "..."}}
  ],
  "tracked_metrics": [
    {{"name": "...", "description": "...", "source_urls": ["https://..."]}}
  ]
}}
"""
    return json_call(PLANNER_MODEL, prompt, "openai_monitor_source_gapfill", spec.get("monitor_id", ""), web_search=True)


def plan_sources(spec: dict) -> dict:
    """Propose, test, then review the survivors and fill gaps. Returns the spec fields to merge in."""
    data, _usage = propose_sources(spec)
    candidates = data.get("sources") or []
    metrics = [m for m in (data.get("tracked_metrics") or []) if isinstance(m, dict) and m.get("name")]
    # extraction during validation should already know the metrics it is looking for
    usable, discarded = validate_sources(candidates, {**spec, "tracked_metrics": metrics})

    gaps: list = []
    try:
        review, _usage = propose_gap_fill(spec, usable, discarded, metrics)
        gaps = [g for g in (review.get("gaps") or []) if g]
        revised = [m for m in (review.get("tracked_metrics") or []) if isinstance(m, dict) and m.get("name")]
        if revised:
            metrics = revised
        seen = {s["url"] for s in usable + discarded}
        extra = [c for c in (review.get("sources") or []) if c.get("url") and c["url"] not in seen]
        if extra:
            more_ok, more_bad = validate_sources(extra, {**spec, "tracked_metrics": metrics})
            usable += more_ok
            discarded += more_bad
    except Exception as e:
        print(f"gap-fill round skipped: {e}")

    # A metric whose every source failed validation can never be filled in — drop it
    usable_urls = {s["url"] for s in usable}
    metrics = [m for m in metrics
               if not m.get("source_urls") or any(u in usable_urls for u in m["source_urls"])]
    return {
        "preferred_sources": usable,
        "discarded_sources": discarded,
        "tracked_metrics": metrics,
        "source_plan_gaps": gaps,
        "sources_planned_at": datetime.now().isoformat(timespec="seconds"),
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    if not args:
        print("Usage: python scripts/dakota_sources.py <monitor_spec.json> [--dry-run]")
        return 1
    spec_path = Path(args[0]).expanduser().resolve()
    spec = json.loads(spec_path.read_text())
    plan = plan_sources(spec)

    print(f"== Source plan for {spec.get('monitor_id')} ==")
    print(f"\nUsable ({len(plan['preferred_sources'])}):")
    for s in plan["preferred_sources"]:
        print(f"  + {s['title']} — {s['url']}  (newest date {s.get('latest_date_seen')}, {s.get('facts_found')} facts)")
    print(f"\nDiscarded ({len(plan['discarded_sources'])}):")
    for s in plan["discarded_sources"]:
        print(f"  - {s['title']} — {s['url']}  [{s['reason']}]")
    print(f"\nTracked metrics ({len(plan['tracked_metrics'])}):")
    for m in plan["tracked_metrics"]:
        print(f"  * {m['name']} — {m.get('description', '')}")

    if dry_run:
        print("\n(dry run — spec not modified)")
        return 0
    spec.update(plan)
    spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False))
    print(f"\n== Saved ==\n{spec_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
