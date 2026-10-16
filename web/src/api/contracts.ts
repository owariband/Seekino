export type RuntimeMode = "checking" | "live" | "offline";
export type DisplayMode = "release" | "debug";
export interface RuntimeConfig { displayMode: DisplayMode }

export interface AuthIdentity {
  userId: string;
  email: string | null;
  isGuest: boolean;
  tenantId: string | null;
  publicTenantId: string;
  mode: "local" | "cloud";
  csrfToken: string;
  permissions: { canUpload: boolean; canManage: boolean; canConfigure: boolean };
}

export interface ApiConnection {
  id: string;
  baseUrl: string;
}

export interface DataSource {
  key: string;
  kind: "local" | "public" | "personal";
  connection: ApiConnection;
  tenantId: string;
  identity: AuthIdentity;
}
export type ThemeMode = "light" | "dark";

export type SeriesStatus = "EMPTY" | "INDEXING" | "PARTIAL" | "READY" | "FAILED";
export type UnixTimeMs = number;
// Media positions and durations named *Seconds are safe integer milliseconds.
// ParseConfig window/fallback durations and request timeouts remain configuration seconds.
export type PlaybackStatus = "PREPARING" | "READY" | "FAILED" | "MISSING";
export type IndexStatus = "NOT_READY" | "SYNCING" | "READY" | "FAILED";
export interface Failure { code: string; message: string }
export interface Page<T> { items: T[]; total: number; hasMore: boolean; nextCursor: string | null }
export interface ImageAsset { imageId: string; url: string; expiresAt: UnixTimeMs | null }
/** atSeconds is a media position in integer milliseconds. */
export interface Keyframe extends ImageAsset { atSeconds: number; alt: string | null }
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
export type VideoParseTaskStatus =
  | "PENDING"
  | "PREPARING"
  | "PARSING"
  | "INDEXING"
  | "COMPLETE"
  | "PARSING_FAILED"
  | "INDEXING_FAILED";

export interface ApiErrorView extends Failure {
  requestId: string;
  fieldErrors: Array<{ field: string; message: string }>;
  retryAt: UnixTimeMs | null;
}
export type ApiErrorPayload = Partial<ApiErrorView>;
export interface Character { name: string; aliases: string[] }

