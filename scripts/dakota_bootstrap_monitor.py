#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv
from openai import OpenAI


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(read_text(path))


def save_json(path: Path, data: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def append_usage_log(project_root: Path, record: Dict[str, Any]) -> None:
    log_path = project_root / "logs" / "usage.jsonl"
    ensure_dir(log_path.parent)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def list_topic_reports(project_root: Path, topic: str) -> List[Path]:
    report_dir = project_root / "reports"
    return sorted(report_dir.glob(f"run-*-{topic}.md"), key=lambda p: p.stat().st_mtime)


def run_dakota_research(project_root: Path, topic: str, query: str) -> Tuple[str, Path]:
    before = {p.resolve() for p in list_topic_reports(project_root, topic)}
    cmd = [sys.executable, "scripts/dakota_research.py", topic, query]
    proc = subprocess.run(
        cmd,
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        raise RuntimeError(f"dakota_research.py failed with code {proc.returncode}\n{combined}")

    after = list_topic_reports(project_root, topic)
    candidates = [p for p in after if p.resolve() not in before]
    if not candidates:
        # Fallback: newest topic report
        if not after:
            raise RuntimeError("dakota_research.py completed but no topic report was found.")
        report_path = after[-1]
    else:
        report_path = max(candidates, key=lambda p: p.stat().st_mtime)

    return combined, report_path


def load_prompt_template(path: Path) -> str:
    return read_text(path).strip()


def call_openai_json(client: OpenAI, model: str, prompt: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    resp = client.responses.create(
        model=model,
        input=prompt,
    )

    usage = getattr(resp, "usage", None)
    usage_record = {}
    if usage:
        usage_record = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cached_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0),
            "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0),
        }

    raw = resp.output_text.strip()
    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Failed to parse JSON from model output.\nError: {e}\nRaw output:\n{raw[:2000]}")
    return data, usage_record


def build_prompt(template: str, variables: Dict[str, str]) -> str:
    text = template
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def critique_email(subject: str, body_markdown: str, report_markdown: str) -> str:
    return f"""Review this bootstrap email against the source report.
Return one short paragraph describing the main weaknesses, or 'Looks good' if none.

EMAIL SUBJECT:
{subject}

EMAIL BODY:
{body_markdown}

SOURCE REPORT:
{report_markdown[:12000]}
""".strip()


def main() -> None:
    load_dotenv()

    if len(sys.argv) != 2:
        print("Usage: python scripts/dakota_bootstrap_monitor.py <path-to-monitor-spec.json>")
        raise SystemExit(1)

    spec_path = Path(sys.argv[1]).expanduser().resolve()
    project_root = Path(__file__).resolve().parents[1]

    if not spec_path.exists():
        raise SystemExit(f"Spec file not found: {spec_path}")

    spec = load_json(spec_path)
    monitor_id = spec["monitor_id"]
    topic = spec["topic"]
    title = spec.get("title", topic)
    bootstrap_query = spec["query_prompts"]["bootstrap_query"]
    refinement_passes = int(spec.get("initial_brief", {}).get("refinement_passes", 1) or 1)

    out_dir = project_root / "reports" / "monitors" / monitor_id
    ensure_dir(out_dir)

    print("== Monitor Spec ==")
    print(spec_path)
    print(f"monitor_id: {monitor_id}")
    print(f"topic:      {topic}")
    print(f"title:      {title}")
    print(f"bootstrap:  {bootstrap_query}")
    print(f"out_dir:    {out_dir}")

    print("\n== Running Dakota Research ==")
    research_output, report_path = run_dakota_research(project_root, topic, bootstrap_query)
    write_text(out_dir / "bootstrap_research_stdout.txt", research_output)
    print(research_output.strip())
    print(f"\n== Full report found ==\n{report_path}")

    report_markdown = read_text(report_path)
    write_text(out_dir / "bootstrap_report.md", report_markdown)

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("DAKOTA_DISCOVERY_MODEL", "gpt-5.4-mini")

    email_template_path = project_root / "config" / "prompts" / "bootstrap_email_prompt.txt"
    state_template_path = project_root / "config" / "prompts" / "bootstrap_state_prompt.txt"

    email_template = load_prompt_template(email_template_path)
    state_template = load_prompt_template(state_template_path)

    variables = {
        "monitor_id": monitor_id,
        "title": title,
        "topic": topic,
        "user_request": spec["user_request"],
        "bootstrap_query": bootstrap_query,
        "watch_axes": "\n".join(f"- {x}" for x in spec.get("watch_axes", [])),
        "breaking_criteria": "\n".join(f"- {x}" for x in spec.get("breaking_criteria", [])),
        "report_markdown": report_markdown[:18000],
        "now_iso": datetime.now().isoformat(timespec="seconds"),
    }

    print("\n== Generating Email Brief ==")
    email_prompt = build_prompt(email_template, variables)
    email_data, email_usage = call_openai_json(client, model, email_prompt)
    append_usage_log(project_root, {
        "timestamp": datetime.now().isoformat(),
        "kind": "openai_bootstrap_email",
        "provider": "openai",
        "model": model,
        "monitor_id": monitor_id,
        "topic": topic,
        "query": bootstrap_query,
        **email_usage,
    })

    critique_notes = []
    revised_email_data = email_data

    for _ in range(max(0, refinement_passes - 1)):
        critique_prompt = critique_email(
            subject=revised_email_data.get("subject", ""),
            body_markdown=revised_email_data.get("body_markdown", ""),
            report_markdown=report_markdown,
        )
        critique_resp = client.responses.create(model=model, input=critique_prompt)
        critique = critique_resp.output_text.strip()
        critique_notes.append(critique)
        append_usage_log(project_root, {
            "timestamp": datetime.now().isoformat(),
            "kind": "openai_bootstrap_email_critique",
            "provider": "openai",
            "model": model,
            "monitor_id": monitor_id,
            "topic": topic,
            "query": bootstrap_query,
            "input_tokens": getattr(getattr(critique_resp, "usage", None), "input_tokens", None),
            "output_tokens": getattr(getattr(critique_resp, "usage", None), "output_tokens", None),
            "total_tokens": getattr(getattr(critique_resp, "usage", None), "total_tokens", None),
            "cached_tokens": getattr(getattr(getattr(critique_resp, "usage", None), "input_tokens_details", None), "cached_tokens", 0),
            "reasoning_tokens": getattr(getattr(getattr(critique_resp, "usage", None), "output_tokens_details", None), "reasoning_tokens", 0),
        })
        revise_prompt = f"""
Revise the bootstrap email using the critique below.
Return JSON only:
{{
  "subject": "...",
  "body_markdown": "..."
}}

CRITIQUE:
{critique}

CURRENT SUBJECT:
{revised_email_data.get("subject","")}

CURRENT BODY:
{revised_email_data.get("body_markdown","")}

SOURCE REPORT:
{report_markdown[:14000]}
""".strip()
        revised_email_data, revise_usage = call_openai_json(client, model, revise_prompt)
        append_usage_log(project_root, {
            "timestamp": datetime.now().isoformat(),
            "kind": "openai_bootstrap_email_revise",
            "provider": "openai",
            "model": model,
            "monitor_id": monitor_id,
            "topic": topic,
            "query": bootstrap_query,
            **revise_usage,
        })

    print("\n== Generating Bootstrap State ==")
    state_prompt = build_prompt(state_template, variables)
    state_data, state_usage = call_openai_json(client, model, state_prompt)
    append_usage_log(project_root, {
        "timestamp": datetime.now().isoformat(),
        "kind": "openai_bootstrap_state",
        "provider": "openai",
        "model": model,
        "monitor_id": monitor_id,
        "topic": topic,
        "query": bootstrap_query,
        **state_usage,
    })

    email_md = f"# {revised_email_data.get('subject', title)}\n\n{revised_email_data.get('body_markdown', '').strip()}\n"
    write_text(out_dir / "bootstrap_email.md", email_md)

    email_json = {
        "monitor_id": monitor_id,
        "subject": revised_email_data.get("subject", title),
        "body_markdown": revised_email_data.get("body_markdown", "").strip(),
        "critique_notes": critique_notes,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_report": str(report_path.relative_to(project_root)),
    }
    save_json(out_dir / "bootstrap_email.json", email_json)
    save_json(out_dir / "bootstrap_state.json", state_data)

    manifest = {
        "monitor_id": monitor_id,
        "spec_path": str(spec_path),
        "topic": topic,
        "title": title,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "files": {
            "bootstrap_report": "bootstrap_report.md",
            "bootstrap_email_markdown": "bootstrap_email.md",
            "bootstrap_email_json": "bootstrap_email.json",
            "bootstrap_state_json": "bootstrap_state.json",
            "bootstrap_research_stdout": "bootstrap_research_stdout.txt",
        },
    }
    save_json(out_dir / "manifest.json", manifest)

    print("\n== Saved ==")
    print(out_dir / "bootstrap_report.md")
    print(out_dir / "bootstrap_email.md")
    print(out_dir / "bootstrap_email.json")
    print(out_dir / "bootstrap_state.json")
    print(out_dir / "manifest.json")

    print("\n== Email Subject ==")
    print(email_json["subject"])


if __name__ == "__main__":
    main()
