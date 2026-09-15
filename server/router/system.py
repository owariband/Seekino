from __future__ import annotations

from fastapi import APIRouter

from ..api import system

router = APIRouter()
router.add_api_route("/healthz", system.health, methods=["GET"])