export interface VideoSummary {
  videoId: string;
  tenantId: string;
  seriesId: string;
  episodeOrder: number;
  uploadSequence: number;
  originalFilename: string;
  totalBytes: number;
  /** Integer milliseconds; the field name is retained for compatibility. */
  durationSeconds: number;
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

export interface VideoEpisodeUpdateRequest {
  tenantId: string;
  videoId: string;
  episodeOrder: number;
}

export interface SeriesSummary {
  isPublished?: boolean;
  seriesId: string;
  tenantId: string;
  /** Stable import key; the series id derives from it. */
  name: string;
  /** Viewer-facing title; use seriesTitle() to fall back to name. */
  displayName?: string | null;
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
  transcriptLanguageHint: string | null;
  importantExtraData: Record<string, unknown> | null;
  characterBible: Character[];
  videos: VideoSummary[];
  pendingUploads: UploadSessionView[];
}

export interface SeriesCreateRequest {
  tenantId: string;
  name: string;
  description?: string | null;
  transcriptLanguageHint?: string | null;
  importantExtraData?: Record<string, unknown> | null;
  characterBible?: Character[];
}

export interface SeriesBackground {
  format: "markdown" | "json";
  content: string;
}

export interface SeriesUpdateRequest {
  isPublished?: boolean;
  tenantId: string;
  seriesId: string;
  displayName?: string | null;
  description?: string | null;
  transcriptLanguageHint?: string | null;
  importantExtraData?: SeriesBackground | null;
}

export interface UploadSessionCreateRequest {
  tenantId: string;
  episodeOrder: number;
  uploadSequence?: number;
  originalFilename: string;
  mediaType: string;
  totalSizeBytes: number;
  contentSha256?: string | null;
  parseConfig?: ParseConfig | null;
}

export interface ParseConfig {
  version: 2;
  window: {
    chunkDurationSeconds: number;
    contextOverlapSeconds: number;
    analysisMaxHeight: number;
    canonicalMaxHeight: number;
  };
  asr: {
    mode: "lite" | "none";
    model: string;
    fps: number;
    maxOutputTokens: number | null;
    concurrency: number;
  };
  vision: {
    model: string;
    inputMode: "video" | "ordered_frames";
    fps: number;
    maxFramesPerWindow: number | null;
    maxOutputTokens: number | null;
  };
  context: {
    maxEvents: number;
    maxChars: number;
    backgroundTextEnabled: boolean;
    backgroundTextMaxChars: number | null;
    backgroundImagesEnabled: boolean;
    maxBackgroundImages: number | null;
    backgroundImageMaxEdge: number;
  };
  fallback: {
    enabled: boolean;
    model: string;
    frameFps: number;
    groupDurationSeconds: number;
    maxFramesPerGroup: number;
    maxHeight: number;
    reasoningEffort: "none" | "low" | "high" | "max";
    maxOutputTokens: number;
    requestTimeoutSeconds: number;
  };
  embedding: {
    model: string;
    dimensions: number;
    videoFps: number;
  };
  execution: {
    requestTimeoutSeconds: number;
    maxAttempts: number;
  };
}

export interface VideoParseConfig {
  visual_input: {
    mode: "ordered_frames" | "video";
    fps: number;
    max_frames_per_chunk: number;
    max_height: number;
  };
  [key: string]: unknown;
}

export interface UploadSessionView {
  uploadSessionId: string;
  tenantId: string;
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
  initialVideoParseTaskId?: string | null;
  lastError: Failure | null;
  version: number;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
  parseConfig?: VideoParseConfig | null;
}

export interface UploadCompleteView {
  uploadSession: UploadSessionView;
  videoId: string;
  jobId: string | null;
  videoParseTaskId: string;
}

export interface VideoChunkCheckpointView {
  sequenceNo: number;
  videoChunkId: string;
  /** Chunk bounds in integer milliseconds. */
  chunkStartSeconds: number | null;
  chunkEndSeconds: number | null;
  stage: string;
  lastError: Failure | null;
}

export interface ParseJobCurrentChunkView {
  sequenceNo: number;
  /** Chunk bounds in integer milliseconds. */
  startSeconds: number;
  endSeconds: number;
}

export interface ParseJobView {
  jobId: string;
  uploadSessionId: string | null;
  tenantId: string;
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
  /** Media progress in integer milliseconds. */
  completedDurationSeconds: number | null;
  completedThroughSeconds: number | null;
  /** Source video duration in integer milliseconds; totalDurationSeconds is retained. */
  videoDurationSeconds: number;
  totalDurationSeconds: number;
  /** Cumulative known worker processing time, never the video duration. */
  processingDurationMs: number;
  /** True only for a terminal job whose every attempt duration was measured. */
  processingTimingComplete: boolean;
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
  canReparse: boolean;
  canCancel: boolean;
  lastError: Failure | null;
  version: number;
  checkpoints: VideoChunkCheckpointView[];
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
  parseConfig?: ParseConfig | null;
}

export interface VideoParseTaskView {
  videoParseTaskId: string;
  videoId: string;
  pipelineVersion: string;
  status: VideoParseTaskStatus;
  parseConfig: Record<string, unknown>;
  chunkCount: number;
  version: number;
  nextExecuteTime: UnixTimeMs;
  attemptCount: number;
  maxAttempts: number;
  leaseOwner: string | null;
  leaseVersion: number;
  leaseExpireTime: UnixTimeMs | null;
  heartbeatTime: UnixTimeMs | null;
  startTime: UnixTimeMs | null;
  finishTime: UnixTimeMs | null;
  processingDurationMs: number;
  processingTimingComplete: boolean;
  lastError: Failure | null;
  isTest: boolean;
  detail: Record<string, unknown> & {
    progress?: {
      completedChunks: number;
      totalChunks: number;
      completedRefines: number;
      totalRefines: number;
      /** Persisted Understanding, Refine, and index publication completions. */
      completedUnits: number;
      /** Understanding and index units per chunk, plus one Refine unit per interval. */
      totalUnits: number;
      /** Understanding and Refine share 95%; index publication (including embedding) contributes 5%. */
      percent: number | null;
      topologyReady: boolean;
      /** Chunks whose current content versions are published and query-visible. */
      indexedChunks: number;
    };
    timing?: {
      recorded: boolean;
      complete: boolean;
      activeExecutions: number;
      /** Union of confirmed Understanding, Refine, and index execution spans (including embedding). */
      effectiveDurationMs: number | null;
    };
  };
  createTime: UnixTimeMs;
  updateTime: UnixTimeMs;
  /** Null on retry/reparse action responses; list/get/events always include this context. */
  tenantId: string | null;
  seriesId: string | null;
  seriesName: string | null;
  episodeOrder: number | null;
  originalFilename: string | null;
  uploadSessionId: string | null;
  /** Source video duration in integer milliseconds. */
  durationSeconds: number | null;
  canRetry: boolean;
  canReparse: boolean;
  canDelete: boolean;
}

export interface UsageTotals {
  callCount: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  cachedInputTokens: number | null;
  cacheWriteInputTokens: number | null;
  reasoningTokens: number | null;
  totalTokens: number | null;
  knownTokens: number;
  missingUsageCount: number;
  cost?: {
    currency: "CNY";
    estimatedAmount: string | null;
    knownAmount: string | null;
    pricedCallCount: number;
    unpricedCallCount: number;
    excludedCallCount: number;
  } | null;
}

export interface UsageModelRow extends UsageTotals {
  provider: string | null;
  src?: string | null;
  model: string | null;
  category: string | null;
  operation: string | null;
}

export interface UsageRunRow extends UsageTotals {
  runId: string;
  taskType: "video_parse" | "chat_retrieval";
  seriesId: string | null;
  videoId: string | null;
}

export interface UsageOverviewOptions {
  fromStartTime?: UnixTimeMs;
  toStartTime?: UnixTimeMs;
  seriesId?: string;
  runId?: string;
}

export interface UsageOverviewVO {
  source: "local";
  status: "available" | "unavailable";
  message: string | null;
  fromStartTime: UnixTimeMs;
  toStartTime: UnixTimeMs;
  fetchedAt: UnixTimeMs;
  truncated: boolean;
  totals: UsageTotals;
  models: UsageModelRow[];
  runs: UsageRunRow[];
}

export interface VideoChunkCandidate {
  videoChunkId: string;
  rank: number;
  fusedRank: number;
  fusedScore: number;
  lexicalRank?: number | null;
  lexicalScore?: number | null;
  transcriptRank: number | null;
  transcriptScore: number | null;
  textRank: number | null;
  textScore: number | null;
  visionRank: number | null;
  visionScore: number | null;
}

export interface TranscriptCue {
  speaker: string | null;
  text: string;
  /** Original-video bounds in integer milliseconds. */
  startSeconds: number;
  endSeconds: number;
}

export interface VideoChunkEvidence {
  seriesId: string;
  tenantId: string;
  seriesName: string;
  seriesStatus: SeriesStatus;
  videoId: string;
  episodeOrder: number;
  originalFilename: string;
  /** Duration and all media bounds below use integer milliseconds. */
  durationSeconds: number;
  videoChunkId: string;
  sequenceNo: number;
  startSeconds: number;
  endSeconds: number;
  analysisWindowStartSeconds: number;
  analysisWindowEndSeconds: number;
  playbackStatus: PlaybackStatus;
  playbackUrl: string | null;
  playbackUrlExpiresAt: UnixTimeMs | null;
  thumbnail: Keyframe | null;
  keyframes: Keyframe[];
  transcript: TranscriptCue[];
  baselineVisualCaption: string | null;
  allTimeEvents: Array<{ description: string; startSeconds: number; endSeconds: number }>;
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
  /** Original-video bounds in integer milliseconds. */
  startSeconds: number | null;
  endSeconds: number | null;
}

export type LexicalFieldName =
  | "dialogue"
  | "screen_text"
  | "person_names"
  | "event_description"
  | "keywords"
  | "questions"
  | "summary";

export interface LexicalMatch {
  field: LexicalFieldName;
  rawText: string;
  language: string | null;
  role: "ORIGINAL" | "TRANSLATION" | null;
  speaker: string | null;
  /** Original-video bounds in integer milliseconds. */
  startSeconds: number;
  endSeconds: number;
  timelineIndex: number | null;
  enrichmentIndex: number | null;
}

export interface SelectionReason {
  summary: string;
  matchedEvidence: MatchedEvidence[];
  limitations: string[];
  /** Matched search text and its source; generated questions are retrieval hints, not facts. */
  lexicalMatches?: LexicalMatch[];
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

export interface LegacyRetrievalChannelWeights {
  transcript: number;
  semantic: number;
  visual: number;
  lexical?: never;
}

export interface LexicalRetrievalChannelWeights {
  lexical: number;
  semantic: number;
  visual: number;
  transcript?: never;
}

export type RetrievalChannelWeights = LegacyRetrievalChannelWeights | LexicalRetrievalChannelWeights;

export interface RetrievalTraceEntry {
  query: string;
  topK: number;
  strategyVersion: string;
  requestedWeights: RetrievalChannelWeights;
  effectiveWeights: RetrievalChannelWeights;
  resultCount: number;
  videoChunkIds: string[];
}

export interface AgentSearchEvidence extends Omit<VideoChunkSearchEvidence, "query"> {
  retrievalTrace?: RetrievalTraceEntry[];
}

export interface VideoSearchRequest {
  tenantId: string;
  query: string;
  seriesIds: string[];
  topK: number;
}

export interface AgentConversationView {
  conversationId: string;
  tenantId: string;
  title: string;
  messageCount: number;
  lastMessagePreview: string | null;
  lastMessageAt: UnixTimeMs | null;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}

export type AnswerPhase = "QUEUED" | "RETRIEVING" | "EVALUATING" | "RESPONDING";
export interface EvidenceReference { messageId: string; videoChunkId: string }
export interface AgentStageTiming { phase: AnswerPhase; durationMs: number }
export interface AgentMessageTiming {
  startedAt: UnixTimeMs;
  totalDurationMs: number | null;
  stages: AgentStageTiming[];
}
/** Video block range fields use integer milliseconds. */
export type AgentRenderBlock =
  | { type: "markdown"; markdown: string }
  | { type: "video"; videoChunkId: string; startSeconds: number; endSeconds: number };

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
  searchResult: AgentSearchEvidence | null;
  renderBlocks: AgentRenderBlock[];
  timing: AgentMessageTiming | null;
  lastError: Failure | null;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}

export interface AgentMessageSendView {
  userMessage: AgentMessageView;
  assistantMessage: AgentMessageView;
}

export interface AgentIntegerOption {
  defaultValue: number;
  minimum: number;
  maximum: number;
}

export interface AgentMessageOptions {
  topK: AgentIntegerOption;
  maxLoopTurns: AgentIntegerOption;
  maxModelCallsPerTurn: AgentIntegerOption;
  maxToolCallsPerTurn: AgentIntegerOption;
}

export type AgentReasoningEffort = "none" | "low" | "high" | "max";

export interface AgentGlobalSettingsValues {
  history_message_limit: number;
  default_top_k: number;
  maximum_top_k: number;
  max_loop_turns: number;
  max_model_calls_per_turn: number;
  maximum_model_calls_per_turn: number;
  max_tool_calls_per_turn: number;
  maximum_tool_calls_per_turn: number;
  limits_enabled: boolean;
  query_reasoning_effort: AgentReasoningEffort;
  evaluator_reasoning_effort: AgentReasoningEffort;
  chat_reasoning_effort: AgentReasoningEffort;
  max_output_tokens: number;
  max_prompt_chars: number;
  context_window_tokens: number;
  context_safety_margin_tokens: number;
  auto_compact_tokens: number;
  compact_target_tokens: number;
  max_tool_result_bytes: number;
  max_read_chars_per_call: number;
  max_catalog_items_per_page: number;
  stage_timeout_seconds: number;
  evaluator_stage_timeout_seconds: number;
  execution_timeout_seconds: number;
  maximum_concurrent_kicks: number;
}

export interface AgentGlobalSettings {
  values: AgentGlobalSettingsValues;
  revision: string;
  restartRequiredFields: string[];
  activeValues: Pick<AgentGlobalSettingsValues, "maximum_concurrent_kicks">;
}

export interface AgentMessageRunOptions {
  topK?: number;
  maxLoopTurns?: number;
  maxModelCallsPerTurn?: number;
  maxToolCallsPerTurn?: number;
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
      searchResult: AgentSearchEvidence;
    })
  | (AgentAnswerEventBase & { type: "answer.block"; block: AgentRenderBlock })
  | (AgentAnswerEventBase & { type: "answer.progress"; phase: AnswerPhase; timing?: AgentMessageTiming | null })
  | (AgentAnswerEventBase & { type: "answer.delta"; delta: string })
  | (AgentAnswerEventBase & AgentMessageSendView & {
      type: "answer.failed";
      error: Failure;
    })
  | (AgentAnswerEventBase & { type: "answer.interrupted"; error: Failure });
