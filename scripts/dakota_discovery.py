import os
import json
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("DAKOTA_DISCOVERY_MODEL", "gpt-5.4-mini")


def _extract_json(text: str) -> str | None:
    """Try to extract a JSON object from potentially mixed text."""
    # Try direct parse first
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            json.loads(stripped)
            return stripped
        except Exception:
            pass
    # Try to find embedded JSON block
    m = re.search(r'\{.*"sources"\s*:\s*\[.*\]\s*\}', stripped, re.DOTALL)
    if m:
        try:
            json.loads(m.group(0))
            return m.group(0)
        except Exception:
            pass
    return None


def _prose_to_json(text: str) -> str:
    """Fallback: convert prose response with markdown links to expected JSON format."""
    # Summary: first block of non-empty, non-header, non-list lines
    lines = text.split('\n')
    summary_parts = []
    for line in lines:
        s = line.strip()
        if not s:
            if summary_parts:
                break
        elif s.startswith('#') or s.startswith('*') or s.startswith('-') or s.startswith('['):
            if summary_parts:
                break
        else:
            summary_parts.append(s)
    summary = ' '.join(summary_parts)[:600]

    # Extract markdown links [Title](url)
    sources = []
    seen = set()
    for m in re.finditer(r'\[([^\]]+)\]\((https?://[^)]+)\)', text):
        title, url = m.group(1), m.group(2)
        # Strip UTM params
        clean = url.split('?utm_source=')[0]
        if clean not in seen:
            seen.add(clean)
            sources.append({"title": title, "url": clean, "source_type": "news"})

    return json.dumps({"summary": summary, "sources": sources}, ensure_ascii=False)


def discover(query: str, recency_days: int | None = None, model: str | None = None) -> str:
    model = model or MODEL
    today = datetime.now().strftime("%Y-%m-%d")
    recency_rule = (
        f"- Today is {today}. Only return items published or updated in the last {recency_days} days.\n"
        f"- Skip evergreen explainers, encyclopedias, background pages and anything older, however relevant.\n"
        f"- If fewer than 8 recent items exist, return fewer. Never pad with older material.\n"
    ) if recency_days else f"- Today is {today}.\n"
    resp = client.responses.create(
        model=model,
        tools=[{"type": "web_search"}],
        input=f"""
Find the most relevant and recent information for this query.

Query:
{query}

Requirements:
{recency_rule}- Return 8 to 12 sources
- Prioritize source diversity
- Include a mix when possible:
  - international news
  - local or national news from the relevant country
  - official election/government/institutional sources
  - polling or analysis sources
- Avoid returning many links from the same domain unless absolutely necessary
- Prefer direct article or primary-source pages over homepages
- Keep the summary under 100 words
- Do not repeat information
- Focus on key facts only

Return JSON only in exactly this format:
{{
  "summary": "...",
  "sources": [
    {{"title": "...", "url": "...", "source_type": "news|local|official|poll|analysis"}},
    {{"title": "...", "url": "...", "source_type": "news|local|official|poll|analysis"}}
  ]
}}
"""
    )

    usage = getattr(resp, "usage", None)
    if usage:
        print("\n== USAGE ==")
        print({
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "cached_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0),
            "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0),
        })

        log_path = Path("logs/usage.jsonl")
        log_path.parent.mkdir(exist_ok=True)

        record = {
            "timestamp": datetime.now().isoformat(),
            "kind": "openai_discovery",
            "provider": "openai",
            "model": model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "cached_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0),
            "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0),
            "query": query,
        }

        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    raw = resp.output_text
    print(raw)

    # Normalize to JSON — model sometimes returns prose instead of JSON
    normalized = _extract_json(raw)
    if normalized is None:
        print("[discover] output was not JSON — falling back to prose parser", flush=True)
        normalized = _prose_to_json(raw)

    return normalized


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:])
    if not q:
        print('Usage: python scripts/dakota_discovery.py "your query"')
        raise SystemExit(1)

    discover(q)
