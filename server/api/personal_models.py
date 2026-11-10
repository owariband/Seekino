from uuid import UUID

from fastapi import Depends, HTTPException, Query, Request

from ..model.dto.agentDTO import AgentConversationModelRequest
from ..model.dto.personal_model import DiscoveredModels, ModelConnectionEndpoint, ModelDefaultSave, ModelRoleSave, PersonalModelConnectionSave, PersonalModelSettingsView
from ..service.model_settings import ModelSettings, PersonalModelNotFoundError
from .context import get_services, require_config_access


def settings_service(request: Request, _=Depends(require_config_access)) -> ModelSettings:
    service = get_services(request).model_settings
    if service is None:
        raise HTTPException(503, "模型设置暂时不可用")
    return service


def list_connections(service: ModelSettings = Depends(settings_service)) -> PersonalModelSettingsView:
    return service.catalog().settings()


def save_connection(body: PersonalModelConnectionSave, connection_id: UUID = Query(alias="connectionId"),
                    service: ModelSettings = Depends(settings_service)) -> PersonalModelSettingsView:
    return service.save(str(connection_id), body)


def delete_connection(connection_id: UUID = Query(alias="connectionId"), revision: int = Query(ge=1),
                      service: ModelSettings = Depends(settings_service)) -> PersonalModelSettingsView:
    return service.delete(str(connection_id), revision)


def set_default(body: ModelDefaultSave, service: ModelSettings = Depends(settings_service)) -> PersonalModelSettingsView:
    return service.set_default(body.model_id, body.revision)


def test_connection(body: AgentConversationModelRequest, connection_id: UUID = Query(alias="connectionId"),
                    service: ModelSettings = Depends(settings_service)) -> dict[str, bool]:
    from gateway.personal_models import PersonalModelRequestError, probe_personal_model
    catalog = service.catalog()
    binding = catalog.models.get(body.model_id) or catalog.find(body.model_id)
    if not binding or not binding.option.available or not binding.option.testable or binding.connection.connection_id != str(connection_id):
        raise PersonalModelNotFoundError()
    try:
        if "chat" in binding.option.roles:
            probe_personal_model(binding.target(environ=service.environ), allow_local=service.allow_local)
        else:
            from gateway.model_probe import probe_registered_model
            probe_registered_model(catalog.config, binding.binding, allow_local=service.allow_local, environ=service.environ)
    except PersonalModelRequestError as exc:
        raise HTTPException(502, str(exc)) from None
    except Exception:
        raise HTTPException(502, "未收到有效的模型回复，请检查协议、地址、API Key 和模型 ID") from None
    return {"ok": True}

def save_role(body: ModelRoleSave, service: ModelSettings = Depends(settings_service)) -> PersonalModelSettingsView:
    return service.assign(body.role, body.model_id, body.revision)


def discover_models(body: ModelConnectionEndpoint, connection_id: UUID = Query(alias="connectionId"),
                    service: ModelSettings = Depends(settings_service)) -> DiscoveredModels:
    return DiscoveredModels(model_ids=service.discover(str(connection_id), body))
