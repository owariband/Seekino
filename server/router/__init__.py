from __future__ import annotations

from fastapi import APIRouter, Depends

from ..api.context import get_identity
from .auth import router as auth_router

from .agent import router as agent_router
from .jobs import router as jobs_router
from .library import router as library_router
from .media import router as media_router
from .search import router as search_router
from .system import router as system_router
from .uploads import router as uploads_router

router = APIRouter()
router.include_router(system_router)
router.include_router(auth_router)
for business_router in (library_router, media_router, uploads_router, jobs_router, search_router, agent_router):
    router.include_router(business_router, dependencies=[Depends(get_identity)])

__all__ = ["router"]
