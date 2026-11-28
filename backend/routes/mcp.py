"""API routes for MCP token management."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.config import is_admin_email
from backend.mcp_token_manager import token_manager

router = APIRouter()


class CreateTokenRequest(BaseModel):
    name: str = ""


class RenameTokenRequest(BaseModel):
    name: str


def _get_user(request: Request) -> dict | None:
    """The signed-in user (session cookie), or None."""
    return getattr(request.state, "user", None)


def _can_manage_token(user: dict, token_id: str) -> str | None:
    """Return an error message if `user` cannot manage this token, else None.

    Only the token's creator (or an admin) may revoke/rename it — prevents any
    signed-in user from revoking another user's token by sequential id.
    """
    if is_admin_email(user["email"]):
        return None
    owner = token_manager.owner_by_id_or_hash(token_id)
    if owner is None:
        return "Token not found"
    if owner != user["email"]:
        return "You can only manage your own tokens"
    return None


@router.post("/api/mcp-token")
def create_mcp_token(request: Request, body: CreateTokenRequest):
    """Create a new named MCP access token. Requires authentication.

    MCP is free during beta — every signed-in user can generate tokens.
    """
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Login required to create MCP tokens"}, status_code=401)
    token = token_manager.create_token(name=body.name[:200], created_by=user["email"])
    return {"token": token, "message": "Copy this token now. It will not be shown again."}


@router.get("/api/mcp-hall-of-fame")
def mcp_hall_of_fame(request: Request):
    """Leaderboard of MCP call counts by token name, over all-time/monthly/daily windows."""
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Login required"}, status_code=401)
    return token_manager.hall_of_fame()


@router.get("/api/mcp-tokens")
def list_mcp_tokens(request: Request):
    """List active MCP tokens for the current user. Requires authentication."""
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Login required"}, status_code=401)
    return {"tokens": token_manager.list_tokens(created_by=user["email"])}


@router.post("/api/mcp-tokens/{token_id}/revoke")
def revoke_mcp_token(token_id: str, request: Request) -> JSONResponse:
    """Revoke an MCP token. Requires authentication and ownership (or admin)."""
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Login required"}, status_code=401)
    err = _can_manage_token(user, token_id)
    if err:
        return JSONResponse({"error": err}, status_code=403 if err != "Token not found" else 404)
    revoked = token_manager.revoke_token(token_id)
    if revoked:
        return JSONResponse({"message": "Token revoked"})
    return JSONResponse({"error": "Token not found or already revoked"}, status_code=404)


@router.post("/api/mcp-tokens/{token_id}/rename")
def rename_mcp_token(token_id: str, body: RenameTokenRequest, request: Request) -> JSONResponse:
    """Rename an MCP token. Requires authentication and ownership (or admin)."""
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Login required"}, status_code=401)
    err = _can_manage_token(user, token_id)
    if err:
        return JSONResponse({"error": err}, status_code=403 if err != "Token not found" else 404)
    renamed = token_manager.rename_token(token_id, body.name[:200])
    if renamed:
        return JSONResponse({"message": "Token renamed"})
    return JSONResponse({"error": "Token not found"}, status_code=404)
