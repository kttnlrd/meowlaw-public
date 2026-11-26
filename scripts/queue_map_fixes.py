#!/usr/bin/env python3
"""Queue pending map-review fixes into the cadena_knowledge issues table.

The daily-bug-squash cron queries `issues` (status NOT IN fixed/resolved) and
fixes them by size. For each map with a completed DeepSeek Pro review and no
.fixed.json marker, insert (or skip if already queued) a 'map'-category issue
whose note points at the review + map files and carries a size hint.

Idempotent: skips tickets already present. Re-run as more reviews complete.
"""
import datetime, json, os, re, subprocess, sys, glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEWS = ROOT / "data" / "map_reviews"

def psql(sql):
    out = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", "cadena_knowledge",
         "-t", "-A", "-c", sql],
        capture_output=True, text=True)
    return out.stdout.strip(), out.stderr.strip()

def existing_tickets():
    out, _ = psql("SELECT ticket FROM issues WHERE category='map';")
    return set(x for x in out.splitlines() if x.strip())

def severity_counts(review):
    sev = {"critical": 0, "major": 0, "minor": 0}
    for i in review.get("issues", []):
        s = i.get("severity", "minor")
        sev[s] = sev.get(s, 0) + 1
    return sev

def main():
    tickets = existing_tickets()
    n = 0
    for f in sorted(REVIEWS.glob("*.review.json")):
        mid = f.name[:-len(".review.json")]
        fixed_marker = REVIEWS / f"{mid}.fixed.json"
        if fixed_marker.exists():
            continue
        review = json.loads(f.read_text())
        if review.get("verdict") == "pass" and not review.get("issues"):
            continue
        sev = severity_counts(review)
        # size hint: rebuild-level structural reviews are LARGE; patch-level are MEDIUM
        if sev["critical"] >= 4 or "map" in [i.get("node_id") for i in review.get("issues", [])] and sev["critical"] >= 2:
            size = "LARGE"
        else:
            size = "MEDIUM"
        ticket = f"MAP-{mid[-12:].upper()}" if False else None
        # deterministic ticket from map id (stable across runs)
        ticket = "MAP-" + re.sub(r"[^A-Z0-9]", "", mid.upper())[-10:]
        if ticket in tickets:
            continue
        note = (f"[{size}] Map review fixes for data/maps/{mid}.json — review: data/map_reviews/{mid}.review.json. "
                f"DeepSeek Pro found {sev['critical']} critical, {sev['major']} major, {sev['minor']} minor issues. "
                f"Method: for every issue, verify against corpus (data/{{act}}/sections/**/*.md frontmatter section/section_title — "
                f"map statute[].title must equal corpus section_title), patch map JSON via python3 json load/modify/dump "
                f"(ensure_ascii=False, indent=1), reject false positives with reason. After patching validate: JSON parses, "
                f"unique node ids, edges resolve, all nodes reachable from 'start'. Write data/map_reviews/{mid}.fixed.json "
                f"{{map_id, fixed_at, issues_applied, issues_rejected, validation}}. Do NOT commit.")
        note_esc = note.replace("'", "''")
        sql = ("INSERT INTO issues (ticket, category, note, created, status) VALUES ("
               f"'{ticket}', 'map', '{note_esc}', '{datetime.date.today().isoformat()}', 'open');")
        psql(sql)
        tickets.add(ticket)
        n += 1
        print(f"queued {ticket}: {mid} [{size}] {sev}")
    print(f"done — {n} new, {len(tickets)} total queued")

if __name__ == "__main__":
    main()
