import sys
from pathlib import Path

report_dir = Path("reports")

files = sorted(report_dir.glob("run-*.md"), reverse=True)
if not files:
    print("No Dakota reports found.")
    raise SystemExit(1)

if len(sys.argv) == 1:
    target = files[0]
else:
    name = sys.argv[1]
    matches = [f for f in files if name in f.name]
    if not matches:
        print(f"No report found matching: {name}")
        raise SystemExit(1)
    target = matches[0]

print(target)
print("=" * 80)
print(target.read_text())

