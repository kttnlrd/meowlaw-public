"""Scan legislation section markdown files for formatting issues."""
from pathlib import Path
import re

sections_dir = Path(__file__).resolve().parent.parent / "data" / "itaa-1997" / "sections"
files = sorted(sections_dir.rglob("*.md"))

total = len(files)
issues = {
    "poor_quality": [],
    "no_heading": [],
    "single_line_body": [],
    "inline_bullets": [],
    "no_line_breaks": [],
}

for f in files:
    content = f.read_text(encoding="utf-8", errors="replace")

    # Split frontmatter from body
    if content.startswith("---"):
        parts = content.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else content.strip()
    else:
        body = content.strip()

    lines = [l for l in body.split("\n") if l.strip()]
    if not lines:
        continue

    # Check 1: No # heading
    has_heading = any(l.strip().startswith("#") for l in lines)

    # Check 2: Single line body (heading + one content line)
    single_line = len(lines) <= 2

    # Check 3: Inline bullet characters
    inline_bullets = bool(re.search(r"[•▪●]", body))

    # Check 4: Fewer than 3 double-newlines (no paragraph breaks)
    para_breaks = body.count("\n\n")
    no_breaks = para_breaks < 3

    score = 0
    reasons = []
    if not has_heading:
        score += 1
        reasons.append("no_heading")
    if single_line:
        score += 1
        reasons.append("single_line")
    if inline_bullets:
        score += 1
        reasons.append("inline_bullets")
    if no_breaks:
        score += 1
        reasons.append("no_breaks")

    if score >= 2:
        issues["poor_quality"].append((f.name, reasons, len(body)))
    elif single_line:
        issues["single_line_body"].append(f.name)
    if not has_heading and score < 2:
        issues["no_heading"].append(f.name)
    if inline_bullets and score < 2:
        issues["inline_bullets"].append(f.name)
    if no_breaks and score < 2:
        issues["no_line_breaks"].append(f.name)

print(f"Total section files: {total}")
print()
print(f"Poor quality (multiple issues): {len(issues['poor_quality'])}")
for name, reasons, size in sorted(issues["poor_quality"])[:20]:
    print(f"  {name}: {reasons} ({size} chars)")
if len(issues["poor_quality"]) > 20:
    print(f"  ... and {len(issues['poor_quality']) - 20} more")

print(f"\nNo # heading (isolated): {len(issues['no_heading'])}")
print(f"Single line body (isolated): {len(issues['single_line_body'])}")
print(f"Inline bullet chars (isolated): {len(issues['inline_bullets'])}")
print(f"Few paragraph breaks (isolated): {len(issues['no_line_breaks'])}")

# Show worst examples
print("\n=== Worst examples ===")
for name, reasons, size in sorted(issues["poor_quality"], key=lambda x: -x[2])[:5]:
    fp = list(sections_dir.rglob(name))
    if fp:
        content = fp[0].read_text(encoding="utf-8", errors="replace")
        # Show the body (after frontmatter)
        if content.startswith("---"):
            parts = content.split("---", 2)
            body = parts[2][:300] if len(parts) >= 3 else content[:300]
        else:
            body = content[:300]
        print(f"\n  {name} ({size} chars, reasons: {reasons})")
        print(f"  Body preview: {body[:200]}")