from fastapi import APIRouter

from ..api import personal_models

router = APIRouter(prefix="/api/agent/model-connections")
router.add_api_route("", personal_models.list_connections, methods=["GET"])
router.add_api_route("/default", personal_models.set_default, methods=["PATCH"])
router.add_api_route("", personal_models.save_connection, methods=["PUT"])
router.add_api_route("", personal_models.delete_connection, methods=["DELETE"])
router.add_api_route("/test", personal_models.test_connection, methods=["POST"])
router.add_api_route("/roles", personal_models.save_role, methods=["PATCH"])
router.add_api_route("/discover", personal_models.discover_models, methods=["POST"])
