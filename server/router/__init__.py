from __future__ import annotations

from fastapi import APIRouter

from .agent import router as agent_router
from .jobs import router as jobs_router
from .library import router as library_router
from .search import router as search_router
from .system import router as system_router
from .uploads import router as uploads_router

router = APIRouter()
router.include_router(system_router)
router.include_router(library_router)
router.include_router(uploads_router)
router.include_router(jobs_router)
router.include_router(search_router)
router.include_router(agent_router)

__all__ = ["router"]
