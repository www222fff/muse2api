from fastapi import APIRouter, Depends

from ... import __version__
from ...services.container import Services
from ..deps import get_services

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/readyz")
async def readyz(svc: Services = Depends(get_services)) -> dict:
    driver = await svc.driver.health()
    # Clients (e.g. MuseStudio) read this to know whether frame chaining gives continuity.
    driver["capabilities"] = {"video_first_frame": svc.driver.capabilities.video_first_frame}
    pool = svc.pool.stats()
    ready = driver.get("ok", False) and (pool["available"] > 0 or not svc.driver.requires_account)
    return {"ready": ready, "driver": driver, "accounts": pool}
