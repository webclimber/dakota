# Dakota Companion Pack v1

This starter pack matches the Dakota setup guide and gives you a runnable baseline repo.

## Included
- FastAPI research service
- usage logging helper
- Caddy reverse proxy config
- launchd plists for API, Caddy, and daily usage report
- `.env.example`
- Python requirements
- helper scripts for bootstrapping and searching River for secrets

## Expected install path
```bash
/Users/dakota/dakota
```

## Quick start
```bash
cd /Users/dakota
cp -R /path/to/dakota-companion-pack dakota
cd dakota
cp .env.example .env
./scripts/bootstrap_env.sh
```

Then edit `.env`, install/pull Ollama models, and copy the plists into `~/Library/LaunchAgents/`.

## First smoke tests
```bash
source .venv/bin/activate
python -m uvicorn services.research_api.app:app --host 127.0.0.1 --port 8787
curl http://127.0.0.1:8787/health
curl -X POST http://127.0.0.1:8787/run \
  -H 'content-type: application/json' \
  -d '{"topic":"test geopolitics sweep","priority":"normal"}'
```

## LaunchAgents install
```bash
mkdir -p ~/Library/LaunchAgents
cp launchd/com.dakota.research-api.plist ~/Library/LaunchAgents/
cp launchd/com.dakota.caddy.plist ~/Library/LaunchAgents/
cp launchd/com.dakota.usage-report.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dakota.research-api.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dakota.caddy.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dakota.usage-report.plist
launchctl kickstart -k gui/$(id -u)/com.dakota.research-api
launchctl kickstart -k gui/$(id -u)/com.dakota.caddy
```
