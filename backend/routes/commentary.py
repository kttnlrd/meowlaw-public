"""CCH commentary (admin-gated), served via subprocess bridge to LES."""
from __future__ import annotations

import json
import logging
import os
import subprocess

from fastapi import APIRouter, Depends, HTTPException

from backend.services.content_access import gate

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(gate("commentary"))])

_LES_DIR = os.environ.get("LEGISLATION_EXPLORER_DIR", "")
_BRIDGE = os.path.join(_LES_DIR, "backend", "bridges", "get_commentary.py") if _LES_DIR else ""
_PY = "/usr/bin/python3.12"
_ENV = {"PATH": "/usr/bin:/usr/local/bin", "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": _LES_DIR}


def _call(*args: str) -> dict:
    if not _LES_DIR:
        raise HTTPException(status_code=503, detail="commentary bridge not configured")
    cmd = [_PY, _BRIDGE] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=_ENV,
                           cwd=_LES_DIR)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Commentary lookup timed out")
    if r.returncode != 0:
        logger.error("commentary bridge failed: %s", r.stderr[:300])
        raise HTTPException(status_code=502, detail="Commentary service unavailable")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Invalid commentary response")


@router.get("/api/commentary/{act}/{section}")
async def commentary(act: str, section: str, limit: int = 50, offset: int = 0):
    return _call(act, section, str(limit), str(offset))