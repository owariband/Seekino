"""Compatibility imports for HTTP contracts, grouped by their UI resource."""
from __future__ import annotations

from ..model.vo.commonVO import ApiError, Failure, FieldError, ImageAsset, Page, UnixTimeMs
from ..model.dto.libraryDTO import Character, SeriesCreateRequest
from ..model.vo.libraryVO import SeriesDetail, SeriesSummary, SeriesView, VideoSummary
from ..model.vo.uploadVO import UploadCompleteView, UploadSegmentView, UploadSessionView
from ..model.dto.uploadDTO import UploadSessionCreateRequest
from ..model.vo.jobVO import JobCounts, JobSummary, ParseJobView, VideoChunkCheckpointView
from ..model.vo.searchVO import SearchHitView, SearchResultView, VideoChunkEvidenceView
from ..model.dto.searchDTO import VideoSearchRequest
from ..model.dto.agentDTO import (
    AgentConversationCreateRequest,
    AgentMessageSendRequest,
    EvidenceReference,
)
from ..model.vo.agentVO import (
    AgentConversationView,
    AgentMessageView,
    AgentMessageSendView,
    AgentMessageEventView,
    AgentAnswerEventView,
    AgentAnswerStartedView,
    AgentAnswerProgressView,
    AgentAnswerEvidenceView,
    AgentAnswerDeltaView,
    AgentAnswerCompletedView,
    AgentAnswerFailedView,
    AgentAnswerInterruptedView,
)
from ..model.vo.systemVO import HealthView

__all__ = [name for name in globals() if not name.startswith("_") and name != "annotations"]
