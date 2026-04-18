import sys
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import trafilatura
import chromadb
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

sys.path.append(str(Path(__file__).resolve().parent))
from dakota_discovery import discover

load_dotenv()

if len(sys.argv) < 2:
    print('Usage: python scripts/dakota_monitor_research.py <monitor_spec.json> [--query-type bootstrap|monitor|digest]')
    sys.exit(1)

spec_path = Path(sys.argv[1]).expanduser().resolve()
query_type = "monitor"
if len(sys.argv) >= 4 and sys.argv[2] == "--query-type":
    query_type = sys.argv[3]
elif len(sys.argv) == 3 and sys.argv[2].startswith("--query-type="):
    query_type = sys.argv[2].split("=", 1)[1]

spec = json.loads(spec_path.read_text())
monitor_id = spec["monitor_id"]
topic = spec["topic"]
queries = spec.get("query_prompts", {})
query_map = {
    "bootstrap": queries.get("bootstrap_query"),
    "monitor": queries.get("monitor_query"),
    "digest": queries.get("digest_query"),
}
query = query_map.get(query_type)
if not query:
    print(f"No query found for query_type={query_type}")
    sys.exit(1)

now = datetime.now()
run_id = now.strftime('run-%Y%m%d-%H%M%S')
MODEL = "qwen2.5:7b"
QUERY_TERMS = {t.lower() for t in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9]+", query) if len(t) >= 4}

llm = ChatOllama(model=MODEL)
client = chromadb.PersistentClient(path="chroma")
col = client.get_or_create_collection("dakota_runs")


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
        "source_type": source.get("source_type", "unknown"),
    }
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 DakotaResearch/1.0"})
        result["status_code"] = r.status_code
        result["final_url"] = str(r.url)
        result["domain"] = urlparse(str(r.url)).netloc
        result["content_type"] = r.headers.get("content-type", "")
        result["freshness_hint"] = r.headers.get("last-modified", "") or r.headers.get("date", "")
        r.raise_for_status()
        extracted = trafilatura.extract(r.text, url=str(r.url), include_comments=False, include_tables=False)
        text = (extracted or "").strip()
        result["text"] = text[:4000]
        result["text_len"] = len(text)
        result["fetch_ok"] = True
        result["kind"] = detect_kind(str(r.url), result["content_type"], text)
    except Exception as e:
        result["notes"].append(f"fetch failed: {e}")
        return result

    score = 10
    if result["text_len"] >= 3000:
        score += 25; result["notes"].append("rich text")
    elif result["text_len"] >= 1200:
        score += 18; result["notes"].append("good text")
    elif result["text_len"] >= 500:
        score += 10; result["notes"].append("some text")
    else:
        score -= 10; result["notes"].append("thin text")

    hay = f"{title}\n{result['text']}".lower()
    overlap = sum(1 for t in QUERY_TERMS if t in hay)
    score += min(overlap * 4, 24)
    result["notes"].append(f"term overlap={overlap}")

    kind = result["kind"]
    if kind == "article":
        score += 15; result["notes"].append("article-like")
    elif kind == "pdf":
        score += 6; result["notes"].append("pdf")
    elif kind == "section":
        score -= 8; result["notes"].append("section-like")
    elif kind == "homepage":
        score -= 15; result["notes"].append("homepage-like")
    elif kind == "thin":
        score -= 12; result["notes"].append("thin page")

    domain = result["domain"].lower()
    trusted_news = ("apnews.com", "reuters.com", "bbc.com", "abcnews.go.com", "abcnews.com", "elpais.com", "elcomercio.pe", "rpp.pe", "theguardian.com", "aljazeera.com")
    official = (".gob.pe", ".gov", ".org")
    if any(d in domain for d in trusted_news):
        score += 12; result["notes"].append("trusted news")
    if any(domain.endswith(sfx) or sfx in domain for sfx in official):
        score += 8; result["notes"].append("official/org")

    fresh_text = f"{result['final_url']} {title} {result['freshness_hint']}".lower()
    if any(y in fresh_text for y in ["2026", "2025"]):
        score += 6; result["notes"].append("recent hint")

    result["score"] = score
    return result


def select_diverse_sources(ranked: list, max_sources: int = 4) -> list:
    selected = []
    domain_counts = {}
    for item in ranked:
        if not item["fetch_ok"] or item["text_len"] < 500:
            continue
        domain = item["domain"]
        count = domain_counts.get(domain, 0)
        if count >= 2:
            continue
        selected.append(item)
        domain_counts[domain] = count + 1
        if len(selected) >= max_sources:
            break
    return selected

