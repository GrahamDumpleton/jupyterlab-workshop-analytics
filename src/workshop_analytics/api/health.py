"""`GET /healthz`: liveness, with no authentication."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Answer that the process is up."""

    return {"status": "ok"}
