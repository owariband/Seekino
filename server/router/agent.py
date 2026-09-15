from __future__ import annotations

from fastapi import APIRouter

from ..api import agent
from ..api.schema import (
    AgentConversationView,
    AgentMessageSendView,
    AgentMessageView,
    AgentSessionView,
)

router = APIRouter()
router.add_api_route(
    "/api/agent/sessions/create",
    agent.create_agent_session,
    methods=["POST"],
    response_model=AgentSessionView,
    status_code=201,
)
router.add_api_route(
    "/api/agent/sessions/list",
    agent.list_agent_sessions,
    methods=["GET"],
    response_model=list[AgentSessionView],
)
router.add_api_route(
    "/api/agent/sessions/get",
    agent.get_agent_session,
    methods=["GET"],
    response_model=AgentSessionView,
)
router.add_api_route(
    "/api/agent/conversations/create",
    agent.create_agent_conversation,
    methods=["POST"],
    response_model=AgentConversationView,
    status_code=201,
)
router.add_api_route(
    "/api/agent/conversations/list",
    agent.list_agent_conversations,
    methods=["GET"],
    response_model=list[AgentConversationView],
)
router.add_api_route(
    "/api/agent/conversations/get",
    agent.get_agent_conversation,
    methods=["GET"],
    response_model=AgentConversationView,
)
router.add_api_route(
    "/api/agent/messages/list",
    agent.list_agent_messages,
    methods=["GET"],
    response_model=list[AgentMessageView],
)
router.add_api_route(
    "/api/agent/messages/send",
    agent.send_agent_message,
    methods=["POST"],
    response_model=AgentMessageSendView,
)
router.add_api_route(
    "/api/agent/messages/events",
    agent.stream_agent_messages,
    methods=["GET"],
)
