"""Agent construction for user-owned model endpoints."""
from __future__ import annotations

from typing import TYPE_CHECKING
from langchain_core.messages import HumanMessage

from .agent_model.context import ContextWindowPolicy
from .openai.structured import OpenAIStructuredToolAgentModel
from .personal_chat import _PersonalChatModel, PersonalModelRequestError
from .personal_endpoint import PersonalModelTarget, personal_http_client

if TYPE_CHECKING:
    from .agent_model.deepagents import DeepAgentsSupervisorModel, DeepAgentSessionStore


class _PersonalStructured(OpenAIStructuredToolAgentModel):
    def close(self) -> None:
        self._chat_model.close()


def build_personal_supervisor(target: PersonalModelTarget, *, session_store: DeepAgentSessionStore,
                              request_timeout_seconds: float, max_output_tokens: int, max_input_chars: int,
                              context_window_policy: ContextWindowPolicy, total_timeout_enabled: bool,
                              allow_local: bool) -> DeepAgentsSupervisorModel:
    from .agent_model.deepagents import DeepAgentsSupervisorModel

    class _PersonalSupervisor(DeepAgentsSupervisorModel):
        def close(self) -> None:
            self._chat_model.close()

    http = personal_http_client(target, allow_local=allow_local, timeout=request_timeout_seconds)
    try:
        return _PersonalSupervisor(
            session_store=session_store, model=target.model_id, provider_name=target.protocol,
            request_timeout_seconds=request_timeout_seconds, max_input_chars=max_input_chars,
            context_window_policy=context_window_policy, total_timeout_enabled=total_timeout_enabled,
            supports_vision_input=target.supports_vision, chat_model=_PersonalChatModel(target, http, max_output_tokens),
        )
    except BaseException:
        http.close()
        raise


def build_personal_structured(target: PersonalModelTarget, *, request_timeout_seconds: float,
                              max_output_tokens: int, max_input_chars: int,
                              context_window_policy: ContextWindowPolicy,
                              allow_local: bool) -> OpenAIStructuredToolAgentModel:
    http = personal_http_client(target, allow_local=allow_local, timeout=request_timeout_seconds)
    try:
        return _PersonalStructured(
            model=target.model_id, provider_name=target.protocol,
            request_timeout_seconds=request_timeout_seconds,
            max_output_tokens=max_output_tokens, max_input_chars=max_input_chars,
            context_window_policy=context_window_policy,
            supports_vision_input=target.supports_vision,
            chat_model=_PersonalChatModel(target, http, max_output_tokens),
        )
    except BaseException:
        http.close()
        raise


def probe_personal_model(target: PersonalModelTarget, *, allow_local: bool) -> None:
    with personal_http_client(target, allow_local=allow_local, timeout=15) as http:
        result = _PersonalChatModel(target, http, 64)._generate([HumanMessage("Reply with OK.")])
        if not result.generations or not result.generations[0].message.text.strip():
            raise PersonalModelRequestError("模型没有返回文本")
