export type RuntimeMode = "checking" | "live" | "offline";
export type ThemeMode = "light" | "dark";

export type SeriesStatus = "EMPTY" | "INDEXING" | "PARTIAL" | "READY" | "FAILED";
export type UnixTimeMs = number;
export type PlaybackStatus = "PREPARING" | "READY" | "FAILED" | "MISSING";
export type IndexStatus = "NOT_READY" | "SYNCING" | "READY" | "FAILED";
export interface Failure { code: string; message: string }
export interface Page<T> { items: T[]; total: number; hasMore: boolean; nextCursor: string | null }
export interface ImageAsset { imageId: string; url: string; expiresAt: UnixTimeMs | null }
export interface Keyframe extends ImageAsset { atMs: number; alt: string | null }
export type UploadStatus =
  | "UPLOADING"
  | "COMPLETING"
  | "COMPLETED"
  | "EXPIRED";
export type UploadCompletionStage = "VERIFYING" | "ASSEMBLING" | "PROBING" | "CREATING_JOB";
export type ParseJobStatus =
  | "QUEUED"
  | "RUNNING"
  | "RETRY_WAIT"
  | "SUCCEEDED"
  | "FAILED";

export interface ApiErrorView extends Failure {
  requestId: string;
  fieldErrors: Array<{ field: string; message: string }>;
  retryAt: UnixTimeMs | null;
}
export type ApiErrorPayload = Partial<ApiErrorView>;
export interface Character { name: string; aliases: string[] }

export interface VideoSummary {
  videoId: string;
  workspaceId: string;
  seriesId: string;
  episodeOrder: number;
  uploadSequence: number;
  originalFilename: string;
  totalBytes: number;
  durationMs: number;
  uploadStatus: string;
  parseStatus: string;
  uploadProgress: number;
  parseProgress: number | null;
  indexStatus: IndexStatus;
  playbackStatus: PlaybackStatus;
  playbackUrl: string | null;
  playbackUrlExpiresAt: UnixTimeMs | null;
  thumbnail: ImageAsset | null;
  latestJobId: string | null;
  lastError: Failure | null;
}

export interface SeriesSummary {
  seriesId: string;
  workspaceId: string;
  name: string;
  description: string | null;
  status: SeriesStatus;
  videoCount: number;
  readyVideoCount: number;
  failedVideoCount: number;
  pendingUploadCount: number;
  cover: ImageAsset | null;
  lastSearch: { conversationId: string; query: string; searchedAt: UnixTimeMs } | null;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}

export interface SeriesView extends SeriesSummary {
  importantExtraData: Record<string, unknown> | null;
  characterBible: Character[];
  videos: VideoSummary[];
  pendingUploads: UploadSessionView[];
}

export interface SeriesCreateRequest {
  workspaceId: string;
  name: string;
  description?: string | null;
  importantExtraData?: Record<string, unknown> | null;
  characterBible?: Character[];
}

export interface SeriesBackground {
  format: "markdown" | "json";
  content: string;
}

export interface SeriesUpdateRequest {
  workspaceId: string;
  seriesId: string;
  description?: string | null;
  importantExtraData?: SeriesBackground | null;
}

export interface UploadSessionCreateRequest {
  workspaceId: string;
  episodeOrder: number;
  uploadSequence?: number;
  originalFilename: string;
  mediaType: string;
  totalSizeBytes: number;
  contentSha256?: string | null;
}

export interface UploadSessionView {
  uploadSessionId: string;
  workspaceId: string;
  seriesId: string;
  episodeOrder: number;
  uploadSequence: number;
  originalFilename: string;
  mediaType: string;
  totalSizeBytes: number;
  contentSha256: string | null;
  preferredSegmentSizeBytes: number;
  totalSegments: number;
  status: UploadStatus;
  uploadedSegmentCount: number;
  uploadedSizeBytes: number;
  missingSegmentNumbers: number[];
  completionStage: UploadCompletionStage | null;
  completionProgress: number | null;
  estimatedRemainingMs: number | null;
  expiresAt: UnixTimeMs;
  videoId: string | null;
  jobId: string | null;
  lastError: Failure | null;
  version: number;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}

export interface UploadCompleteView {
  uploadSession: UploadSessionView;
  videoId: string;
  jobId: string;
}

export interface VideoChunkCheckpointView {
  sequenceNo: number;
  videoChunkId: string;
  chunkStartMs: number | null;
  chunkEndMs: number | null;
  stage: string;
  lastError: Failure | null;
}

export interface ParseJobCurrentChunkView {
  sequenceNo: number;
  startMs: number;
  endMs: number;
}

export interface ParseJobView {
  jobId: string;
  uploadSessionId: string | null;
  workspaceId: string;
  seriesId: string;
  videoId: string;
  seriesName: string;
  episodeOrder: number;
  originalFilename: string;
  pipelineVersion: string;
  retryOfJobId: string | null;
  status: ParseJobStatus;
  stage: string;
  progress: number;
  overallProgress: number | null;
  stageProgress: number | null;
  completedDurationMs: number | null;
  completedThroughMs: number | null;
  totalDurationMs: number;
  completedChunkCount: number | null;
  totalChunkCount: number;
  currentChunk: ParseJobCurrentChunkView | null;
  attemptCount: number;
  maxAttempts: number;
  availableAt: UnixTimeMs | null;
  startedAt: UnixTimeMs | null;
  completedAt: UnixTimeMs | null;
  estimatedRemainingMs: number | null;
  canRetry: boolean;
  canCancel: boolean;
  lastError: Failure | null;
  version: number;
  checkpoints: VideoChunkCheckpointView[];
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}

export type ParseUsageCoverage = "not_started" | "recorded" | "partial" | "unknown";

