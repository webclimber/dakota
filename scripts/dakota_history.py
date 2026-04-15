from pathlib import Path

report_dir = Path("reports")
files = sorted(report_dir.glob("run-*.md"), reverse=True)

if not files:
    print("No Dakota reports found.")
    raise SystemExit(0)

for f in files:
    print(f.name)

