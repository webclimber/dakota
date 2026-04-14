
m __future__ import annotations

import json
import os
import smtplib
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

BASE = Path.home() / "dakota"
LOG = BASE / "logs" / "usage.jsonl"
REPORTS = BASE / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

cutoff = datetime.now() - timedelta(days=1)
rows = []
if LOG.exists():
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        ts = datetime.fromisoformat(item["ts"])
        if ts >= cutoff:
            rows.append(item)

by_model = Counter()
by_provider = Counter()
latency = defaultdict(list)
remote_cost = 0.0

for r in rows:
    by_model[r.get("model", "unknown")] += 1
    by_provider[r.get("provider", "unknown")] += 1
    if "latency_ms" in r:
        latency[r.get("model", "unknown")].append(r["latency_ms"])
    remote_cost += float(r.get("estimated_cost_usd", 0) or 0)

lines = []
lines.append(f"Dakota Daily Usage Report — {datetime.now().strftime('%Y-%m-%d')}")
lines.append("")
lines.append(f"Runs in last 24h: {len(rows)}")
lines.append(f"Estimated remote spend: ${remote_cost:,.4f}")
lines.append("")
lines.append("By provider:")
for k, v in by_provider.most_common():
    lines.append(f"  {k:20} {v:>5}")
lines.append("")
lines.append("By model:")
for k, v in by_model.most_common():
    avg = sum(latency[k]) / len(latency[k]) if latency[k] else 0
    lines.append(f"  {k:20} runs={v:>4} avg_latency_ms={avg:>8.1f}")

body = "\n".join(lines)
outfile = REPORTS / f"usage-report-{datetime.now().strftime('%Y-%m-%d')}.txt"
outfile.write_text(body, encoding="utf-8")

host = os.getenv("SMTP_HOST", "")
port = int(os.getenv("SMTP_PORT", "587"))
user = os.getenv("SMTP_USERNAME", "")
password = os.getenv("SMTP_PASSWORD", "")
mail_from = os.getenv("USAGE_REPORT_FROM", "")
mail_to = os.getenv("USAGE_REPORT_TO", "")
starttls = os.getenv("SMTP_STARTTLS", "true").lower() == "true"

if host and mail_from and mail_to:
    msg = MIMEText(body)
    msg["Subject"] = f"Dakota Daily Usage Report {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = mail_from
    msg["To"] = mail_to

    with smtplib.SMTP(host, port) as s:
        if starttls:
            s.starttls()
        if user:
            s.login(user, password)
        s.send_message(msg)
