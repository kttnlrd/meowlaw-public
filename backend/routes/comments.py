from __future__ import annotations

import logging

from fastapi import HTTPException, APIRouter, Request
from pydantic import BaseModel

from backend.auth import require_admin
from backend.services.comments_service import get_comments, add_comment, resolve_comment

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_user(request: Request) -> dict | None:
    """Return user dict if authenticated, None otherwise."""
    try:
        from backend.auth import require_user
        return require_user(request)
    except Exception:
        return None




class CommentCreate(BaseModel):
    act: str
    section: str
    text: str


class CommentResolve(BaseModel):
    comment_id: int


@router.get("/api/comments/{act}/{section}")
def list_comments(act: str, section: str, request: Request, limit: int = 100, offset: int = 0):
    """Comments are public to read — no auth required."""
    try:
        comments = get_comments(act, section, limit, offset)
        return {"act": act, "section": section, "count": len(comments), "comments": comments}
    except Exception:
        logger.exception("Error fetching comments")
        raise HTTPException(status_code=500, detail="Failed to fetch comments")


@router.post("/api/comments")
def create_comment(payload: CommentCreate, request: Request):
    """Requires auth — author is pulled from session, not user-supplied."""
    user = _require_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required to comment")
    author = user.get("name") or user.get("email", "Unknown")
    try:
        return add_comment(payload.act, payload.section, author, payload.text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Error creating comment")
        raise HTTPException(status_code=500, detail="Failed to create comment")


@router.post("/api/comments/resolve")
def mark_resolved(payload: CommentResolve, request: Request):
    """Admin only.

    ``comments`` stores only a display ``author`` name — there is no user id or
    email to compare against — so author-or-admin ownership can't be enforced.
    """
    require_admin(request)
    try:
        resolve_comment(payload.comment_id)
        return {"ok": True}
    except Exception:
        logger.exception("Error resolving comment")
        raise HTTPException(status_code=500, detail="Failed to resolve comment")
