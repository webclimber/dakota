# Dakota

Mac Mini at `dakota.home.arpa` / `192.168.7.36`. Acts as River's AI compute helper — runs heavier research and monitor-check workloads offloaded via SSH from the River Mini.

**SSH access:** `dakota@dakota.home.arpa`
**Working directory:** `/Users/dakota/dakota`
**Python venv:** `/Users/dakota/dakota/.venv/bin/python`

## Role

Dakota does not initiate work. River SSHs in, runs a script, and SCPs back the result. Dakota provides:
- Monitor compilation and periodic check execution (deep-monitor pipeline)
- Research summarization for independent-research pipeline (Phase 3)
- ChromaDB vector store for embeddings
- FastAPI research API (available but not yet wired into production flows)

## Services

| Service | Label | Status |
|---------|-------|--------|
| FastAPI research API | `com.dakota.research-api` | Running (`localhost:8787`) |
| Caddy reverse proxy | `com.dakota.caddy` | Running |
| Ollama | `com.dakota.ollama` | Not running (launchd exit 1) |
| Daily usage report | `com.dakota.daily-usage-report` | Not running (launchd exit 127) |

Manage via launchctl: `launchctl kickstart -k gui/$(id -u)/com.dakota.research-api`

## Models

| Env var | Value | Used for |
|---------|-------|---------|
| `DAKOTA_DISCOVERY_MODEL` | `gpt-5.4-mini` (OpenAI) | Monitor compilation, checks |
| `DAKOTA_FALLBACK_MODEL` | `gemini-2.5-flash` (Google) | Fallback if OpenAI fails |

Ollama is installed but not currently running.

## Scripts Called by River

These are the scripts River invokes via SSH as part of its pipelines:

| Script | Called by | Purpose |
|--------|-----------|---------|
| `scripts/dakota_compile_monitor.py` | River `start_monitor.py` | Compiles natural-language request → structured `spec.json` |
| `scripts/dakota_bootstrap_monitor.py` | River `start_monitor.py` | Generates initial email + state snapshot for a new monitor |
| `scripts/dakota_monitor_check.py` | River `run_scheduler.py` (every 10 min) | Runs periodic monitor check → `latest_event.json` |

River SCPs results back from `reports/monitors/<id>/` after each check.

## Other Scripts

| Script | Purpose |
|--------|---------|
| `scripts/dakota_research.py` | General research queries |
| `scripts/dakota_discovery.py` | Source discovery |
| `scripts/dakota_rank_sources.py` | Source ranking |
| `scripts/dakota_monitor_research.py` | Deep research for monitor events |
| `scripts/dakota_history.py` | Historical data queries |
| `scripts/dakota_smoke.py` | Smoke test for API + model connectivity |

## Directory Layout

```
/Users/dakota/dakota/
├── scripts/          # All Python scripts (see above)
├── config/           # Prompts, monitor defaults, settings
├── memory/
│   ├── structured/   # preferences.json (owner, approval mode, delivery prefs)
│   └── monitor_specs/ # Compiled spec JSONs per monitor
├── reports/
│   └── monitors/<id>/ # latest_event.json, run reports
├── chroma/           # ChromaDB vector store
├── logs/             # usage.jsonl (LLM call log), service logs
├── agents/research/  # Research agent state
├── bin/              # Helper scripts (dakota-compile-monitor, etc.)
├── launchd/          # LaunchAgent plists
├── .env              # API keys + model config (not in git)
└── requirements.txt
```

## State Files

| Path | Purpose |
|------|---------|
| `memory/monitor_specs/` | One JSON per compiled monitor spec |
| `reports/monitors/<id>/latest_event.json` | Most recent check result (River SCPs this) |
| `logs/usage.jsonl` | LLM call log — input/output tokens per invocation |
| `chroma/` | ChromaDB embeddings store |

## Connectivity Check

```bash
# From River:
ssh -o ConnectTimeout=10 -o BatchMode=yes dakota@dakota.home.arpa "echo ok"

# From Dakota:
curl http://127.0.0.1:8787/health
```

## Known Issues

- Ollama not running (launchd exit 1) — local model fallback unavailable
- `com.dakota.daily-usage-report` exits 127 (command not found) — likely broken venv path in plist
- `independent_research_worker.py` not yet deployed — River falls back to local summarization for Phase 3
- Monitor spec files accumulate in `memory/monitor_specs/` with no cleanup
