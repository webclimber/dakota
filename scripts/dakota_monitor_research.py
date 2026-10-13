import os
import sys
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import chromadb
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

sys.path.append(str(Path(__file__).resolve().parent))
from dakota_discovery import discover
from dakota_sources import fetch_and_extract_many, days_old

load_dotenv()

if len(sys.argv) < 2:
    print('Usage: python scripts/dakota_monitor_research.py <monitor_spec.json> [--query-type bootstrap|monitor|digest]')
    sys.exit(1)

spec_path = Path(sys.argv[1]).expanduser().resolve()
query_type = "monitor"
active_queries_override = None
argv_rest = sys.argv[2:]
i = 0
while i < len(argv_rest):
    if argv_rest[i] == "--query-type" and i + 1 < len(argv_rest):
        query_type = argv_rest[i + 1]
        i += 2
    elif argv_rest[i].startswith("--query-type="):
        query_type = argv_rest[i].split("=", 1)[1]
        i += 1
    elif argv_rest[i] == "--queries" and i + 1 < len(argv_rest):
        try:
            active_queries_override = json.loads(argv_rest[i + 1])
        except Exception as e:
            print(f"Warning: could not parse --queries: {e}")
        i += 2
    else:
        i += 1

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
MODEL = os.environ.get("OLLAMA_SUMMARIZE_MODEL", "qwen3.6:35b")
DISCOVERY_MODEL = os.environ.get("DAKOTA_MONITOR_DISCOVERY_MODEL", "gpt-5.6-luna")
MONITOR_STYLE = spec.get("monitor_style", "breaking_news")
# How far back a search result may be dated and still count as news for this cadence
RECENCY_DAYS = {"daily": 3, "weekly": 9, "monthly": 35}.get(spec.get("publish_cadence") or "", 3)
MAX_DISCOVERED_FETCH = 14
MAX_DISCOVERED_SELECTED = 6
MAX_DISCOVERED_AGE_DAYS = max(7, RECENCY_DAYS * 3)
QUERY_TERMS = {t.lower() for t in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9]+", query) if len(t) >= 4}

client = chromadb.PersistentClient(path="chroma")
col = client.get_or_create_collection("dakota_runs")


_SPANISH_LOCAL_DOMAINS = {
    "marbella":  ["sur.es", "surinenglish.com", "laopiniondemalaga.es", "malagahoy.es", "marbella24.com"],
    "málaga":    ["sur.es", "surinenglish.com", "laopiniondemalaga.es", "malagahoy.es"],
    "malaga":    ["sur.es", "surinenglish.com", "laopiniondemalaga.es", "malagahoy.es"],
    "sevilla":   ["diariodesevilla.es", "elcorreoweb.es", "abc.es"],
    "granada":   ["granadahoy.com", "ideal.es"],
    "cadiz":     ["diariodecadiz.es", "lavozdigital.es"],
    "almeria":   ["lavozdealmeria.es", "ideal.es"],
    "madrid":    ["elmundo.es", "elpais.com", "20minutos.es"],
    "barcelona": ["lavanguardia.com", "elperiodico.com", "ara.cat"],
}

_AXIS_ES = {
    "regulation": "regulaciones normativas",
    "weather":    "tiempo meteorología",
    "infra":      "infraestructura obras",
    "transport":  "transporte tráfico",
    "tourism":    "turismo hostelería",
    "housing":    "vivienda alquiler",
    "community":  "vecinos calidad vida",
    "municipal":  "ayuntamiento noticias",
    "project":    "proyectos inversión",
}


def generate_supplemental_queries(main_query: str, spec: dict) -> list:
    title = (spec.get("title") or "").lower()
    watch_axes = spec.get("watch_axes") or []

    local_domains = []
    location_display = []
    for place, domains in _SPANISH_LOCAL_DOMAINS.items():
        if place in title:
            local_domains = domains
            location_display.append(place.replace("á", "á").capitalize())

    if not local_domains:
        return []

    # Spanish axis keywords from watch_axes
    spanish_terms = []
    for axis in watch_axes[:4]:
        for key, esp in _AXIS_ES.items():
            if key in axis.lower():
                spanish_terms.append(esp.split()[0])
                break

    loc = " ".join(location_display[:2]) or "local"
    axes_str = " ".join(spanish_terms[:2]) if spanish_terms else "noticias"

    q_spanish = f"{loc} noticias locales {axes_str} esta semana"
    site_expr = " OR ".join(f"site:{d}" for d in local_domains[:4])
    q_site = f"{loc} local news ({site_expr})"

    return [q_spanish, q_site]


search_queries = active_queries_override if active_queries_override is not None else (spec.get("search_queries") or [])
supplemental = generate_supplemental_queries(query, spec)
base_queries = search_queries if search_queries else [query]
all_queries = base_queries + [q for q in supplemental if q not in base_queries]

# Extend QUERY_TERMS with terms from all search queries so sources score correctly
for _q in all_queries:
    QUERY_TERMS.update(t.lower() for t in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9]+", _q) if len(t) >= 4)


print(f"== Discovery ({len(all_queries)} queries, last {RECENCY_DAYS} days) ==")
all_sources_by_url: dict = {}
summaries: list = []
for _q in all_queries:
    print(f"\n  query: {_q[:100]}")
    _raw = discover(_q, recency_days=RECENCY_DAYS, model=DISCOVERY_MODEL)
    try:
        _data = json.loads(_raw)
    except Exception as e:
        print(f"  failed to parse discovery JSON: {e}")
        continue
    summaries.append(_data.get("summary", ""))
    for s in _data.get("sources", []):
        url = s.get("url", "")
        if url and url not in all_sources_by_url:
            all_sources_by_url[url] = s

