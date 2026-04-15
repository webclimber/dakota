import sys
import json
import re
from urllib.parse import urlparse
from email.utils import parsedate_to_datetime

import httpx
import trafilatura
from dotenv import load_dotenv

from pathlib import Path
sys_path = str(Path(__file__).resolve().parent)
if sys_path not in sys.path:
    sys.path.append(sys_path)

from dakota_discovery import discover

load_dotenv()

if len(sys.argv) < 3:
    print('Usage: python scripts/dakota_rank_sources.py <topic> "your research question"')
    sys.exit(1)

topic = sys.argv[1]
query = " ".join(sys.argv[2:])

QUERY_TERMS = {t.lower() for t in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9]+", query) if len(t) >= 4}

def detect_kind(url: str, content_type: str, text: str) -> str:
    u = url.lower()
    ct = (content_type or "").lower()
    if ".pdf" in u or "pdf" in ct:
        return "pdf"
    if len(text) < 400:
        return "thin"
    path = urlparse(url).path.strip("/").split("/")
    if len(path) <= 1:
        return "homepage"
    if any(seg in {"politica", "politics", "world", "americas", "news", "section", "tag", "tags", "category"} for seg in path):
        return "section"
    return "article"

def fetch_source(source: dict) -> dict:
    title = source.get("title", "").strip()
    url = source.get("url", "").strip()
    result = {
        "title": title,
        "url": url,
        "final_url": url,
        "status_code": None,
        "content_type": "",
        "domain": urlparse(url).netloc,
        "text": "",
        "text_len": 0,
        "kind": "unknown",
        "freshness_hint": "",
        "score": 0,
        "notes": [],
        "fetch_ok": False,
    }

    try:
        r = httpx.get(
            url,
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 DakotaResearch/1.0"},
        )
        result["status_code"] = r.status_code
        result["final_url"] = str(r.url)
        result["domain"] = urlparse(str(r.url)).netloc
        result["content_type"] = r.headers.get("content-type", "")
        result["freshness_hint"] = r.headers.get("last-modified", "") or r.headers.get("date", "")
        r.raise_for_status()

        extracted = trafilatura.extract(r.text, url=str(r.url), include_comments=False, include_tables=False)
        text = (extracted or "").strip()
        result["text"] = text[:2000]
        result["text_len"] = len(text)
        result["fetch_ok"] = True
        result["kind"] = detect_kind(str(r.url), result["content_type"], text)
    except Exception as e:
        result["notes"].append(f"fetch failed: {e}")
        return result

    score = 0

    # fetch succeeded
    score += 10

    # text richness
    if result["text_len"] >= 3000:
        score += 25
        result["notes"].append("rich text")
    elif result["text_len"] >= 1200:
        score += 18
        result["notes"].append("good text")
    elif result["text_len"] >= 500:
        score += 10
        result["notes"].append("some text")
    else:
        score -= 10
        result["notes"].append("thin text")

    # query term overlap
    hay = f"{title}\n{result['text']}".lower()
    overlap = sum(1 for t in QUERY_TERMS if t in hay)
    score += min(overlap * 4, 24)
    result["notes"].append(f"term overlap={overlap}")

    # kind
    kind = result["kind"]
    if kind == "article":
        score += 15
        result["notes"].append("article-like")
    elif kind == "pdf":
        score += 6
        result["notes"].append("pdf")
    elif kind == "section":
        score -= 8
        result["notes"].append("section-like")
    elif kind == "homepage":
        score -= 15
        result["notes"].append("homepage-like")
    elif kind == "thin":
        score -= 12
        result["notes"].append("thin page")

    # domain trust-ish
    domain = result["domain"].lower()
    trusted_news = ("apnews.com", "reuters.com", "bbc.com", "abcnews.go.com", "abcnews.com", "elpais.com", "elcomercio.pe", "rpp.pe")
    official = (".gob.pe", ".gov", ".org")
    if any(d in domain for d in trusted_news):
        score += 12
        result["notes"].append("trusted news")
    if any(domain.endswith(sfx) or sfx in domain for sfx in official):
        score += 8
        result["notes"].append("official/org")

    # freshness from URL/text hints
    fresh_text = f"{result['final_url']} {title} {result['freshness_hint']}".lower()
    if any(y in fresh_text for y in ["2026", "2025"]):
        score += 6
        result["notes"].append("recent hint")

    result["score"] = score
    return result


print("== Discovery ==")
raw = discover(query)

try:
    data = json.loads(raw)
except Exception as e:
    print(f"Failed to parse discovery JSON: {e}")
    sys.exit(1)

summary = data.get("summary", "")
sources = data.get("sources", [])

if not sources:
    print("No sources returned by discovery.")
    sys.exit(1)

print(f"\n== Topic ==\n{topic}")
print(f"\n== Query ==\n{query}")
print(f"\n== External Summary ==\n{summary}")

print(f"\n== Fetching and scoring {len(sources)} source(s) ==")
ranked = []
for idx, source in enumerate(sources, 1):
    item = fetch_source(source)
    ranked.append(item)
    print(f"[{idx}/{len(sources)}] {item['title']} -> score {item['score']}")

ranked.sort(key=lambda x: x["score"], reverse=True)

print("\n== Ranked Sources ==")
for i, item in enumerate(ranked, 1):
    print(f"\n[{i}] score={item['score']} kind={item['kind']} domain={item['domain']}")
    print(f"title: {item['title']}")
    print(f"url:   {item['final_url']}")
    print(f"text_len: {item['text_len']}")
    print(f"notes: {', '.join(item['notes'])}")
    snippet = item["text"][:300].replace("\n", " ").strip()
    if snippet:
        print(f"snippet: {snippet}")


