#!/usr/bin/env python3
"""Monitor batch summary processes. Silent unless there's a problem."""
import os, glob, re, sys

LOGS = {
    "FCA 1/5": "/tmp/fca_batch_1.log",
    "FCA 2/5": "/tmp/fca_batch_2.log",
    "FCA 3/5": "/tmp/fca_batch_3.log",
    "FCA 4/5": "/tmp/fca_batch_4.log",
    "FCA 5/5": "/tmp/fca_batch_5.log",
    "HCA":     "/tmp/hca_batch.log",
    "FCAFC":   "/tmp/fcafc_batch.log",
    "Reprocess": "/tmp/reprocess_log.txt",
}

problems = []
completed = []
for name, logfile in LOGS.items():
    if not os.path.exists(logfile):
        problems.append(f"{name}: no log file")
        continue
    
    with open(logfile) as f:
        lines = f.readlines()
    
    if not lines:
        problems.append(f"{name}: log empty — may not have started")
        continue
    
    # Get status from last line
    last = lines[-1].strip()
    
    # Check for failures
    fail_count = 0
    empty_count = 0
    ok_count = 0
    total = 0
    last_progress = ""
    
    for line in lines:
        m = re.search(r'❌.*?(\d+)', line)
        if m and 'To process' not in line:
            fail_count = max(fail_count, int(m.group(1)))
        m = re.search(r'⚠️.*?(\d+)', line)
        if m and 'To process' not in line:
            empty_count = max(empty_count, int(m.group(1)))
        m = re.search(r'✅.*?(\d+)', line)
        if m and 'To process' not in line:
            ok_count = max(ok_count, int(m.group(1)))
        if 'DONE' in line:
            total = ok_count
    
    # Extract progress from last line
    prog = re.search(r'\[(\d+)/(\d+)\]', last)
    if prog:
        current, total = int(prog.group(1)), int(prog.group(2))
        pct = current / total * 100 if total > 0 else 0
        if fail_count > 0:
            problems.append(f"{name}: {current}/{total} ({pct:.0f}%) — {fail_count} failures")
        elif empty_count > 0:
            problems.append(f"{name}: {current}/{total} ({pct:.0f}%) — {empty_count} empty docs")
        else:
            last_progress = f"✓ {name}: {current}/{total} ({pct:.0f}%)"
    elif 'DONE' in last or 'DONE' in lines[-2] if len(lines) > 1 else False:
        completed.append(f"✓ {name}: COMPLETED ({ok_count} OK, {fail_count} failed)")
    else:
        # Check if process finished (last line is summary)
        if 'DONE' in last:
            completed.append(f"✓ {name}: COMPLETED")
        else:
            problems.append(f"{name}: no progress detected")

# Output — silent if nothing wrong
if problems:
    print("🔴 BATCH ISSUES")
    for p in problems:
        print(f"  {p}")
if completed:
    print("🟢 BATCHES COMPLETE")
    for c in completed:
        print(f"  {c}")
if not problems and not completed:
    print("🟢 All batches running clean")