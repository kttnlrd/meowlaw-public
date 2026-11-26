#!/usr/bin/env python3
"""Watchdog for embed_private_rulings.py — guards against row deletion.

Samples the embeddings DB count for source_type='ruling' act='private' and
alerts on the three failure signatures:

  EMBED-DROP   count DECREASED between samples (the wipe signature — a run
               deleting rows it should preserve; killed the DB once already)
  EMBED-STALL  count unchanged for > stall_min (run hung or died silently)
  EMBED-DEAD   the embed process is not running while target is unfinished

Alerts are written to stdout as single-line ALERT markers (consumed by
Hermes watch_patterns), appended to a log file, and a sentinel file is
touched so cron/systemd can pick it up too. Alerts fire once per state
change, not continuously.

Usage:
  python3 scripts/monitor_embed.py                 # defaults
  python3 scripts/monitor_embed.py --interval 30 --stall-min 10 --target 245000
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import DATA_DIR  # noqa: E402

DB = DATA_DIR / "embeddings.db"
LOG = DATA_DIR / "embed_monitor.log"
SENTINEL = Path("/tmp/embed_monitor_alert")
QUERY = "SELECT count(*) FROM embeddings WHERE source_type='ruling' AND act='private'"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def embed_proc_alive() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "embed_private_rulings.py"],
            capture_output=True, text=True, timeout=10,
        )
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return True  # can't tell — don't false-alarm


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--interval", type=int, default=30, help="sample interval seconds")
    ap.add_argument("--stall-min", type=int, default=10, help="minutes of no growth before EMBED-STALL")
    ap.add_argument("--target", type=int, default=0, help="expected final chunk count (0 = unknown, ETA skipped)")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    prev = None
    last_change = time.time()
    alerted: set[str] = set()
    log(f"monitor start | db={args.db} | interval={args.interval}s | stall={args.stall_min}min | target={args.target or 'unknown'}")

    while True:
        try:
            cur = conn.execute(QUERY).fetchone()[0]
        except Exception as e:
            log(f"EMBED-ERR db query failed: {e}")
            time.sleep(args.interval)
            continue
        now = time.time()
        alive = embed_proc_alive()

        if prev is not None:
            delta = cur - prev
            rate = delta / args.interval * 3600
            eta = ""
            if args.target and rate > 0 and cur < args.target:
                eta = f" | eta={(args.target - cur) / rate * 3600 / 60:.0f}min"
            status = "ok"
            if delta < 0:
                status = f"DROP {-delta}"
            elif delta == 0:
                status = "flat"
            log(f"rows={cur} delta={delta:+d} rate={rate:.0f}/hr{eta} proc={'up' if alive else 'DOWN'} [{status}]")

            # --- anomaly detection ---
            if delta < 0:
                key = "EMBED-DROP"
                msg = (f"{key} | rows={cur} (was {prev}) | dropped {-delta} in {args.interval}s | "
                       "embed run is DELETING rows — kill it now: pkill -f embed_private_rulings")
                if key not in alerted:
                    log(msg)
                    SENTINEL.write_text(msg)
                    alerted.add(key)
                last_change = now
            elif cur == prev:
                stalled_for = (now - last_change) / 60
                if stalled_for >= args.stall_min and "EMBED-STALL" not in alerted:
                    msg = (f"EMBED-STALL | rows={cur} unchanged for {stalled_for:.0f}min | "
                           f"proc={'up' if alive else 'DOWN'} | check: ps aux | grep embed_private")
                    log(msg)
                    SENTINEL.write_text(msg)
                    alerted.add("EMBED-STALL")
            else:
                last_change = now
                alerted.discard("EMBED-STALL")

            if not alive and "EMBED-DEAD" not in alerted:
                msg = (f"EMBED-DEAD | rows={cur} | no embed_private_rulings.py process | "
                       "if target unfinished, restart: backend/venv/bin/python -u scripts/embed_private_rulings.py")
                log(msg)
                SENTINEL.write_text(msg)
                alerted.add("EMBED-DEAD")
            elif alive:
                alerted.discard("EMBED-DEAD")
        else:
            log(f"rows={cur} (baseline)")

        prev = cur
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
