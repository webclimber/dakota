import os
import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("DAKOTA_DISCOVERY_MODEL", "gpt-5.4-mini")


def discover(query: str) -> str:
    resp = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search"}],
        input=f"""
Find the most relevant and recent information for this query.

Query:
{query}

Requirements:
- Return 8 to 12 sources
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
            "model": MODEL,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "cached_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0),
            "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0),
            "query": query,
        }

        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    print(resp.output_text)
    return resp.output_text


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:])
    if not q:
        print('Usage: python scripts/dakota_discovery.py "your query"')
        raise SystemExit(1)

    discover(q)
