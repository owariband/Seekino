from uuid import UUID

from fastapi import HTTPException, Query, Request

from ..model.dto.agentDTO import AgentConversationModelRequest
from ..model.dto.personal_model import PersonalModelConnectionSave, PersonalModelSettingsView
from ..service.personal_models import PersonalModelConnections
from .context import get_identity, get_services


def _context(request: Request) -> tuple[str, PersonalModelConnections]:
    identity = get_identity(request)
    if identity.mode == "local" and (not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}):
        raise HTTPException(403, "请从本机管理个人模型连接")
    service = get_services(request).personal_models
    if service is None:
        raise HTTPException(503, "个人模型设置暂时不可用")
    return identity.user_id, service


def list_connections(request: Request) -> PersonalModelSettingsView:
    owner, service = _context(request)
    return service.list(owner)


def save_connection(request: Request, body: PersonalModelConnectionSave, connection_id: UUID = Query(alias="connectionId")) -> PersonalModelSettingsView:
    owner, service = _context(request)
    return service.save(owner, str(connection_id), body)


def delete_connection(request: Request, connection_id: UUID = Query(alias="connectionId"), revision: int = Query(ge=1)) -> PersonalModelSettingsView:
    owner, service = _context(request)
    return service.delete(owner, str(connection_id), revision)


def set_default(request: Request, body: AgentConversationModelRequest) -> PersonalModelSettingsView:
    owner, service = _context(request)
    return service.set_default(owner, body.model_id)


def test_connection(request: Request, body: AgentConversationModelRequest, connection_id: UUID = Query(alias="connectionId")) -> dict[str, bool]:
    owner, service = _context(request)
    from gateway.personal_models import PersonalModelRequestError, probe_personal_model

    target = service.resolve(owner, body.model_id, connection_id=str(connection_id))
    try:
        probe_personal_model(target, allow_local=service.allow_local)
    except PersonalModelRequestError as exc:
        raise HTTPException(502, str(exc)) from None
    except Exception:
        raise HTTPException(502, "未收到有效的模型回复，请检查协议、地址、API Key 和模型 ID") from None
    return {"ok": True}
