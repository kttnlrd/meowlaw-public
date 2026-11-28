"""Data version API route — just the GET endpoint."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from backend.services import data_version_registry

router = APIRouter()


@router.get("/api/data-version")
def get_data_version():
    current = data_version_registry.current_version()
    sources = data_version_registry.source_status()
    history = data_version_registry.version_history(limit=10)
    return JSONResponse({
        "current_version": current,
        "sources": sources,
        "version_history": history,
    })
