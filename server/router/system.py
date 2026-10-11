from __future__ import annotations

from fastapi import APIRouter

from ..api import system
from ..model.vo.systemVO import HealthView, RuntimeConfigView

router = APIRouter()
router.add_api_route("/api/system/runtime-config", system.runtime_config, methods=["GET"], response_model=RuntimeConfigView)
router.add_api_route("/healthz", system.health, methods=["GET"], response_model=HealthView)
