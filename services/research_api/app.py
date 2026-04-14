from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from services.research_api.usage import log_usage

BASE = Path.home() / "dakota"
LOG_DIR = BASE / "logs"
MEMORY_DIR = BASE / "memory" / "structured"
REPORT_DIR = BASE / "reports"
for path in (LOG_DIR, MEMORY_DIR, REPORT_DIR):
    path.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE / ".env")

app = FastAPI(title="Dakota Research API", version="0.1.0")


class RunRequest(BaseModel):
    topic: str = Field(..., min_length=3)
    priority: str = "normal"
    source: str = "manual"
    model_hint: str | None = None
    notes: str | None = None


class WebhookRequest(BaseModel):
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)
    token: str


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "dakota-research-api",
        "time": datetime.now().isoformat(),
        "node": os.environ.get("DAKOTA_NODE_NAME", "dakota"),
        "env": os.environ.get("DAKOTA_ENV", "dev"),
    }


@app.get("/config")
def config_summary() -> dict[str, Any]:
    return {
        "small_model": os.environ.get("DEFAULT_LOCAL_SMALL_MODEL", "qwen2.5:7b"),
        "medium_model": os.environ.get("DEFAULT_LOCAL_MEDIUM_MODEL", "llama3.1:8b"),
        "large_model": os.environ.get("DEFAULT_LOCAL_LARGE_MODEL", "qwen2.5:14b"),
        "external_provider": os.environ.get("DEFAULT_EXTERNAL_PROVIDER", "openai"),
        "external_model": os.environ.get("DEFAULT_EXTERNAL_MODEL", "gpt-5.4"),
    }


@app.post("/run")
def run_job(req: RunRequest) -> dict[str, Any]:
    started = time.time()
    selected_model = req.model_hint or os.environ.get("DEFAULT_LOCAL_MEDIUM_MODEL", "llama3.1:8b")
    result = {
        "topic": req.topic,
        "priority": req.priority,
        "source": req.source,
        "notes": req.notes,
        "selected_model": selected_model,
        "status": "queued",
        "created_at": datetime.now().isoformat(),
    }
    _append_jsonl(LOG_DIR / "runs.jsonl", result)
    log_usage(
        "run_request",
        {
            "topic": req.topic,
            "priority": req.priority,
            "source": req.source,
            "selected_model": selected_model,
            "provider": "local-orchestrator",
            "input_tokens": None,
            "output_tokens": None,
            "estimated_cost_usd": None,
        },
    )
    return {
        "ok": True,
        "queued": True,
        "latency_ms": round((time.time() - started) * 1000, 2),
        "job": result,
    }


@app.post("/webhook/river")
async def webhook_from_river(req: WebhookRequest, request: Request) -> dict[str, Any]:
    expected = os.environ.get("DAKOTA_WEBHOOK_TOKEN", "")
    if not expected or req.token != expected:
        raise HTTPException(status_code=401, detail="invalid token")

    event = {
        "received_at": datetime.now().isoformat(),
        "client": request.client.host if request.client else None,
        "event": req.event,
        "payload": req.payload,
    }
    _append_jsonl(LOG_DIR / "river_webhooks.jsonl", event)
    log_usage(
        "river_webhook",
        {
            "event_name": req.event,
            "provider": "river",
            "client": request.client.host if request.client else None,
        },
    )
    return {"ok": True, "accepted": True}