export interface ParseUsageRowVO {
  provider: string;
  model: string;
  category: "parse" | "embedding";
  operation: string;
  callCount: number;
  inputTokens: number | null;
  outputTokens: number | null;
  cachedInputTokens: number | null;
  reasoningTokens: number | null;
  totalTokens: number | null;
  estimatedCostCny: string | null;
  missingUsageCount: number;
  unpricedCallCount: number;
}

export interface ParseUsageVO {
  jobId: string;
  videoId: string;
  seriesId: string;
  seriesName: string;
  episodeOrder: number;
  originalFilename: string;
  currency: "CNY";
  coverage: ParseUsageCoverage;
  totalTokens: number | null;
  estimatedCostCny: string | null;
  callCount: number;
  missingUsageCount: number;
  unpricedCallCount: number;
  items: ParseUsageRowVO[];
}

export interface VideoChunkCandidate {
  videoChunkId: string;
  rank: number;
  fusedRank: number;
  fusedScore: number;
  textRank: number | null;
  textScore: number | null;
  visionRank: number | null;
  visionScore: number | null;
}

export interface TranscriptCue {
  speaker: string | null;
  text: string;
  startMs: number;
  endMs: number;
}

export interface VideoChunkEvidence {
  seriesId: string;
  workspaceId: string;
  seriesName: string;
  seriesStatus: SeriesStatus;
  videoId: string;
  episodeOrder: number;
  originalFilename: string;
  durationMs: number;
  videoChunkId: string;
  sequenceNo: number;
  startMs: number;
  endMs: number;
  analysisWindowStartMs: number;
  analysisWindowEndMs: number;
  playbackStatus: PlaybackStatus;
  playbackUrl: string | null;
  playbackUrlExpiresAt: UnixTimeMs | null;
  thumbnail: Keyframe | null;
  keyframes: Keyframe[];
  transcript: TranscriptCue[];
  baselineVisualCaption: string | null;
  allTimeEvents: Array<{ description: string; startMs: number; endMs: number }>;
  allTimeEventText: string;
  videoCompact: string;
  contextBeforeCompact: string | null;
  contextAfterCompact: string | null;
  windowSummary: string;
  modelProvider: string | null;
  modelName: string | null;
  pipelineVersion: string;
  isFallback: boolean;
  parsedAt: UnixTimeMs;
}

export type MatchedEvidenceKind = "TRANSCRIPT" | "VISUAL_EVENT" | "CONTEXT";

export interface MatchedEvidence {
  kind: MatchedEvidenceKind;
  text: string;
  startMs: number | null;
  endMs: number | null;
}

export interface SelectionReason {
  summary: string;
  matchedEvidence: MatchedEvidence[];
  limitations: string[];
}

export interface VideoChunkSearchHit {
  candidate: VideoChunkCandidate;
  selectionReason: SelectionReason;
  evidence: VideoChunkEvidence;
}

export interface AgentReply {
  message: string;
  resultCount: number;
}

export interface VideoChunkSearchEvidence {
  query: string;
  seriesIds: string[];
  searchedVideoIds: string[];
  partial: boolean;
  warnings: string[];
  hits: VideoChunkSearchHit[];
}

export interface VideoChunkSearchResult extends VideoChunkSearchEvidence {
  agentReply: AgentReply;
}

export interface VideoSearchRequest {
  workspaceId: string;
  query: string;
  seriesIds: string[];
  topK: number;
}

export interface AgentConversationView {
  conversationId: string;
  workspaceId: string;
  title: string;
  messageCount: number;
  lastMessagePreview: string | null;
  lastMessageAt: UnixTimeMs | null;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}

export type AnswerPhase = "QUEUED" | "RETRIEVING" | "EVALUATING" | "RESPONDING";
export interface EvidenceReference { messageId: string; videoChunkId: string }
export type AgentRenderBlock =
  | { type: "markdown"; markdown: string }
  | { type: "video"; videoChunkId: string };

export interface AgentMessageView {
  messageId: string;
  conversationId: string;
  seriesIds: string[];
  sequenceNo: number;
  role: "USER" | "ASSISTANT";
  status: "PROCESSING" | "COMPLETED" | "FAILED";
  phase: AnswerPhase | null;
  version: number;
  reference: EvidenceReference | null;
  content: string | null;
  modelName: string | null;
  replyToMessageId: string | null;
  retrievalQuery: string | null;
  searchResult: VideoChunkSearchEvidence | null;
  renderBlocks?: AgentRenderBlock[];
  lastError: Failure | null;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}

export interface AgentMessageSendView {
  userMessage: AgentMessageView;
  assistantMessage: AgentMessageView;
}

export interface AgentMessageEventView {
  type: "messages";
  conversationId: string;
  messages: AgentMessageView[];
  occurredAt: UnixTimeMs;
}

interface AgentAnswerEventBase {
  conversationId: string;
  messageId: string;
  sequence: number;
  occurredAt: UnixTimeMs;
}

export type AgentAnswerEvent =
  | (AgentAnswerEventBase & AgentMessageSendView & {
      type: "answer.started" | "answer.completed";
    })
  | (AgentAnswerEventBase & {
      type: "answer.evidence";
      searchResult: VideoChunkSearchEvidence;
    })
  | (AgentAnswerEventBase & { type: "answer.block"; block: AgentRenderBlock })
  | (AgentAnswerEventBase & { type: "answer.progress"; phase: AnswerPhase })
  | (AgentAnswerEventBase & { type: "answer.delta"; delta: string })
  | (AgentAnswerEventBase & AgentMessageSendView & {
      type: "answer.failed";
      error: Failure;
    })
  | (AgentAnswerEventBase & { type: "answer.interrupted"; error: Failure });
