"""Workflow API — procedural knowledge endpoints.

- GET /api/workflows                    list loaded workflows
- GET /api/workflows/detect?q=...       topic detection
- GET /api/workflows/entry?workflow=ID  entry node slices
- GET /api/workflows/node?workflow=ID&node=NODE  current node slice
- GET /api/workflows/status             registry health / validation errors
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from backend.services.content_access import gate
from backend.services.workflow_registry import get_registry

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(gate("workflows"))])


@router.get("/api/workflows")
def list_workflows():
    reg = get_registry()
    return {
        "workflows": [
            {
                "id": wf.id,
                "name": wf.name,
                "area": wf.area,
                "version": wf.version,
                "nodes": len(wf.nodes),
                "entry": wf.entry,
                "rules": wf.rules,
            }
            for wf in reg.workflows.values()
        ],
        "errors": reg.errors,
    }


@router.get("/api/workflows/detect")
def detect_workflows(q: str = Query(..., description="Query text")):
    reg = get_registry()
    hits = reg.detect(q)
    return {
        "query": q,
        "workflows": hits,
        "entries": {wf_id: reg.entry_slices(wf_id) for wf_id in hits},
    }


@router.get("/api/workflows/entry")
def workflow_entry(workflow: str = Query(...)):
    reg = get_registry()
    slices = reg.entry_slices(workflow)
    if slices is None:
        return {"error": f"unknown workflow: {workflow}"}
    return {"workflow": workflow, "entry": slices}


@router.get("/api/workflows/node")
def workflow_node(
    workflow: str = Query(...),
    node: str = Query(...),
):
    reg = get_registry()
    sl = reg.node_slice(workflow, node)
    if sl is None:
        return {"error": f"unknown workflow/node: {workflow}/{node}"}
    return {"workflow": workflow, "slice": sl}


@router.get("/api/workflows/status")
def workflow_status():
    reg = get_registry()
    return {
        "loaded": sorted(reg.workflows.keys()),
        "validation_errors": reg.errors,
        "workflows_dir": str(reg.workflows_dir),
    }
