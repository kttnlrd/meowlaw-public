"""Generate rulings_list.json for the graph endpoint."""
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.services.data_loader import load_rulings

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "rulings" / "rulings_list.json"

print("Loading rulings...")
rulings = load_rulings()
print(f"Loaded {len(rulings)} rulings")

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(rulings, f, indent=2, ensure_ascii=False)

print(f"Written to {OUTPUT} ({OUTPUT.stat().st_size / 1024 / 1024:.1f} MB)")