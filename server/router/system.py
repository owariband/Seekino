from __future__ import annotations

from fastapi import APIRouter

from ..api import system
from ..model.vo.systemVO import HealthView

router = APIRouter()
router.add_api_route("/healthz", system.health, methods=["GET"], response_model=HealthView)
