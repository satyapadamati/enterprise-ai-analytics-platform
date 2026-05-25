import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health", summary="Health check")
async def health_check() -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@router.get("/health/ready", summary="Readiness probe")
async def readiness_check() -> JSONResponse:
    """Kubernetes-style readiness probe."""
    return JSONResponse(
        status_code=200,
        content={"status": "ready"},
    )
