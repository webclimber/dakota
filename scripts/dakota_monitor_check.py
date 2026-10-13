import os
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

sys.path.append(str(Path(__file__).resolve().parent))
from dakota_sources import parse_json_text

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
BREAKING_NEWS_PROMPT = ROOT / "config" / "prompts" / "monitor_event_prompt.txt"
NEWSLETTER_PROMPT = ROOT / "config" / "prompts" / "newsletter_event_prompt.txt"
BREAKING_MODEL = os.getenv("DAKOTA_MONITOR_EVENT_MODEL", "gpt-5.6-luna")
NEWSLETTER_MODEL = os.getenv("DAKOTA_NEWSLETTER_MODEL", "gpt-5.6-terra")
client = OpenAI()

if len(sys.argv) < 2:
    print('Usage: python scripts/dakota_monitor_check.py <monitor_spec.json> [--queries JSON]')
    sys.exit(1)

spec_path = Path(sys.argv[1]).expanduser().resolve()
spec = json.loads(spec_path.read_text())
monitor_id = spec["monitor_id"]

# Parse optional --queries override (JSON list of query strings from River's rotation)
active_queries = None
argv_rest = sys.argv[2:]
# --dry-run: do everything, but write the event to dryrun_event.json so it neither enters the
# issue history nor replaces latest_event.json
DRY_RUN = "--dry-run" in argv_rest
for i, arg in enumerate(argv_rest):
    if arg == "--queries" and i + 1 < len(argv_rest):
        try:
            active_queries = json.loads(argv_rest[i + 1])
        except Exception as e:
            print(f"Warning: could not parse --queries arg: {e}")
        break
topic = spec["topic"]
monitor_dir = ROOT / "reports" / "monitors" / monitor_id
bootstrap_state_path = monitor_dir / "bootstrap_state.json"
if not bootstrap_state_path.exists():
    print(f"Missing bootstrap_state.json at {bootstrap_state_path}")
    sys.exit(1)

print("== Monitor Spec ==")
print(spec_path)
print(f"monitor_id: {monitor_id}")
print(f"topic:      {topic}")
print(f"title:      {spec.get('title', '')}")
print(f"monitor:    {spec.get('query_prompts', {}).get('monitor_query', '')}")
print(f"monitor_dir:{monitor_dir}")

print("\n== Running Dedicated Monitor Research ==")
cmd = [sys.executable, str(ROOT / "scripts" / "dakota_monitor_research.py"), str(spec_path), "--query-type", "monitor"]
if active_queries is not None:
    cmd += ["--queries", json.dumps(active_queries)]
    print(f"query rotation: {len(active_queries)} queries — {active_queries}")
proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
print(proc.stdout)
if proc.returncode != 0:
    print(proc.stderr)
    sys.exit(proc.returncode)

runs_dir = monitor_dir / "runs"
run_files = sorted(runs_dir.glob("*-monitor.json"), reverse=True)
if not run_files:
    print("No monitor JSON run found.")
    sys.exit(1)
current_run_path = run_files[0]
current_run = json.loads(current_run_path.read_text())

bootstrap_state = json.loads(bootstrap_state_path.read_text())
prior_events_dir = monitor_dir / "events"
prior_events_dir.mkdir(parents=True, exist_ok=True)
recent_event_files = sorted(prior_events_dir.glob("*-event.json"), reverse=True)[:5]
recent_events = [json.loads(p.read_text()) for p in recent_event_files]

monitor_style = spec.get("monitor_style", "breaking_news")
is_newsletter = monitor_style == "newsletter"
MODEL = NEWSLETTER_MODEL if is_newsletter else BREAKING_MODEL
prompt_file = NEWSLETTER_PROMPT if is_newsletter else BREAKING_NEWS_PROMPT
print(f"monitor_style: {monitor_style}  prompt: {prompt_file.name}  model: {MODEL}")
prompt_template = prompt_file.read_text()


def _event_items(ev: dict) -> list:
    """Reader-facing statements of a past issue (older events only have plain bullets)."""
    items = [i.get("text", "") for i in (ev.get("items") or []) if isinstance(i, dict)]
    return [i for i in (items or ev.get("recommended_digest_bullets") or []) if i]