print("== Discovery ==")
raw = discover(query)
try:
    data = json.loads(raw)
except Exception as e:
    print(f"Failed to parse discovery JSON: {e}")
    sys.exit(1)
summary = data.get("summary", "")
sources = data.get("sources", [])
print(f"\n== Fetching and scoring {len(sources)} source(s) ==")
ranked = [fetch_source(s) for s in sources]
ranked.sort(key=lambda x: x["score"], reverse=True)
selected = select_diverse_sources(ranked, max_sources=4)
print("\n== Selected Sources ==")
for s in selected:
    print(f"- {s['title']} ({s['domain']}) score={s['score']} kind={s['kind']}")

sources_text = "\n".join([f"- {s['title']} ({s['final_url']})" for s in selected])
fetched_text = "\n\n====================\n\n".join([f"Title: {s['title']}\nURL: {s['final_url']}\n\n{s['text']}" for s in selected])

memory = col.query(query_texts=[query], n_results=3, where={"topic": topic})
memory_docs = memory.get("documents", [[]])[0] or []
memory_text = "\n\n---\n\n".join(memory_docs) if memory_docs else "None"

prompt = f"""
You are a research assistant focused on recurring topic monitoring.

Topic:
{topic}

Monitor ID:
{monitor_id}

Query Type:
{query_type}

Question:
{query}

External summary:
{summary}

Selected sources:
{sources_text}

Fetched source text:
{fetched_text}

Previous memory for this topic:
{memory_text}

Write a concise but useful answer grounded in the selected sources.

Return exactly this structure:

Executive summary:
- ...

Key points:
- ...
- ...
- ...

Open questions:
- ...
- ...
"""

resp = llm.invoke(prompt)
analysis_text = resp.content.strip()

art_dir = Path("reports") / "monitors" / monitor_id / "runs"
art_dir.mkdir(parents=True, exist_ok=True)
json_path = art_dir / f"{now.strftime('%Y%m%d-%H%M%S')}-{query_type}.json"
md_path = art_dir / f"{now.strftime('%Y%m%d-%H%M%S')}-{query_type}.md"

analysis = {"executive_summary": "", "key_points": [], "open_questions": []}
section = None
for line in analysis_text.splitlines():
    t = line.strip()
    if not t:
        continue
    low = t.lower()
    if low.startswith("executive summary"):
        section = "executive_summary"; continue
    if low.startswith("key points"):
        section = "key_points"; continue
    if low.startswith("open questions"):
        section = "open_questions"; continue
    if section == "executive_summary":
        analysis["executive_summary"] += (t.lstrip("- ").strip() + " ")
    elif section in ("key_points", "open_questions"):
        analysis[section].append(t.lstrip("- ").strip())
analysis["executive_summary"] = analysis["executive_summary"].strip()

run_obj = {
    "monitor_id": monitor_id,
    "topic": topic,
    "query_type": query_type,
    "query": query,
    "generated_at": now.isoformat(),
    "external_summary": summary,
    "selected_sources": [
        {k: s[k] for k in ["title", "final_url", "domain", "score", "kind", "source_type", "text_len"]}
        for s in selected
    ],
    "retrieved_memory": memory_docs,
    "analysis": analysis,
    "analysis_text": analysis_text,
    "artifacts": {
        "json_path": str(json_path.resolve()),
        "report_path": str(md_path.resolve()),
    },
}
json_path.write_text(json.dumps(run_obj, indent=2, ensure_ascii=False))
with open(md_path, "w") as f:
    f.write("# Dakota Monitor Research Run\n\n")
    f.write(f"**Monitor ID:** {monitor_id}\n")
    f.write(f"**Topic:** {topic}\n")
    f.write(f"**Query type:** {query_type}\n\n")
    f.write(f"## Query\n{query}\n\n")
    f.write(f"## External Summary\n{summary}\n\n")
    f.write("## Selected Sources\n")
    for s in selected:
        f.write(f"- {s['title']} ({s['final_url']}) score={s['score']} kind={s['kind']}\n")
    f.write("\n## Retrieved Memory\n")
    if memory_docs:
        for i, m in enumerate(memory_docs, 1):
            f.write(f"### Memory {i}\n{m}\n\n")
    else:
        f.write("None\n\n")
    f.write("## Analysis\n")
    f.write(analysis_text + "\n")
print(f"\n== JSON Run ==\n{json_path}")
print(f"\n== Markdown Report ==\n{md_path}")
print("\n== Analysis ==\n")
print(analysis_text)
