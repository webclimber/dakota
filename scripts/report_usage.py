from __future__ import annotations

import json
import os
import smtplib
from collections import Counter
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

BASE = Path.home() / "dakota"
load_dotenv(BASE / ".env")
LOG_FILE = BASE / "logs" / "usage.jsonl"
REPORTS_DIR = BASE / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def load_recent_records(hours: int = 24) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=hours)
    records: list[dict] = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec["ts"])
            if ts >= cutoff:
                records.append(rec)
        except Exception:
            continue
    return records


def build_report(records: list[dict]) -> tuple[str, str]:
    today = datetime.now().strftime("%Y-%m-%d")
    event_counts = Counter(rec.get("event_type", "unknown") for rec in records)
    provider_counts = Counter(rec.get("provider", "unknown") for rec in records)
    lines = []
    lines.append(f"Dakota daily usage report - {today}")
    lines.append("")
    lines.append(f"Events in last 24h: {len(records)}")
    lines.append("")
    lines.append("Event counts:")
    for key, count in sorted(event_counts.items()):
        lines.append(f"  - {key}: {count}")
    lines.append("")
    lines.append("Provider counts:")
    for key, count in sorted(provider_counts.items()):
        lines.append(f"  - {key}: {count}")
    lines.append("")
    lines.append("Recent events:")
    for rec in records[-10:]:
        lines.append(f"  - {rec.get('ts')} | {rec.get('event_type')} | {rec.get('topic', rec.get('event_name', ''))}")
    body = "\n".join(lines)
    report_file = REPORTS_DIR / f"usage-report-{today}.txt"
    report_file.write_text(body + "\n", encoding="utf-8")
    return today, body


def maybe_send_email(subject: str, body: str) -> None:
    to_addr = os.environ.get("USAGE_REPORT_TO", "").strip()
    from_addr = os.environ.get("USAGE_REPORT_FROM", "").strip()
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    use_starttls = os.environ.get("SMTP_STARTTLS", "true").lower() == "true"
    use_ssl = os.environ.get("SMTP_SSL", "false").lower() == "true"

    if not all([to_addr, from_addr, host]):
        print("Email settings incomplete; wrote local report only.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    if use_ssl:
        with smtplib.SMTP_SSL(host, port) as server:
            if username:
                server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            if use_starttls:
                server.starttls()
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(msg)


if __name__ == "__main__":
    records = load_recent_records(hours=24)
    day, body = build_report(records)
    maybe_send_email(f"Dakota daily usage report {day}", body)
    print(body)
