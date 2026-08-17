from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
MAP = {
    "examples/identity_self.example.json": "Layer-1/identity/identity_self.json",
    "examples/epis_self.example.json": "Layer-1/identity/epis_self.json",
    "examples/identity_calculated.example.json": "Layer-1/identity/identity_calculated.json",
    "examples/habits.example.json": "Layer-1/habits/habits.json",
    "examples/current_state.example.json": "Layer-1/memory/current_state.json",
    "examples/deadlines.example.json": "Layer-1/memory/deadlines.json",
    "examples/monthly.example.json": "Layer-1/memory/monthly.json",
    "examples/morning_report.example.json": "Layer-1/memory/morning_report.json",
    "examples/pending.example.json": "Layer-1/memory/pending.json",
    "examples/people.example.json": "Layer-1/memory/people.json",
    "examples/phone_daily.example.json": "Layer-1/memory/phone_daily.json",
    "examples/screen_daily.example.json": "Layer-1/memory/screen_daily.json",
    "examples/session_buffer.example.json": "Layer-1/memory/session_buffer.json",
    "examples/weekly.example.json": "Layer-1/memory/weekly.json",
}
for src_rel, dst_rel in MAP.items():
    src, dst = ROOT/src_rel, ROOT/dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copy2(src, dst)
        print(f"created {dst_rel}")
    else:
        print(f"kept existing {dst_rel}")
print("Private runtime files are gitignored.")
