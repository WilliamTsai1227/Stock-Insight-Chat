"""Authenticated, read-only management endpoints for external MCP servers."""

from uuid import UUID

from fastapi import APIRouter, Depends

from app.backend.module.jwt import get_current_user_id
from app.backend.tools.mcp_client import mcp_client_manager


router = APIRouter(prefix="/api/mcp", tags=["MCP"])


@router.get("/servers")
async def list_mcp_servers(
    _user_id: UUID = Depends(get_current_user_id),
):
    """Return connection state and safe tool metadata, never URLs or headers."""
    return {
        "status": "success",
        "data": await mcp_client_manager.public_status(),
    }


@router.post("/servers/refresh")
async def refresh_mcp_servers(
    _user_id: UUID = Depends(get_current_user_id),
):
    """Reload environment configuration and force tool discovery."""
    mcp_client_manager.reload_config()
    return {
        "status": "success",
        "data": await mcp_client_manager.public_status(force=True),
    }
