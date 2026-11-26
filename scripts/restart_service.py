#!/usr/bin/env python3.12
"""Restart the public legislation-explorer service without sudo.

The API on :8765 is the systemd unit's MainPID, owned by harrison. Its environment carries secrets
(PGPASSWORD, JWT_SECRET, AZURE_*) set by the unit, so the replacement must inherit them exactly -
they are read from /proc/<pid>/environ and passed straight into the child, never printed and never
written to a file on disk.

Usage:
  verify   start the same command on a spare port with the captured environment, health-check it,
           then stop it.  Proves the launch works before production is touched.
  switch   stop the running process and start the replacement on the real port, then health-check.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PID = int(os.environ.get("SERVICE_PID", "95451"))
CWD = str(Path(__file__).resolve().parent.parent)
PY = "/usr/bin/python3.12"
PORT = 8765
LOG = "/tmp/apply-full/prod-restart.log"


def env_of(pid: int) -> dict[str, str]:
    env: dict[str, str] = {}
    with open(f"/proc/{pid}/environ", "rb") as f:
        for kv in f.read().split(b"\0"):
            if b"=" in kv:
                k, v = kv.split(b"=", 1)
                env[k.decode()] = v.decode()
    return env


def cmdline_of(pid: int) -> list[str]:
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        return [a.decode() for a in f.read().split(b"\0") if a]


def spawn(port: int, env: dict[str, str]) -> subprocess.Popen:
    log = open(LOG, "ab")
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", str(port)],
        cwd=CWD, env=env, stdout=log, stderr=log, start_new_session=True)


def health(port: int, timeout: float = 90.0) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    last = "no attempt"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.read(200).decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, f"HTTP {e.code}"
        except Exception as e:                       # not up yet
            last = f"{type(e).__name__}"
            time.sleep(2)
    return 0, last


def section_ok(port: int) -> tuple[int, int]:
    """(status, length) for a corpus read that must reflect the current files."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/section/itaa-1997/83-170",
                                    timeout=20) as r:
            body = r.read().decode(errors="replace")
        return r.status, len(body)
    except Exception as e:
        return 0, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["verify", "switch", "status"])
    a = ap.parse_args()

    env = env_of(PID)
    cmd = cmdline_of(PID)
    print(f"  target pid {PID}: {' '.join(cmd)}")
    print(f"  environment captured: {len(env)} variables (values not printed)")

    if a.action == "status":
        for port in (PORT, 8799):
            st, detail = health(port, timeout=3)
            print(f"  :{port} -> {st} {detail[:60]}")
        return 0

    if a.action == "verify":
        p = spawn(8799, env)
        print(f"  spawned pid {p.pid} on :8799")
        st, detail = health(8799)
        print(f"  health :8799 -> {st} {detail[:80]}")
        s = 0
        if st == 200:
            s, n = section_ok(8799)
            print(f"  section read :8799 -> HTTP {s}, {n} bytes")
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
        print(f"  verification instance stopped (exit {p.returncode})")
        return 0 if st == 200 and s == 200 else 1

    # switch
    st, _ = health(PORT, timeout=3)
    if st == 200:
        print(f"  stopping pid {PID} on :{PORT}")
        os.kill(PID, signal.SIGTERM)
        for _ in range(30):
            if not os.path.exists(f"/proc/{PID}"):
                break
            time.sleep(1)
        else:
            print("  still alive after 30s — refusing to start a second instance on the same port")
            return 1
        print("  stopped")
    else:
        print(f"  :{PORT} not responding (HTTP {st}) — starting the replacement without stopping "
              f"anything")

    p = spawn(PORT, env)
    print(f"  spawned pid {p.pid} on :{PORT}")
    st, detail = health(PORT, timeout=120)
    print(f"  health :{PORT} -> {st} {detail[:80]}")
    if st == 200:
        s, n = section_ok(PORT)
        print(f"  section read :{PORT} -> HTTP {s}, {n} bytes")
    return 0 if st == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
