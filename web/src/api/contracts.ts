export type RuntimeMode = "checking" | "live" | "offline";
export type ThemeMode = "light" | "dark";

export type SeriesStatus = "EMPTY" | "INDEXING" | "READY";
export type UploadStatus =
  | "UPLOADING"
  | "COMPLETING"
  | "COMPLETED"
  | "EXPIRED";
export type ParseJobStatus =
  | "QUEUED"
  | "RUNNING"
  | "RETRY_WAIT"
  | "SUCCEEDED"
  | "FAILED";

export interface ApiErrorPayload {
  code?: string;
  message?: string;
  detail?: unknown;
}

export interface VideoSummary {
  videoId: string;
  episodeOrder: number;
  uploadSequence: number;
  originalFilename: string;
  originalSourceUri: string;
  inferenceProxyUri: string | null;
  contentHash: string;
  totalBytes: number;
  durationMs: number;
  uploadStatus: string;
  parseStatus: string;
  uploadProgress: number;
  parseProgress: number;
}

export interface SeriesView {
  seriesId: string;
  workspaceId: string;
  name: string;
  description: string | null;
  status: SeriesStatus;
  importantExtraData: Record<string, unknown> | null;
  characterBible: Array<Record<string, unknown>>;
  videoCount: number;
  videos: VideoSummary[];
}

export interface SeriesCreateRequest {
  workspaceId: string;
  name: string;
  description?: string | null;
  importantExtraData?: Record<string, unknown> | null;
  characterBible?: Array<Record<string, unknown>>;
}

export interface UploadSessionCreateRequest {
  workspaceId: string;
  episodeOrder: number;
  uploadSequence?: number;
  originalFilename: string;
  mediaType: string;
  totalSizeBytes: number;
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
  preferredSegmentSizeBytes: number;
  totalSegments: number;
  status: UploadStatus;
  uploadedSegmentCount: number;
  uploadedSizeBytes: number;
  missingSegmentNumbers: number[];
  expiresAt: string;
  videoId: string | null;
  lastError: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface UploadCompleteView {
  uploadSession: UploadSessionView;
  videoId: string;
  jobId: string;
}

export interface VideoChunkCheckpointView {
  sequenceNo: number;
  videoChunkId: string;
  stage: string;
  chunkMediaUri: string | null;
  analysisWindowMediaUri: string | null;
  lastError: string | null;
}

export interface ParseJobView {
  jobId: string;
  seriesId: string;
  videoId: string;
  seriesName: string;
  episodeOrder: number;
  originalFilename: string;
  pipelineVersion: string;
  status: ParseJobStatus;
  stage: string;
  progress: number;
  attemptCount: number;
  maxAttempts: number;
  availableAt: string;
  leaseExpiresAt: string | null;
  startedAt: string | null;
  completedAt: string | null;
  lastError: string | null;
  checkpoints: VideoChunkCheckpointView[];
  createdAt: string;
  updatedAt: string;
}

export interface VideoChunkCandidate {
  videoChunkId: string;
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
  seriesDescription: string | null;
  seriesStatus: SeriesStatus;
  importantExtraData: Record<string, unknown> | null;
  characterBible: Array<Record<string, unknown>>;
  videoId: string;
  episodeOrder: number;
  originalFilename: string;
  originalSourceUri: string;
  inferenceProxyUri: string | null;
  contentHash: string;
  durationMs: number;
  uploadStatus: string;
  parseStatus: string;
  videoChunkId: string;
  sequenceNo: number;
  startMs: number;
  endMs: number;
  analysisWindowStartMs: number;
  analysisWindowEndMs: number;
  chunkMediaRef: string;
  analysisWindowMediaRef: string;
  transcript: TranscriptCue[];
  baselineVisualCaption: string | null;
  keyframeUris: string[];
  allTimeEvents: Array<Record<string, unknown>>;
  allTimeEventText: string;
  retrievalText: string;
  videoCompact: string;
  contextBeforeCompact: string;
  contextAfterCompact: string;
  windowSummary: string;
  modelProvider: string | null;
  modelName: string | null;
  pipelineVersion: string;
  isFallback: boolean;
  parseProvenance: Record<string, unknown> | null;
  parsedAt: string;
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

export interface VideoChunkSearchResult {
  query: string;
  seriesIds: string[];
  agentReply: AgentReply;
  hits: VideoChunkSearchHit[];
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
  lastMessageAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface AgentMessageView {
  messageId: string;
  conversationId: string;
  seriesIds: string[];
  sequenceNo: number;
  role: "USER" | "ASSISTANT";
  status: "PROCESSING" | "COMPLETED" | "FAILED";
  content: string | null;
  modelName: string | null;
  replyToMessageId: string | null;
  retrievalQuery: string | null;
  searchResult: VideoChunkSearchResult | null;
  lastError: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface AgentMessageSendView {
  userMessage: AgentMessageView;
  assistantMessage: AgentMessageView;
}

export interface AgentMessageEventView {
  conversationId: string;
  messages: AgentMessageView[];
}

interface AgentAnswerEventBase {
  conversationId: string;
  messageId: string;
  sequence: number;
}

export type AgentAnswerEvent =
  | (AgentAnswerEventBase & AgentMessageSendView & {
      type: "answer.started" | "answer.completed";
    })
  | (AgentAnswerEventBase & {
      type: "answer.evidence";
      hits: VideoChunkSearchHit[];
    })
  | (AgentAnswerEventBase & { type: "answer.delta"; delta: string })
  | (AgentAnswerEventBase & {
      type: "answer.failed";
      code: string;
      message: string;
      userMessage?: AgentMessageView;
      assistantMessage?: AgentMessageView;
    });