todays_sources = current_run.get("selected_sources", [])
if is_newsletter:
    # The writer sees today's extracted facts plus a list of what was already told to the reader.
    # It never sees its own earlier prose as material to build on — that is what made every issue
    # a paraphrase of the previous one.
    already_reported, seen_items = [], set()
    for ev in recent_events:
        for text in _event_items(ev):
            if text not in seen_items:
                seen_items.add(text)
                already_reported.append(text)
    previous = recent_events[0] if recent_events else {}
    user_payload = {
        "today": datetime.now().strftime("%Y-%m-%d"),
        "monitor": {k: spec.get(k) for k in ("title", "user_request", "publish_cadence", "watch_axes", "tracked_metrics")},
        "todays_sources": [
            {k: s.get(k) for k in ("title", "final_url", "preferred", "latest_date", "facts", "metrics")}
            for s in todays_sources
        ],
        "previous_issue": {
            "date": (previous.get("generated_at") or "")[:10] or None,
            "tracked_metrics": previous.get("tracked_metrics") or [],
        },
        "already_reported": already_reported,
    }
else:
    user_payload = {
        "monitor_spec": spec,
        "bootstrap_state": bootstrap_state,
        "current_run": current_run,
        "recent_events": recent_events,
    }
prompt = prompt_template + "\n\nJSON INPUT:\n" + json.dumps(user_payload, ensure_ascii=False, indent=2)

resp = client.responses.create(model=MODEL, input=prompt)
usage = getattr(resp, "usage", None)
if usage:
    print("== USAGE ==")
    print({
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cached_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0),
        "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0),
    })

raw = resp.output_text
print("\n== Raw Event Output ==")
print(raw)

try:
    event = parse_json_text(raw)
except Exception as e:
    print(f"Failed to parse event JSON: {e}")
    sys.exit(1)

if is_newsletter:
    # Enforce sourcing in code, not just in the prompt: a statement that does not point at a page
    # read today is dropped, and metric history comes from stored events rather than the model.
    known_urls = {s.get("final_url") for s in todays_sources}
    kept, dropped = [], []
    for item in event.get("items") or []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        urls = [u for u in (item.get("source_urls") or []) if u in known_urls]
        if urls:
            kept.append({"text": item["text"], "source_urls": urls, "date": item.get("date")})
        else:
            dropped.append(item["text"])
    event["items"] = kept
    event["dropped_unsourced"] = dropped
    if dropped:
        print(f"dropped {len(dropped)} unsourced item(s)")

    last_values = {}
    for ev in reversed(recent_events):  # oldest first so the newest value wins
        for m in ev.get("tracked_metrics") or []:
            if m.get("name") and m.get("value") not in (None, ""):
                last_values[m["name"]] = {"value": m["value"], "as_of": m.get("as_of"),
                                          "issue_date": (ev.get("generated_at") or "")[:10]}
    metrics = []
    for m in event.get("tracked_metrics") or []:
        if not isinstance(m, dict) or not m.get("name") or m.get("value") in (None, ""):
            continue
        if m.get("source_url") not in known_urls:
            continue
        prev = last_values.get(m["name"])
        metrics.append({
            "name": m["name"], "value": str(m["value"]), "as_of": m.get("as_of"),
            "source_url": m["source_url"],
            "previous_value": prev["value"] if prev else None,
            "previous_issue_date": prev["issue_date"] if prev else None,
            "changed": bool(prev) and str(prev["value"]).strip() != str(m["value"]).strip(),
        })
    event["tracked_metrics"] = metrics
    event["quiet"] = not kept
    event["recommended_digest_bullets"] = [i["text"] for i in kept]
    event["new_facts"] = [i["text"] for i in kept]
    event["changed_facts"] = [f"{m['name']}: {m['previous_value']} -> {m['value']}" for m in metrics if m["changed"]]
    event["breaking"] = False
    event["delivery_recommendation"] = "daily_digest"

llm_usage = None
if usage:
    llm_usage = {
        "model": MODEL,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }

event.update({
    "monitor_id": monitor_id,
    "topic": topic,
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "source_run_json": str(current_run_path.resolve()),
    "bootstrap_state_path": str(bootstrap_state_path.resolve()),
    "llm_usage": llm_usage,
    "extract_llm_usage": current_run.get("extract_usage"),
})

if DRY_RUN:
    event_path = latest_path = monitor_dir / "dryrun_event.json"
    latest_path.write_text(json.dumps(event, indent=2, ensure_ascii=False))
else:
    event_path = prior_events_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-event.json"
    event_path.write_text(json.dumps(event, indent=2, ensure_ascii=False))
    latest_path = monitor_dir / "latest_event.json"
    latest_path.write_text(json.dumps(event, indent=2, ensure_ascii=False))

print("\n== Saved ==")
print(event_path)
print(latest_path)
print("\n== Delivery Recommendation ==")
print(event.get("delivery_recommendation", "store_only"))
print("\n== Breaking ==")
print(event.get("breaking", False))
print("\n== Importance Score ==")
print(event.get("importance_score", 0))
