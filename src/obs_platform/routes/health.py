from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from obs_platform.database import DatabaseUnavailableError


router = APIRouter()


@router.get("/health", response_model=None)
async def health_check(request: Request):
    check_database = getattr(request.app.state, "check_database", None)
    if check_database is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "checks": {"database": "error"},
                "detail": "database check is not configured",
            },
        )

    try:
        await check_database()
    except DatabaseUnavailableError as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "checks": {"database": "error"},
                "detail": str(exc),
            },
        )

    return {"status": "ok", "checks": {"database": "ok"}}