summary = " | ".join(s for s in summaries if s)

# Preferred sources (picked and validated when the monitor was created) are read every run;
# search results only supplement them with news.
preferred = spec.get("preferred_sources") or []
preferred_urls = {p["url"] for p in preferred}
preferred_domains = {urlparse(p["url"]).netloc for p in preferred}
discovered = [s for u, s in all_sources_by_url.items() if u not in preferred_urls][:MAX_DISCOVERED_FETCH]

print(f"\n== Reading {len(preferred)} preferred + {len(discovered)} discovered source(s) ==")
pref_results = fetch_and_extract_many(preferred, spec)
disc_results = fetch_and_extract_many(discovered, spec)

selected: list = []
discarded: list = []
for p, r in zip(preferred, pref_results):
    r["preferred"] = True
    r["provides"] = p.get("provides", "")
    r["source_type"] = "preferred"
    (selected if r["ok"] else discarded).append(r)

usable_discovered = []
for s, r in zip(discovered, disc_results):
    r["preferred"] = False
    r["provides"] = ""
    r["source_type"] = s.get("source_type", "unknown")
    age = days_old(r["latest_date"], now)
    if r["ok"] and age is not None and age > MAX_DISCOVERED_AGE_DAYS:
        r["ok"] = False
        r["reason"] = f"not recent — newest date on the page is {r['latest_date']}"
    (usable_discovered if r["ok"] else discarded).append(r)

# Newest first; undated pages last. At most 2 per domain, counting preferred sources.
usable_discovered.sort(key=lambda r: (days_old(r["latest_date"], now) is None,
                                      days_old(r["latest_date"], now) or 0, -len(r["facts"])))
domain_counts = {d: 2 for d in preferred_domains}
for r in usable_discovered:
    if len([x for x in selected if not x["preferred"]]) >= MAX_DISCOVERED_SELECTED:
        break
    if domain_counts.get(r["domain"], 0) >= 2:
        continue
    domain_counts[r["domain"]] = domain_counts.get(r["domain"], 0) + 1
    selected.append(r)

print("\n== Selected Sources ==")
for s in selected:
    print(f"- {'[preferred] ' if s['preferred'] else ''}{s['title']} ({s['domain']}) newest={s['latest_date']} facts={len(s['facts'])}")
print("\n== Could Not Use ==")
for s in discarded:
    print(f"- {'[preferred] ' if s['preferred'] else ''}{s['title']} ({s['domain']}): {s['reason']}")

extract_usage = [r["usage"] for r in pref_results + disc_results if r.get("usage")]

sources_text = "\n".join([f"- {s['title']} ({s['final_url']})" for s in selected])


def _facts_block(s: dict) -> str:
    rows = [f"  - {f['fact']}" + (f" [{f['date']}]" if f.get("date") else "") for f in s["facts"]]
    rows += [f"  - {m['name']}: {m.get('value')}" + (f" [{m['as_of']}]" if m.get("as_of") else "") for m in s["metrics"]]
    return f"Title: {s['title']}\nURL: {s['final_url']}\nNewest date on page: {s['latest_date']}\n" + "\n".join(rows)


fetched_text = "\n\n====================\n\n".join(_facts_block(s) for s in selected)

memory = col.query(query_texts=[query], n_results=3, where={"topic": topic})
memory_docs = memory.get("documents", [[]])[0] or []
memory_text = "\n\n---\n\n".join(memory_docs) if memory_docs else "None"

# Newsletter issues are written from the per-source facts directly (dakota_monitor_check.py);
# the local-model synthesis is only used by breaking-news monitors' "Research Notes".
analysis_text = ""
if MONITOR_STYLE != "newsletter":
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

Selected sources:
{sources_text}

Facts extracted from each source today:
{fetched_text}

Previous memory for this topic (background only — may be out of date):
{memory_text}

Write a concise but useful answer grounded only in the facts extracted today.
Do not state anything the extracted facts do not support.

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
    llm = ChatOllama(model=MODEL, reasoning=False)
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

SOURCE_KEYS = ["title", "final_url", "domain", "source_type", "text_len", "preferred", "provides",
               "latest_date", "facts", "metrics"]
run_obj = {
    "monitor_id": monitor_id,
    "topic": topic,
    "query_type": query_type,
    "query": query,
    "all_queries": all_queries,
    "recency_days": RECENCY_DAYS,
    "generated_at": now.isoformat(),
    "external_summary": summary,
    "selected_sources": [{k: s[k] for k in SOURCE_KEYS} for s in selected],
    "discarded_sources": [
        {"title": s["title"], "url": s["url"], "domain": s["domain"], "preferred": s["preferred"], "reason": s["reason"]}
        for s in discarded
    ],
    "extract_usage": {
        "model": extract_usage[0]["model"] if extract_usage else None,
        "calls": len(extract_usage),
        "input_tokens": sum(u["input_tokens"] for u in extract_usage),
        "output_tokens": sum(u["output_tokens"] for u in extract_usage),
        "total_tokens": sum(u["total_tokens"] for u in extract_usage),
    },
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
        f.write(f"- {s['title']} ({s['final_url']}) newest={s['latest_date']} facts={len(s['facts'])}\n")
    f.write("\n## Could Not Use\n")
    for s in discarded:
        f.write(f"- {s['title']} ({s['url']}): {s['reason']}\n")
    f.write("\n## Extracted Facts\n")
    f.write(fetched_text + "\n")
    f.write("\n## Analysis\n")
    f.write(analysis_text + "\n")
print(f"\n== JSON Run ==\n{json_path}")
print(f"\n== Markdown Report ==\n{md_path}")
print("\n== Analysis ==\n")
print(analysis_text)
