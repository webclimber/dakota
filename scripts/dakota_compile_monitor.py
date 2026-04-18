import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional, List, Dict, Any

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ConfigDict, ValidationError, field_validator

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "usage.jsonl"
SPEC_DIR = ROOT / "memory" / "monitor_specs"
PROMPT_PATH = ROOT / "config" / "prompts" / "monitor_compiler_prompt.txt"
DEFAULTS_PATH = ROOT / "config" / "monitor_defaults.json"

load_dotenv(ROOT / ".env")

MODEL = os.getenv("DAKOTA_PLANNER_MODEL", "gpt-5.4-mini")
TIMEZONE_DEFAULT = "America/Los_Angeles"


class ChannelEmail(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    enabled: bool = False
    time_local: Optional[str] = Field(default=None, alias="send_time_local")

class ChannelTelegram(BaseModel):
    enabled: bool = False


class Delivery(BaseModel):
    daily_email: ChannelEmail = Field(default_factory=ChannelEmail)
    telegram_breaking: ChannelTelegram = Field(default_factory=ChannelTelegram)


class ImportanceThresholds(BaseModel):
    telegram_breaking_min: int = 85
    digest_include_min: int = 55

    @field_validator("telegram_breaking_min", "digest_include_min")
    @classmethod
    def validate_range(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("thresholds must be between 0 and 100")
        return value


class InitialBrief(BaseModel):
    enabled: bool = True
    refinement_passes: int = 2

    @field_validator("refinement_passes")
    @classmethod
    def validate_passes(cls, value: int) -> int:
        return max(1, min(5, value))


class Budget(BaseModel):
    max_external_spend_usd: float = 25.0


class QueryPrompts(BaseModel):
    bootstrap_query: str
    monitor_query: str
    digest_query: str


class MonitorSpec(BaseModel):
    monitor_id: str
    title: str
    topic: str
    user_request: str
    timezone: str = TIMEZONE_DEFAULT
    created_at: str
    duration_days: int = 7
    start_date_local: str
    end_date_local: str
    monitor_mode: Literal["quick", "balanced", "deep"] = "balanced"
    check_frequency_minutes: int = 60
    delivery: Delivery = Field(default_factory=Delivery)
    importance_thresholds: ImportanceThresholds = Field(default_factory=ImportanceThresholds)
    initial_brief: InitialBrief = Field(default_factory=InitialBrief)
    budget: Budget = Field(default_factory=Budget)
    watch_axes: List[str] = Field(default_factory=list)
    breaking_criteria: List[str] = Field(default_factory=list)
    query_prompts: QueryPrompts

    @field_validator("monitor_id")
    @classmethod
    def monitor_id_sane(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_\\-]+", value):
            raise ValueError("monitor_id must contain lowercase letters, digits, underscore or hyphen")
        return value

    @field_validator("topic")
    @classmethod
    def topic_sane(cls, value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9_\\-]+", "_", value).strip("_")
        return value or "general_monitor"

    @field_validator("check_frequency_minutes")
    @classmethod
    def freq_sane(cls, value: int) -> int:
        if value < 15:
            return 15
        if value > 1440:
            return 1440
        return value


def load_defaults() -> Dict[str, Any]:
    if DEFAULTS_PATH.exists():
        return json.loads(DEFAULTS_PATH.read_text())
    return {}


def load_prompt() -> str:
    return PROMPT_PATH.read_text()


def now_local() -> datetime:
    # Keep it simple and aligned with your setup; this machine is already on local time.
    return datetime.now()


def infer_end_date(now: datetime, request: str) -> tuple[str, str, int]:
    text = request.lower()
    days = 7
    m = re.search(r"next\s+(\d+)\s+days?", text)
    if m:
        days = int(m.group(1))
    start = now
    end = now + timedelta(days=days)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), days


def stable_topic_from_request(request: str) -> str:
    text = request.lower()
    if "peru" in text and "election" in text:
        return "peru_elections"
    if "marbella" in text:
        return "marbella"
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    parts = [p for p in slug.split("_") if p not in {"the", "and", "for", "with", "via", "once", "daily"}]
    return "_".join(parts[:4]) or "general_monitor"


def build_messages(request: str) -> list[dict[str, str]]:
    now = now_local()
    system_prompt = load_prompt().format(
        current_date=now.strftime("%Y-%m-%d"),
        current_time=now.strftime("%H:%M:%S"),
    )
    defaults = load_defaults()
    start_dt, end_dt, duration_days = infer_end_date(now, request)
    seed = {
        "default_timezone": defaults.get("timezone", TIMEZONE_DEFAULT),
        "default_monitor_mode": defaults.get("monitor_mode", "balanced"),
        "default_check_frequency_minutes": defaults.get("check_frequency_minutes", 60),
        "default_importance_thresholds": defaults.get("importance_thresholds", {
            "telegram_breaking_min": 85,
            "digest_include_min": 55,
        }),
        "default_initial_brief": defaults.get("initial_brief", {"enabled": True, "refinement_passes": 2}),
        "default_budget": defaults.get("budget", {"max_external_spend_usd": 25}),
        "precomputed": {
            "suggested_topic": stable_topic_from_request(request),
            "start_date_local": start_dt,
            "end_date_local": end_dt,
            "duration_days": duration_days,
        }
    }
    user_prompt = json.dumps({
        "user_request": request,
        "defaults_and_hints": seed,
        "required_output_fields": [
            "monitor_id", "title", "topic", "user_request", "timezone", "created_at",
            "duration_days", "start_date_local", "end_date_local", "monitor_mode",
            "check_frequency_minutes", "delivery", "importance_thresholds",
            "initial_brief", "budget", "watch_axes", "breaking_criteria", "query_prompts"
        ]
    }, ensure_ascii=False)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def append_usage(query: str, usage: Any) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    record = {
        "timestamp": datetime.now().isoformat(),
        "kind": "openai_monitor_compile",
        "provider": "openai",
        "model": MODEL,
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "cached_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0),
        "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0),
        "query": query,
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def call_model(request: str) -> str:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    resp = client.responses.create(
        model=MODEL,
        input=build_messages(request),
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
        append_usage(request, usage)
    return resp.output_text


def normalize_spec(data: dict, request: str) -> dict:
    now = now_local()
    defaults = load_defaults()
    start_dt, end_dt, duration_days = infer_end_date(now, request)

    data.setdefault("user_request", request)
    data.setdefault("created_at", now.isoformat(timespec="seconds"))
    data.setdefault("timezone", defaults.get("timezone", TIMEZONE_DEFAULT))
    data.setdefault("duration_days", duration_days)
    data.setdefault("start_date_local", start_dt)
    data.setdefault("end_date_local", end_dt)
    data.setdefault("monitor_mode", defaults.get("monitor_mode", "balanced"))
    data.setdefault("check_frequency_minutes", defaults.get("check_frequency_minutes", 60))
    data.setdefault("delivery", {})
    data.setdefault("importance_thresholds", defaults.get("importance_thresholds", {}))
    data.setdefault("initial_brief", defaults.get("initial_brief", {}))
    data.setdefault("budget", defaults.get("budget", {}))
    data.setdefault("watch_axes", [])
    data.setdefault("breaking_criteria", [])
    data.setdefault("topic", stable_topic_from_request(request))

    title = data.get("title") or request[:80]
    data["title"] = title.strip()
    if not data.get("monitor_id"):
        stamp = now.strftime("%Y%m%d")
        data["monitor_id"] = f"{data['topic']}_{stamp}"

    if not data.get("query_prompts"):
        topic = data["topic"].replace("_", " ")
        data["query_prompts"] = {
            "bootstrap_query": f"Build a strong initial brief for {topic}. Explain the current state, top actors, key uncertainties, and what should be monitored over the requested period.",
            "monitor_query": f"What changed for {topic} since the last Dakota run? Focus only on new facts, confirmed changes, and official statements.",
            "digest_query": f"Summarize the meaningful developments for {topic} over the last 24 hours for an email digest. Separate major changes from background noise."
        }
    return data


def save_spec(spec: MonitorSpec) -> Path:
    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    path = SPEC_DIR / f"{spec.monitor_id}.json"
    with open(path, "w") as f:
        json.dump(json.loads(spec.model_dump_json()), f, indent=2, ensure_ascii=False)
    return path


def main() -> int:
    if len(sys.argv) < 2:
        print('Usage: python scripts/dakota_compile_monitor.py "your monitor request"')
        return 1

    request = " ".join(sys.argv[1:]).strip()
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is missing in .env")
        return 1

    print("== Request ==")
    print(request)

    raw = call_model(request)
    print("\n== Raw Model Output ==")
    print(raw)

    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"\nFailed to parse JSON: {e}")
        return 1

    data = normalize_spec(data, request)

    try:
        spec = MonitorSpec.model_validate(data)
    except ValidationError as e:
        print("\nValidation failed:")
        print(e)
        return 1

    path = save_spec(spec)

    print("\n== Validated Monitor Spec ==")
    print(spec.model_dump_json(indent=2))
    print(f"\n== Saved ==\n{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
