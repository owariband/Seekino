"""Compatibility imports for HTTP contracts, grouped by their UI resource."""
from __future__ import annotations

from ..model.vo.commonVO import ApiError, Failure, FieldError, ImageAsset, Page, UnixTimeMs
from ..model.dto.libraryDTO import Character, SeriesCreateRequest
from ..model.vo.libraryVO import SeriesDetail, SeriesSummary, SeriesView, VideoSummary
from ..model.vo.uploadVO import UploadCompleteView, UploadSegmentView, UploadSessionView
from ..model.dto.uploadDTO import UploadReparseRequest, UploadSessionCreateRequest
from ..model.vo.jobVO import VideoParseTaskView
from ..model.vo.searchVO import SearchHitView, SearchResultView, VideoChunkEvidenceView
from ..model.dto.searchDTO import VideoSearchRequest
from ..model.dto.agentDTO import (
    AgentConversationCreateRequest,
    AgentGlobalSettingsPatchRequest,
    AgentMessageSendRequest,
    EvidenceReference,
)
from ..model.vo.agentVO import (
    AgentConversationView,
    AgentGlobalSettingsView,
    AgentMessageOptionsView,
    AgentMessageView,
    AgentMessageSendView,
    AgentMessageEventView,
    AgentAnswerEventView,
    AgentAnswerStartedView,
    AgentAnswerProgressView,
    AgentAnswerEvidenceView,
    AgentAnswerBlockView,
    AgentAnswerDeltaView,
    AgentAnswerCompletedView,
    AgentAnswerFailedView,
    AgentAnswerInterruptedView,
    AgentMarkdownRenderBlockView,
    AgentVideoRenderBlockView,
)
from ..model.vo.systemVO import HealthView

__all__ = [name for name in globals() if not name.startswith("_") and name != "annotations"]
