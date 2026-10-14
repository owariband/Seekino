from __future__ import annotations

from fastapi import APIRouter

from ..api import agent
from ..model.vo.commonVO import Page
from ..api.schema import (
    AgentConversationView,
    AgentGlobalSettingsView,
    AgentMessageOptionsView,
    AgentMessageSendView,
    AgentMessageView,
)

router = APIRouter()
router.add_api_route(
    "/api/agent/global-settings",
    agent.get_agent_global_settings,
    methods=["GET"],
    response_model=AgentGlobalSettingsView,
)
router.add_api_route(
    "/api/agent/global-settings",
    agent.patch_agent_global_settings,
    methods=["PATCH"],
    response_model=AgentGlobalSettingsView,
)
router.add_api_route(
    "/api/agent/messages/options",
    agent.get_agent_message_options,
    methods=["GET"],
    response_model=AgentMessageOptionsView,
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
    response_model=Page[AgentConversationView],
)
router.add_api_route(
    "/api/agent/conversations/get",
    agent.get_agent_conversation,
    methods=["GET"],
    response_model=AgentConversationView,
)
router.add_api_route(
    "/api/agent/conversations/delete",
    agent.delete_agent_conversation,
    methods=["POST"],
    response_model=AgentConversationView,
)
router.add_api_route(
    "/api/agent/messages/list",
    agent.list_agent_messages,
    methods=["GET"],
    response_model=Page[AgentMessageView],
)
router.add_api_route(
    "/api/agent/messages/get",
    agent.get_agent_message,
    methods=["GET"],
    response_model=AgentMessageSendView,
)
router.add_api_route(
    "/api/agent/messages/cancel",
    agent.cancel_agent_message,
    methods=["POST"],
    response_model=AgentMessageSendView,
)
router.add_api_route(
    "/api/agent/messages/send",
    agent.send_agent_message,
    methods=["POST"],
    response_model=AgentMessageSendView,
    responses={
        200: {
            "description": "JSON message pair, or live answer events when Accept is text/event-stream.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
    },
)
router.add_api_route(
    "/api/agent/messages/events",
    agent.stream_agent_messages,
    methods=["GET"],
)
