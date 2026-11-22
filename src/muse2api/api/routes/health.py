from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ... import __version__
from ...services.container import Services
from ..deps import get_services

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@router.get("/readyz")
async def readyz(svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    driver = await svc.driver.health()
    pool = svc.pool.stats()
    ready = driver.get("ok", False) and (pool["available"] > 0 or not svc.driver.requires_account)
    return {"ready": ready, "driver": driver, "accounts": pool}
