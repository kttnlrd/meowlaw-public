#!/usr/bin/env python3
"""
Monthly cron: backup → scan → log → act.

Runs as the first-of-month cron job. Steps:
  1. Database backup (pg_dump all cadena DBs + legislation-explorer data)
  2. Scan acts (legislation.gov.au OData → new compilations)
  3. Scan cases (AustLII → new tax cases)
  4. Scan rulings (ATO website → new/amended rulings)
  5. Log all changes found (change log output)
  6. If changes: ingest, sync JSON, rebuild search index, restart server
  7. Record version in data_version_registry

Usage:
  python3 scripts/monthly_update.py          # full run (backup + scan + act)
  python3 scripts/monthly_update.py --dry-run # scan only, log changes, no action
  python3 scripts/monthly_update.py --scan-only # scan + log, skip backup + action
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BACKEND = ROOT / "backend"
HERMES_SCRIPTS = Path.home() / ".hermes" / "scripts"
VENV_PYTHON = BACKEND / "venv" / "bin" / "python"
LOG_DIR = Path.home() / "logs"

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"monthly_update_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE)),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("monthly_update")

# Module imports for data_version_registry
sys.path.insert(0, str(BACKEND))
from services.data_version_registry import create_version  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def run_script(script_name: str, timeout: int = 600) -> dict:
    """Run a Python script in the backend venv, return structured output."""
    script_path = SCRIPTS / script_name
    if not script_path.exists():
        return {"exit": -1, "output": {}, "stderr": f"Script not found: {script_path}"}
    try:
        p = subprocess.run(
            [str(VENV_PYTHON), str(script_path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "exit": -1,
            "output": {"_timeout": True, "script": script_name, "timeout_s": timeout},
            "stdout_preview": (e.stdout or "")[:500],
            "stderr": f"Timeout after {timeout}s: {script_name}",
        }
    # Both checkers emit json.dumps(..., indent=2). The old line-scan broke on
    # the first bare "{" line, so every checker run appeared to return {}.
    out = {}
    stdout = p.stdout or ""
    start = stdout.find("{")
    if start != -1:
        try:
            out = json.loads(stdout[start:])
        except json.JSONDecodeError as e:
            out = {"_parse_error": str(e), "_stdout_tail": stdout[-300:]}
    return {
        "exit": p.returncode,
        "output": out,
        "stdout_preview": p.stdout[:500],
        "stderr": p.stderr[:500],
    }


def run_shell(script_name: str, timeout: int = 600) -> dict:
    """Run a shell script."""
    script_path = SCRIPTS / script_name
    if not script_path.exists():
        return {"exit": -1, "output": "", "stderr": f"Script not found: {script_path}"}
    p = subprocess.run(
        ["bash", str(script_path)],
        capture_output=True, text=True, timeout=timeout,
    )
    return {
        "exit": p.returncode,
        "output": p.stdout,
        "stderr": p.stderr[:500],
    }


# ── Change log builder ───────────────────────────────────────────────────────

def build_change_log(
    legislation_result: dict,
    rulings_result: dict,
    cases_result: dict,
) -> list[dict]:
    """Build a unified change log from all scanner results."""
    changes = []

    # Legislation changes
    for r in legislation_result.get("results", []):
        if r.get("has_changes"):
            changes.append({
                "source": r.get("source", "legislation"),
                "action": "updated",
                "item": r.get("act_name", "Unknown act"),
                "detail": (
                    f"Compilation changed: "
                    f"local={r.get('local', {}).get('compilation_no', '?')} "
                    f"remote={r.get('remote', {}).get('compilation_no', '?')}"
                ),
            })

    # Ruling changes
    for r in rulings_result.get("results", []):
        for n in r.get("new", []):
            changes.append({
                "source": f"rulings_{r.get('type', 'unknown')}",
                "action": "new",
                "item": f"{n.get('type', '')} {n.get('year', '')}/{n.get('num', '')}",
                "detail": n.get("title", ""),
            })
        for a in r.get("amended", []):
            changes.append({
                "source": f"rulings_{r.get('type', 'unknown')}",
                "action": "amended",
                "item": f"{a.get('type', '')} {a.get('year', '')}/{a.get('num', '')}",
                "detail": "Content hash changed",
            })

    # Case changes (from the ingest script output)
    if cases_result.get("exit") == 0:
        # Parse the ingest script output for new cases
        for line in cases_result.get("stdout_preview", "").split("\n"):
            if "new" in line.lower() and ("case" in line.lower() or "added" in line.lower()):
                changes.append({
                    "source": "cases",
                    "action": "new",
                    "item": line.strip(),
                    "detail": "",
                })

    return changes


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Monthly legislation update cron")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only scan and log changes, take no action")
    parser.add_argument("--scan-only", action="store_true",
                        help="Skip backup, only scan and log")
    args = parser.parse_args()

    t0 = time.time()
    log.info("=" * 60)
    log.info("MONTHLY UPDATE START")
    log.info("=" * 60)
    log.info("Log file: %s", LOG_FILE)
    if args.dry_run:
        log.info("DRY RUN MODE — no actions will be taken")
    if args.scan_only:
        log.info("SCAN ONLY MODE — skipping backup")

    # ── Step 1: Database backup ──────────────────────────────────────────────
    if not args.scan_only:
        log.info("")
        log.info("─" * 40)
        log.info("STEP 1: Database backup")
        log.info("─" * 40)
        backup_result = run_shell("db_backup.sh", timeout=600)
        if backup_result["exit"] != 0:
            log.error("Backup failed! Aborting monthly update.")
            log.error("Stderr: %s", backup_result["stderr"])
            sys.exit(1)
        log.info("Backup completed successfully.")
        for line in backup_result["output"].split("\n"):
            if line.strip():
                log.info("  %s", line.strip())
    else:
        log.info("STEP 1: Skipped (--scan-only)")

    # ── Step 2: Scan legislation ─────────────────────────────────────────────
    log.info("")
    log.info("─" * 40)
    log.info("STEP 2: Scan legislation updates")
    log.info("─" * 40)
    leg_result = run_script("check_legislation_updates.py", timeout=120)
    if leg_result["exit"] != 0:
        log.warning("Legislation check failed (exit=%d): %s",
                     leg_result["exit"], leg_result["stderr"])
    else:
        leg_data = leg_result.get("output", {})
        n_changed = leg_data.get("acts_changed", 0)
        n_checked = leg_data.get("acts_checked", 0)
        log.info("Acts checked: %d, Changed: %d", n_checked, n_changed)
        for r in leg_data.get("results", []):
            if r.get("has_changes"):
                log.info("  ✦ %s: %s → %s (compilation %s)",
                         r.get("act_name", "?"),
                         r.get("local", {}).get("compilation_no", "?"),
                         r.get("remote", {}).get("compilation_no", "?"),
                         r.get("remote", {}).get("compilation_date", "?"))
                for am in r.get("amending_acts", []):
                    log.info("      Amending act: %s", am.get("name", "?"))

    # ── Step 3: Scan cases ───────────────────────────────────────────────────
    log.info("")
    log.info("─" * 40)
    log.info("STEP 3: Scan for new tax cases")
    log.info("─" * 40)
    case_script = HERMES_SCRIPTS / "monthly_case_ingest.py"
    p = subprocess.run(
        [str(VENV_PYTHON), str(case_script)],
        capture_output=True, text=True, timeout=1200,
    )
    cases_result = {
        "exit": p.returncode,
        "output": {},
        "stdout_preview": p.stdout[:1000],
        "stderr": p.stderr[:500],
    }
    if cases_result["exit"] != 0:
        log.warning("Case check failed (exit=%d): %s",
                     cases_result["exit"], cases_result["stderr"])
    else:
        log.info("Case check completed.")
        for line in cases_result.get("stdout_preview", "").split("\n"):
            if line.strip():
                log.info("  %s", line.strip())

    # ── Step 4: Scan rulings ─────────────────────────────────────────────────
    log.info("")
    log.info("─" * 40)
    log.info("STEP 4: Scan for new/amended ATO rulings")
    log.info("─" * 40)
    rul_result = run_script("check_ruling_updates.py", timeout=600)
    if rul_result["exit"] != 0:
        log.warning("Ruling check failed (exit=%d): %s",
                     rul_result["exit"], rul_result["stderr"])
    else:
        rul_data = rul_result.get("output", {})
        n_new = rul_data.get("total_new_rulings", 0)
        n_amended = rul_data.get("total_amended_rulings", 0)
        n_checked = rul_data.get("total_checked", 0)
        log.info("Rulings checked: %d, New: %d, Amended: %d",
                 n_checked, n_new, n_amended)
        if n_new:
            log.info("  New rulings by type:")
            for r in rul_data.get("results", []):
                for n in r.get("new", []):
                    log.info("    ✦ %s %s/%s — %s",
                             n.get("type", ""), n.get("year", ""),
                             n.get("num", ""), n.get("title", "")[:80])
        if n_amended:
            log.info("  Amended rulings:")
            for r in rul_data.get("results", []):
                for a in r.get("amended", []):
                    log.info("    ▲ %s %s/%s — hash changed",
                             a.get("type", ""), a.get("year", ""),
                             a.get("num", ""))

    # ── Step 5: Build change log ─────────────────────────────────────────────
    log.info("")
    log.info("─" * 40)
    log.info("STEP 5: Change log")
    log.info("─" * 40)
    changes = build_change_log(leg_result.get("output", {}),
                               rul_result.get("output", {}),
                               cases_result)
    if not changes:
        log.info("No changes detected. Skipping action steps.")
    else:
        log.info("%d changes detected:", len(changes))
        for c in changes:
            log.info("  [%s] %s - %s", c["action"], c["item"], c["detail"])

    # ── Step 6: Act on changes ───────────────────────────────────────────────
    if args.dry_run or not changes:
        log.info("")
        if args.dry_run:
            log.info("DRY RUN — skipping action steps (backup, version, rebuild, restart)")
        else:
            log.info("No changes to act on.")
    else:
        log.info("")
        log.info("─" * 40)
        log.info("STEP 6: Acting on changes")
        log.info("─" * 40)

        # Build source updates for version registry
        source_updates = []

        # Legislation
        leg_data = leg_result.get("output", {})
        source_updates.append({
            "source": "legislation",
            "added": leg_data.get("acts_changed", 0),
            "modified": 0,
        })

        # Rulings
        rul_data = rul_result.get("output", {})
        source_updates.append({
            "source": "rulings",
            "added": rul_data.get("total_new_rulings", 0),
            "modified": rul_data.get("total_amended_rulings", 0),
        })

        # Cases
        source_updates.append({
            "source": "cases",
            "added": 0,
            "modified": 0,
        })

        # Save version record
        summary = f"{len(changes)} items updated: " + \
                  f"{sum(1 for c in changes if c['action']=='new')} new, " + \
                  f"{sum(1 for c in changes if c['action']=='updated' or c['action']=='amended')} modified"
        version = create_version(summary=summary, changes=changes, source_updates=source_updates)
        log.info("Version recorded: %s", version)

        # Rebuild search index
        log.info("Rebuilding search index...")
        rebuild = run_script("rebuild_search_index.py", timeout=300)
        if rebuild["exit"] == 0:
            log.info("Search index rebuilt.")
        else:
            log.warning("Search index rebuild failed: %s", rebuild["stderr"])

        # Restart server
        log.info("Restarting legislation-explorer server...")
        restart = subprocess.run(
            ["systemctl", "--user", "restart", "legislation-explorer"],
            capture_output=True, text=True, timeout=30,
        )
        if restart.returncode == 0:
            log.info("Server restarted.")
        else:
            log.warning("Server restart failed: %s", restart.stderr)

    # ── Done ─────────────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    log.info("")
    log.info("=" * 60)
    log.info("MONTHLY UPDATE COMPLETE (%.0fs)", elapsed)
    log.info("=" * 60)
    log.info("Log file: %s", LOG_FILE)

    # Print summary to stdout for the cron job to capture
    summary = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_seconds": round(elapsed, 2),
        "dry_run": args.dry_run,
        "steps": {
            "backup": not args.scan_only,
            "legislation_checked": leg_result.get("output", {}).get("acts_checked", 0),
            "legislation_changed": leg_result.get("output", {}).get("acts_changed", 0),
            "cases_checked": True if cases_result.get("exit") == 0 else False,
            "rulings_checked": rul_result.get("output", {}).get("total_checked", 0),
            "rulings_new": rul_result.get("output", {}).get("total_new_rulings", 0),
            "rulings_amended": rul_result.get("output", {}).get("total_amended_rulings", 0),
        },
        "total_changes": len(changes),
        "changes": changes,
        "version": locals().get("version", None),
    }
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()