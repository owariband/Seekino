from __future__ import annotations

from fastapi import APIRouter

from ..api import auth
from ..model.vo.authVO import AuthIdentityView, EmailCodeSendView

router = APIRouter()
router.add_api_route("/api/auth/me", auth.get_me, methods=["GET"], response_model=AuthIdentityView)
router.add_api_route("/api/auth/guest", auth.create_guest, methods=["POST"], response_model=AuthIdentityView)
router.add_api_route("/api/auth/email/send", auth.send_email_code, methods=["POST"], response_model=EmailCodeSendView)
router.add_api_route("/api/auth/email/verify", auth.verify_email_code, methods=["POST"], response_model=AuthIdentityView)
router.add_api_route("/api/auth/logout", auth.logout, methods=["POST"], response_model=AuthIdentityView)
