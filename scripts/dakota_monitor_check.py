import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "config" / "prompts" / "monitor_event_prompt.txt"
MODEL = "gpt-5.4-mini"
client = OpenAI()

if len(sys.argv) < 2:
    print('Usage: python scripts/dakota_monitor_check.py <monitor_spec.json>')
    sys.exit(1)

spec_path = Path(sys.argv[1]).expanduser().resolve()
spec = json.loads(spec_path.read_text())
monitor_id = spec["monitor_id"]
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

prompt_template = PROMPT_PATH.read_text()
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
    event = json.loads(raw)
except Exception as e:
    print(f"Failed to parse event JSON: {e}")
    sys.exit(1)

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
})

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
