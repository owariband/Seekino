import { markChatTextRendered } from "../../api/chatDelivery";
import { ArrowUp, ChevronDown, ChevronRight, History, PanelLeft, PanelLeftClose, LoaderCircle, Play, Plus, SlidersHorizontal, Square, Trash2, TriangleAlert, X } from "lucide-react";
import { Children, createContext, forwardRef, memo, type FormEvent, type ReactNode, useCallback, useEffect, useId, useContext, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import Markdown, { type Components, defaultUrlTransform } from "react-markdown";
import { Link, useInRouterContext, useLocation, useNavigate } from "react-router";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

import { useI18n } from "../../app/i18n";
import { translate, type Locale } from "../../shared/messages";
import { useWorkspace } from "../../app/workspace";
import { deleteAgentConversation, errorMessage, getAgentMessageOptions, identityStorageKey, mediaUrl, TENANT_ID } from "../../api/client";
import type { AgentAttachmentView, AgentConversationView, AgentMessageOptions, AgentMessageRunOptions, AgentMessageView, AgentRenderBlock, DisplayMode, EvidenceReference, SeriesSummary, VideoChunkSearchHit } from "../../api/contracts";
import { episodeLabel, formatTimecode, formatTimeRange, formatTimeText, MEDIA_TIMESTAMP_PATTERN, timestampMilliseconds } from "../../shared/format";
import { useReportAssistantBusy } from "../../shared/assistantActivity";
import { Kino } from "../../shared/kino/Kino";
import { useAuth } from "../auth/AuthProvider";
import { PlaybackFrame } from "../player/PlaybackFrame";
import { AgentProgress } from "./AgentProgress";
import { EvidencePlayer } from "./EvidencePlayer";
import { FOCUS_SEARCH_EVENT, loadConversations } from "./conversationNavigation";
import { containsModel, preferredChatModel, rememberChatModel, savedChatModelId, usePersonalChatModels } from "../settings/usePersonalChatModels";
import { PersonalModelSelect } from "../settings/PersonalModelSelect";
import { SettingsLink } from "../settings/SettingsLink";
import { usePublishChatSettings } from "../settings/activeChatSettings";
import { explainHit } from "./explain";
import { useAgentConversation, type AgentProgressEvent } from "./useAgentConversation";
import { AGENT_ATTACHMENT_ACCEPT, useAgentAttachments } from "./useAgentAttachments";
import { useThreadFollow } from "./useThreadFollow";
import styles from "./search.module.css";

type MessageOptionOverrides = Omit<AgentMessageRunOptions, "topK">;
type SelectedMessageOptions = Required<MessageOptionOverrides>;


const DEFAULT_CHAT_MODEL_ID = "default";
const SIDEBAR_WIDTH_KEY = "seekino-search-sidebar-width";
const SIDEBAR_MIN_WIDTH = 176;
const SIDEBAR_MAX_WIDTH = 440;
const SIDEBAR_DEFAULT_WIDTH = 248;
const SIDEBAR_VIEWPORT_RESERVE = 560;

const EXAMPLE_QUESTIONS = ["下雨天有人在门口等待的画面", "乐队第一次站上舞台演出", "两个人在天台吵架的那一段"];
const EMBEDDED_EXAMPLE_QUESTIONS = ["刚才发生了什么？", "现在画面里的人是谁？", "前面有类似的场景吗？"];
const toolLabels: Record<Extract<AgentRenderBlock, { type: "tool" }>["tool"], string> = {
  search_video_graph: "查找视频资料",
  search_web: "搜索网页资料",
  understand_current_video: "查看当前视频",
};

type ToolBlock = Extract<AgentRenderBlock, { type: "tool" }>;
type VideoBlock = Extract<AgentRenderBlock, { type: "video" }>;

function MessageAttachment({ attachment, index }: { attachment: AgentAttachmentView; index: number }) {
  const { t } = useI18n();
  const [failed, setFailed] = useState(false);
  const url = mediaUrl(attachment.previewUrl);
  const label = t("图片 {value0}", { value0: index + 1 });
  if (!url || failed) return <span className={styles.attachmentUnavailable} role="img" aria-label={t("{value0}：附件暂不可用", { value0: label })}>{label}<small>{t("附件暂不可用")}</small></span>;
  return <a className={styles.messageAttachment} href={url} target="_blank" rel="noreferrer" aria-label={t("查看{value0}原图", { value0: label })}>
    <img src={url} alt={label} loading="lazy" onError={() => setFailed(true)} />
  </a>;
}

type SearchPageProps = {
  navigation?: Pick<AgentConversationView, "conversationId" | "title"> & { key: string };
  onNavigationConsumed?: () => void;
  embedded?: boolean;
  initialSeriesId?: string;
  initialSeries?: SeriesSummary;
  captureExtraContext?: () => Record<string, unknown> | null;
  onOpenVideo?: (target: { tenantId: string; seriesId: string; videoId: string; startSeconds: number }) => void;
  /** Seeks the episode being watched; enables clickable timestamps in answers. Milliseconds. */
  onSeekCurrent?: (positionMs: number) => void;
  /** Shown just above the composer, e.g. what the watch page lets the assistant see. */
  composerContext?: ReactNode;
};

export interface SearchPageHandle {
  focusComposer(): void;
}
const messageOptionsStorageKey = () => identityStorageKey("seekino-agent-message-options");
type StoredMessageOptions = { overrides?: MessageOptionOverrides; serverOptions?: AgentMessageOptions; selection?: SelectedMessageOptions };
const messageOptionNames = ["maxLoopTurns", "maxModelCallsPerTurn", "maxToolCallsPerTurn"] as const;

function defaultMessageOptions(options: AgentMessageOptions): SelectedMessageOptions {
  return {
    maxLoopTurns: 0,
    maxModelCallsPerTurn: options.maxModelCallsPerTurn.defaultValue,
    maxToolCallsPerTurn: options.maxToolCallsPerTurn.defaultValue,
  };
}

function boundedMessageOptionOverrides(overrides: MessageOptionOverrides, options: AgentMessageOptions): MessageOptionOverrides {
  const bounded: MessageOptionOverrides = {};
  for (const name of messageOptionNames) {
    const value = overrides[name];
    const limits = options[name];
    if (typeof value === "number" && Number.isInteger(value)
      && value >= limits.minimum && value <= limits.maximum) {
      bounded[name] = value;
    }
  }
  return bounded;
}

function storedMessageOptionOverrides(options: AgentMessageOptions): MessageOptionOverrides {
  try {
    const saved = JSON.parse(localStorage.getItem(messageOptionsStorageKey()) ?? "null") as StoredMessageOptions | null;
    if (saved?.overrides) return boundedMessageOptionOverrides(saved.overrides, options);
    if (!saved?.serverOptions || !saved.selection) return {};
    const previousDefaults = defaultMessageOptions(saved.serverOptions);
    const previousOverrides: MessageOptionOverrides = {};
    for (const name of messageOptionNames) {
      if (saved.selection[name] !== previousDefaults[name]) previousOverrides[name] = saved.selection[name];
    }
    return boundedMessageOptionOverrides(previousOverrides, options);
  } catch {
    return {};
  }
}

function savedSidebarWidth(): number {
  try {
    const stored = window.localStorage.getItem(SIDEBAR_WIDTH_KEY);
    if (stored === null || !stored.trim()) return SIDEBAR_DEFAULT_WIDTH;
    const width = Number(stored);
    return Number.isFinite(width)
      ? Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, Math.round(width)))
      : SIDEBAR_DEFAULT_WIDTH;
  } catch {
    return SIDEBAR_DEFAULT_WIDTH;
  }
}

function SearchCoverage({ result, displayMode }: { result: AgentMessageView["searchResult"]; displayMode: DisplayMode }) {
  const { t } = useI18n();
  if (!result) return null;
  const warnings = displayMode === "debug" ? result.warnings : result.coverageMessages ?? [];
  if (warnings.length) return <div className={styles.resultWarnings} role="status">{warnings.map((warning) => <p key={warning}>{warning}</p>)}</div>;
  return result.partial ? <p className={styles.resultWarnings} role="status">{t("本次回答仅覆盖当前可检索的视频。")}</p> : null;
}

function emptyVideoMessage(result: NonNullable<AgentMessageView["searchResult"]>) {
  const hadCandidates = result.hadCandidates === undefined
    ? result.retrievalTrace?.some((entry) => entry.status !== "error" && entry.resultCount > 0) || null
    : result.hadCandidates;
  if (hadCandidates === true) return "检索到了候选片段，但本次回答未附带可播放视频，请重试。";
  if (hadCandidates === false) return "没有检索到可展示的视频片段，试试换个描述。";
  return "本次回答未附带视频片段。";
}

function messageRenderBlocks(message: AgentMessageView): AgentRenderBlock[] {
  const blocks = Array.isArray(message.renderBlocks) && message.renderBlocks.length > 0
    ? message.renderBlocks
    : message.content
      ? [{ type: "markdown" as const, markdown: message.content }]
      : [];
  const hitsById = new Map(
    message.searchResult?.hits.map((hit) => [hit.evidence.videoChunkId, hit]) ?? [],
  );
  return blocks.filter((block) => {
    if (block.type === "markdown") return Boolean(block.markdown.trim());
    if (block.type === "tool") return typeof block.callId === "string" && Boolean(block.callId.trim()) && Object.hasOwn(toolLabels, block.tool)
      && (block.status === "inProgress" || block.status === "completed" || block.status === "failed");
    const hit = hitsById.get(block.videoChunkId);
    return Boolean(
      hit
      && Number.isSafeInteger(block.startSeconds)
      && Number.isSafeInteger(block.endSeconds)
      && 0 <= block.startSeconds
      && block.startSeconds < block.endSeconds
      && block.endSeconds <= hit.evidence.durationSeconds,
    );
  });
}

function conversationTime(conversation: AgentConversationView, formatLocale: string): string {
  return new Date(conversation.lastMessageAt ?? conversation.updatedAt).toLocaleString(formatLocale, {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function conversationAccessibleName(conversation: AgentConversationView, locale: Locale, formatLocale: string): string {
  return translate(locale, "{value0}，{value1}，编号 {value2}", { value0: conversation.title, value1: conversationTime(conversation, formatLocale), value2: conversation.conversationId });
}

function linkTimestamps(children: ReactNode, onSeek: ((positionMs: number) => void) | undefined, displayMode: DisplayMode, locale: Locale): ReactNode {
  const displayText = displayMode === "debug" ? (value: string) => value : formatTimeText;
  return Children.map(children, (child) => {
    if (typeof child !== "string") return child;
    if (!onSeek) return displayText(child);
    const parts: ReactNode[] = [];
    let cursor = 0;
    for (const match of child.matchAll(MEDIA_TIMESTAMP_PATTERN)) {
      const positionMs = timestampMilliseconds(match);
      if (positionMs === null) continue;
      parts.push(displayText(child.slice(cursor, match.index)));
      parts.push(<button key={match.index} type="button" className={styles.timeLink} onClick={() => onSeek(positionMs)} title={translate(locale, "跳转到 {value0}", { value0: formatTimecode(positionMs) })}>{displayText(match[0])}</button>);
      cursor = match.index! + match[0].length;
    }
    if (!parts.length) return displayText(child);
    parts.push(displayText(child.slice(cursor)));
    return parts;
  });
}

const SeekContext = createContext<{ onSeek?: (positionMs: number) => void; displayMode: DisplayMode }>({ displayMode: "release" });

function WithTimes({ children, link = true }: { children: ReactNode; link?: boolean }) {
  const { onSeek, displayMode } = useContext(SeekContext);
  const { locale } = useI18n();
  return linkTimestamps(children, link ? onSeek : undefined, displayMode, locale);
}

function BlockedAnswerImage({ alt }: { alt?: string }) {
  const { t } = useI18n();
  return <span className={styles.answerImageBlocked}>{alt ? t("[图片：{value0}]", { value0: alt }) : t("[图片]")}</span>;
}

// Stable component identities: inline overrides would remount the answer on every render.
const answerComponents: Components = {
  h1: ({ children }) => <h1><WithTimes>{children}</WithTimes></h1>,
  h2: ({ children }) => <h2><WithTimes>{children}</WithTimes></h2>,
  h3: ({ children }) => <h3><WithTimes>{children}</WithTimes></h3>,
  h4: ({ children }) => <h4><WithTimes>{children}</WithTimes></h4>,
  h5: ({ children }) => <h5><WithTimes>{children}</WithTimes></h5>,
  h6: ({ children }) => <h6><WithTimes>{children}</WithTimes></h6>,
  p: ({ children }) => <p><WithTimes>{children}</WithTimes></p>,
  li: ({ children }) => <li><WithTimes>{children}</WithTimes></li>,
  strong: ({ children }) => <strong><WithTimes>{children}</WithTimes></strong>,
  em: ({ children }) => <em><WithTimes>{children}</WithTimes></em>,
  td: ({ children }) => <td><WithTimes>{children}</WithTimes></td>,
  th: ({ children }) => <th><WithTimes>{children}</WithTimes></th>,
  a: ({ children, href, title }) => href ? <a href={href} title={title} target="_blank" rel="noopener noreferrer"><WithTimes link={false}>{children}</WithTimes></a> : <span><WithTimes>{children}</WithTimes></span>,
  img: BlockedAnswerImage,
  table: ({ children }) => <div className={styles.answerTable}><table>{children}</table></div>,
};

// A stable memo boundary: unchanged Markdown keeps its committed tree while the turn updates.
const MarkdownAnswer = memo(function MarkdownAnswer({ markdown, conversationId, messageId }: { markdown: string; conversationId: string; messageId: string }) {
  const element = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Observe committed, visible DOM on the next frame; this is separate from
    // receipt and is not a claim about the exact physical paint timestamp.
    const frame = requestAnimationFrame(() => {
      if (element.current?.isConnected && element.current.getClientRects().length && element.current.textContent?.trim()) {
        markChatTextRendered(conversationId, messageId, element.current.textContent.length);
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [markdown, conversationId, messageId]);
  return (
    <div ref={element} className={styles.answerText}>
      <Markdown remarkPlugins={[remarkGfm, remarkBreaks]} skipHtml urlTransform={defaultUrlTransform} components={answerComponents}>{markdown}</Markdown>
    </div>
  );
});

function ToolRow({ block, message, displayMode }: { block: ToolBlock; message: AgentMessageView; displayMode: DisplayMode }) {
  const { t } = useI18n();
  if (displayMode !== "debug" && message.status !== "PROCESSING") return null;
  const active = message.status === "PROCESSING" && block.status === "inProgress";
  const status = block.status === "completed" ? t("已完成") : block.status === "failed" ? t("调用失败") : active ? t("进行中") : t("未确认完成");
  return <div className={styles.toolCall} role="status" aria-label={t(toolLabels[block.tool])} data-status={block.status} data-active={active}>
    <div className={styles.toolCallHeading}><span>{t(toolLabels[block.tool])}</span><small>{status}</small></div>
    {displayMode === "debug" && typeof block.query === "string" && block.query.trim() ? <p>{block.tool === "search_web" ? t("搜索词") : block.tool === "understand_current_video" ? t("核验目标") : t("检索目标")}{t("：")}{block.query}</p> : null}
  </div>;
}

function ModelRetryRow({ retry }: { retry: NonNullable<AgentMessageView["modelRetry"]> }) {
  const label = `reconnecting ${retry.attempt}/${retry.maximum}`;
  return <div className={styles.toolCall} role="status" aria-label={label} data-status="inProgress" data-active="true">
    <div className={styles.toolCallHeading}><span>{label}</span></div>
  </div>;
}

function VideoBlockCard({ message, block, hit, displayMode, selected, onOpenVideo, onSelect }: {
  message: AgentMessageView;
  block: VideoBlock;
  hit: VideoChunkSearchHit;
  displayMode: DisplayMode;
  selected: boolean;
  onOpenVideo?: SearchPageProps["onOpenVideo"];
  onSelect: (selection: { conversationId: string; messageId: string; chunkId: string }) => void;
}) {
  const { t, locale } = useI18n();
  const explanation = explainHit(hit, locale);
  const rangeLabel = formatTimeRange(block.startSeconds, block.endSeconds);
  const thumbnailUrl = hit.evidence.thumbnail ? mediaUrl(hit.evidence.thumbnail.url) : null;
  const open = () => onOpenVideo
    ? onOpenVideo({ tenantId: hit.evidence.tenantId, seriesId: hit.evidence.seriesId, videoId: hit.evidence.videoId, startSeconds: block.startSeconds })
    : onSelect({ conversationId: message.conversationId, messageId: message.messageId, chunkId: hit.evidence.videoChunkId });
  return (
    <section className={styles.renderVideoCard} aria-label={t("检索助手提交的视频片段")}>
      <button type="button" className={`${styles.videoCard} ${selected ? styles.videoCardSelected : ""}`} onClick={open} aria-pressed={selected} title={onOpenVideo ? t("打开整集并定位到片段") : undefined}>
        {hit.evidence.playbackStatus === "READY" && hit.evidence.playbackUrl ? (
          <span className={styles.videoCardThumb} role="img" aria-label={t("{value0} {value1} {value2} 视频片段预览", { value0: hit.evidence.seriesName, value1: episodeLabel(hit.evidence.episodeOrder), value2: rangeLabel })}>
            <PlaybackFrame className={styles.candidatePreviewFrame} src={mediaUrl(hit.evidence.playbackUrl)} positionSeconds={block.startSeconds} />
            <Play className={styles.candidatePreviewPlay} size={42} fill="currentColor" aria-hidden="true" />
          </span>
        ) : thumbnailUrl ? (
          <img className={styles.videoCardThumbImg} src={thumbnailUrl} alt={hit.evidence.thumbnail?.alt || t("{value0} 视频片段首帧", { value0: hit.evidence.seriesName })} loading="lazy" />
        ) : (
          <span className={styles.videoCardThumbFallback} role="img" aria-label={displayMode === "debug" ? t("该片段暂无首帧缩略图") : t("播放所选片段")}>
            <Play size={22} fill="currentColor" aria-hidden="true" />
            <span>{hit.evidence.playbackStatus === "READY" ? displayMode === "debug" ? t("暂无首帧 · 点击加载视频") : t("播放片段") : t("视频暂不可播放")}</span>
          </span>
        )}
        <span className={styles.videoCardBody}>
          <strong className={styles.videoCardTitle}>{displayMode === "debug" ? explanation.summary : formatTimeText(explanation.summary)}</strong>
          <small className={styles.videoCardMeta}>{hit.evidence.seriesName} · {episodeLabel(hit.evidence.episodeOrder)} · <time>{rangeLabel}</time></small>
        </span>
      </button>
    </section>
  );
}

type MessageRowProps = {
  message: AgentMessageView;
  steps: AgentProgressEvent[];
  reasoning: string;
  displayMode: DisplayMode;
  kinoHost: boolean;
  kinoAwake: boolean;
  selectedChunkId: string | null;
  series: SeriesSummary[];
  onOpenVideo?: SearchPageProps["onOpenVideo"];
  onSelect: (selection: { conversationId: string; messageId: string; chunkId: string }) => void;
};

const EMPTY_STEPS: AgentProgressEvent[] = [];

// Kept at module scope so typing and one bubble's updates do not re-render every historical answer.
const MessageRow = memo(function MessageRow({
  message, steps, reasoning, displayMode, kinoHost, kinoAwake, selectedChunkId, series, onOpenVideo, onSelect,
}: MessageRowProps) {
  const { t, locale } = useI18n();
  return (
    <article className={message.role === "USER" ? styles.userMessage : styles.agentMessage}>
      {message.role === "USER" ? (
        <div>
          {message.attachments?.length ? <div className={styles.messageAttachments} aria-label={t("消息图片")}>
            {message.attachments.map((attachment, index) => <MessageAttachment key={`${attachment.mediaUri}:${attachment.previewUrl}`} attachment={attachment} index={index} />)}
          </div> : null}
          <p>{message.content}</p>
          {displayMode === "debug" && message.seriesIds.length ? <small className={styles.messageScope}>{t("可用视频范围：")}{message.seriesIds.map((id) => series.find((item) => item.seriesId === id)?.name ?? id).join(locale === "en" ? ", " : "、")}</small> : null}
        </div>
      ) : (
        <div>
          <AgentProgress message={message} steps={steps} reasoning={reasoning} displayMode={displayMode}
            kinoHost={kinoHost} kinoAwake={kinoAwake} />
          {message.status === "FAILED" ? <p className={styles.messageError} role="alert">{message.lastError?.message || t("本次回答未完成")}</p> : null}
          {!message.content && message.status === "COMPLETED" && !message.searchResult ? <p className={styles.messageError}>{t("未生成回答")}</p> : null}
          <SearchCoverage result={message.searchResult} displayMode={displayMode} />
          {messageRenderBlocks(message).map((block, index) => {
            if (block.type === "markdown") return <MarkdownAnswer key={`markdown:${index}`} markdown={block.markdown} conversationId={message.conversationId} messageId={message.messageId} />;
            if (block.type === "tool") return <ToolRow key={`tool:${block.callId}`} block={block} message={message} displayMode={displayMode} />;
            const hit = message.searchResult?.hits.find((item) => item.evidence.videoChunkId === block.videoChunkId);
            if (!hit) return null;
            const selected = selectedChunkId === hit.evidence.videoChunkId;
            return <VideoBlockCard key={`video:${block.videoChunkId}`} message={message} block={block} hit={hit} displayMode={displayMode} selected={selected} onOpenVideo={onOpenVideo} onSelect={onSelect} />;
          })}
          {message.status === "PROCESSING" && message.modelRetry ? <ModelRetryRow retry={message.modelRetry} /> : null}
          {message.status === "COMPLETED" && message.searchResult && !message.searchResult.hits.length ? (
            <p className={styles.candidateEmpty}>
              <Kino state="think" width={38} scale={2} ground={false} />
              <span>{t(emptyVideoMessage(message.searchResult))}</span>
            </p>
          ) : null}
        </div>
      )}
    </article>
  );
});

export function RoutedSearchPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { scopeKey } = useAuth();
  const request = location.state?.conversation;
  const navigation = request?.scopeKey === scopeKey && typeof request.conversationId === "string" && typeof request.title === "string"
    ? { conversationId: request.conversationId, title: request.title, key: location.key }
    : undefined;
  return <SearchPage navigation={navigation} onNavigationConsumed={() => {
    const state = { ...location.state };
    delete state.conversation;
    navigate(location.pathname + location.search + location.hash, { replace: true, state });
  }} />;
}

export const SearchPage = forwardRef<SearchPageHandle, SearchPageProps>(function SearchPage(
  { embedded = false, initialSeriesId, initialSeries: suppliedSeries, captureExtraContext, onOpenVideo, onSeekCurrent, composerContext, navigation, onNavigationConsumed },
  ref,
) {
  const { t, locale, formatLocale } = useI18n();
  const localeRef = useRef(locale);
  localeRef.current = locale;
  const { mode, displayMode, series: workspaceSeries, isLoading: workspaceLoading, error: workspaceError } = useWorkspace();
  const series = useMemo(() => suppliedSeries ? [suppliedSeries, ...workspaceSeries.filter((item) => item.seriesId !== suppliedSeries.seriesId
    && (!captureExtraContext || item.tenantId === suppliedSeries.tenantId))] : workspaceSeries,
  [suppliedSeries, workspaceSeries, captureExtraContext]);
  const { canConfigure, canManage, activeSource, scopeKey } = useAuth();
  const optionScope = scopeKey ?? "";
  const requiresExplicitScope = activeSource.kind === "public" || Boolean(suppliedSeries && suppliedSeries.tenantId !== TENANT_ID);
  const inRouter = useInRouterContext();
  const [requestedConversationId, setRequestedConversationId] = useState<string | null>(navigation?.conversationId ?? null);
  const consumedNavigationRef = useRef<string | undefined>(undefined);
  const [conversations, setConversations] = useState<AgentConversationView[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(true);
  const [conversationListError, setConversationListError] = useState<string | null>(null);
  const [conversationActionError, setConversationActionError] = useState<string | null>(null);
  const [preparingAttachments, setPreparingAttachments] = useState(false);
  const [deletingConversationId, setDeletingConversationId] = useState<string | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [conversationStatus, setConversationStatus] = useState("");
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<string[]>([]);
  const scopeTouchedRef = useRef(false);
  const [query, setQuery] = useState("");
  const draftRevisionRef = useRef(0);
  const [reference, setReference] = useState<EvidenceReference | null>(null);
  const [messageOptionConfig, setMessageOptionConfig] = useState<AgentMessageOptions | null>(null);
  const [messageOptionOverrides, setMessageOptionOverrides] = useState<MessageOptionOverrides | null>(null);
  const [messageOptionsError, setMessageOptionsError] = useState<string | null>(null);
  const modelCatalog = usePersonalChatModels(optionScope, canConfigure === true && mode === "live");
  const customLoopTurnsRef = useRef(100);
  const messageOptionsRequestRef = useRef(0);
  const messageOptions = messageOptionConfig && messageOptionOverrides
    ? { ...defaultMessageOptions(messageOptionConfig), ...messageOptionOverrides }
    : null;
  const messageOptionSummary = !messageOptions || !messageOptionOverrides ? t("服务端默认") : [
    messageOptionOverrides.maxLoopTurns === undefined ? null : t("最多 {value0} 轮", { value0: messageOptions.maxLoopTurns }),
  ].filter(Boolean).join(" · ") || (Object.keys(messageOptionOverrides).length ? t("已自定义") : t("服务端默认"));
  const [selection, setSelection] = useState<{ conversationId: string; messageId: string; chunkId: string } | null>(null);
  const deleteWarningId = useId();
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const conversationPickerRef = useRef<HTMLDetailsElement>(null);
  const historyToggleRef = useRef<HTMLButtonElement>(null);
  const historyDialogRef = useRef<HTMLDialogElement>(null);
  const [compactLayout, setCompactLayout] = useState(() => window.matchMedia("(max-width: 900px)").matches);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [historyDrawerOpen, setHistoryDrawerOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(savedSidebarWidth);
  const [sidebarResizing, setSidebarResizing] = useState(false);
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth);
  const sidebarDragRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const maxSidebarWidth = Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, viewportWidth - SIDEBAR_VIEWPORT_RESERVE));
  const visibleSidebarWidth = Math.min(sidebarWidth, maxSidebarWidth);
  const sidebarStyle = embedded ? undefined : ({ "--history-sidebar-width": `${visibleSidebarWidth}px` } as CSSProperties);
  useEffect(() => {
    if (embedded) return;
    const media = window.matchMedia("(max-width: 900px)");
    const sync = () => { setCompactLayout(media.matches); setHistoryDrawerOpen(false); };
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, [embedded]);
  useEffect(() => {
    if (embedded || !compactLayout || !historyDrawerOpen) return;
    const dialog = historyDialogRef.current!;
    dialog.showModal();
    return () => { dialog.close(); };
  }, [embedded, compactLayout, historyDrawerOpen]);
  useEffect(() => {
    try { window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth)); } catch { /* Resizing still works without storage. */ }
  }, [sidebarWidth]);
  useEffect(() => {
    const sync = () => setViewportWidth(window.innerWidth);
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);
  useEffect(() => {
    if (!compactLayout && sidebarOpen) return;
    sidebarDragRef.current = null;
    setSidebarResizing(false);
  }, [compactLayout, sidebarOpen]);
  const [composerToolsOpen, setComposerToolsOpen] = useState(false);
  const composerToolsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!composerToolsOpen) return;
    const closeOutside = (event: globalThis.PointerEvent) => {
      if (event.target instanceof Node && !composerToolsRef.current?.contains(event.target)) setComposerToolsOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [composerToolsOpen]);
  const scopePickerRef = useRef<HTMLDetailsElement>(null);
  const scopeStatusId = useId();
  const answerSettingsRef = useRef<HTMLDetailsElement>(null);
  const deleteCancelRef = useRef<HTMLButtonElement>(null);
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null);
  const conversationFailureRef = useRef<HTMLParagraphElement>(null);
  const conversationsRef = useRef<AgentConversationView[]>([]);
  const deletedConversationIdsRef = useRef(new Set<string>());
  const focusAfterMutationRef = useRef<"composer" | "picker" | null>(null);
  const conversationListRequestRef = useRef(0);
  const wasSearchingRef = useRef(false);
  const { threadRef, follow: followThread, onScroll: onThreadScroll, scrollToBottomIfFollowing } = useThreadFollow();

  useImperativeHandle(ref, () => ({
    focusComposer: () => composerRef.current?.focus({ preventScroll: true }),
  }), []);
  useEffect(() => {
    if (embedded) return;
    const focus = () => composerRef.current?.focus({ preventScroll: true });
    if ((window.history.state as { usr?: { focusComposer?: boolean } } | null)?.usr?.focusComposer) focus();
    window.addEventListener(FOCUS_SEARCH_EVENT, focus);
    return () => window.removeEventListener(FOCUS_SEARCH_EVENT, focus);
  }, [embedded]);
  useEffect(() => {
    const closeOutside = (event: PointerEvent) => {
      if (!(event.target instanceof Node)) return;
      for (const ref of [conversationPickerRef, scopePickerRef, answerSettingsRef]) {
        const picker = ref.current;
        if (picker?.open && !picker.contains(event.target)) picker.removeAttribute("open");
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, []);
  const {
    conversationId, messages, progress, reasoning, loading, searching, connecting = false, cancelling = false, error,
    modelId: storedModelId = DEFAULT_CHAT_MODEL_ID, modelSaving = false, modelError = null,
    clear: clearConversation, ensureConversationId, applyDraftDefault, selectModel, send, stop,
  } = useAgentConversation(requestedConversationId);
  const personalSettings = modelCatalog && "settings" in modelCatalog ? modelCatalog.settings : null;
  const connections = personalSettings?.connections ?? [];
  // Guests chat through the server's opaque default and never load model connections.
  const modelId = canConfigure
    ? (conversationId ? savedChatModelId(connections, storedModelId) : storedModelId)
    : DEFAULT_CHAT_MODEL_ID;
  const hasModels = connections.some((connection) => connection.models.some((model) => model.available !== false));
  const modelSelected = !canConfigure || containsModel(connections, modelId);
  const modelSelectionInvalid = Boolean(personalSettings && modelId !== DEFAULT_CHAT_MODEL_ID && !modelSelected);
  const draftModelId = personalSettings ? preferredChatModel(personalSettings) : DEFAULT_CHAT_MODEL_ID;
  useEffect(() => {
    if (!personalSettings) return;
    if (conversationId && modelSelected && !loading && !modelSaving) rememberChatModel(modelId);
    applyDraftDefault?.(preferredChatModel(personalSettings));
  }, [applyDraftDefault, personalSettings, conversationId, modelSelected, modelId, modelSaving, loading]);
  const creatingConversation = preparingAttachments || (searching && !conversationId);
  usePublishChatSettings(canConfigure === true, { modelId, modelSaving, modelError, selectModel,
    loading: loading || creatingConversation || mode !== "live" });
  // Kino perches on the newest assistant bubble and leaps to the next one when a new turn starts.
  const kinoHostId = messages.findLast((message) => message.role === "ASSISTANT")?.messageId ?? null;
  // Timestamp links and display mode are the same for every answer; a stable value keeps memoized rows still.
  const seekContext = useMemo(() => ({ onSeek: onSeekCurrent, displayMode }), [onSeekCurrent, displayMode]);
  const attachments = useAgentAttachments(conversationId);
  const [pendingFiles, setPendingFiles] = useState<{ conversationId: string; files: File[] } | null>(null);
  useEffect(() => {
    if (!pendingFiles) return;
    if (pendingFiles.conversationId === conversationId) attachments.addFiles(pendingFiles.files);
    setPendingFiles(null);
  }, [pendingFiles, conversationId, attachments.addFiles]);
  const addFiles = async (files: File[]) => {
    if (!files.length || !modelSelected || modelSaving) return;
    if (conversationId) { attachments.addFiles(files); return; }
    setPreparingAttachments(true);
    try {
      const id = await ensureConversationId();
      if (id) setPendingFiles((current) => ({ conversationId: id, files: [...(current?.conversationId === id ? current.files : []), ...files] }));
    } finally {
      setPreparingAttachments(false);
    }
  };
  const draftContextRef = useRef({ conversationId, identityKey: identityStorageKey("agent-draft") });
  draftContextRef.current = { conversationId, identityKey: identityStorageKey("agent-draft") };
  useReportAssistantBusy(searching || messages.some((message) => message.status === "PROCESSING"));
  const pendingExampleRef = useRef(false);
  useEffect(() => {
    if (!pendingExampleRef.current) return;
    pendingExampleRef.current = false;
    composerRef.current?.form?.requestSubmit();
  }, [query]);
  const emptyPublicScope = requiresExplicitScope && mode === "live" && !workspaceLoading && !workspaceError && !series.length;
  const initialSeries = initialSeriesId ? series.find((item) => item.seriesId === initialSeriesId) : null;
  const sourceTenantId = series.find((item) => item.seriesId === selectedSeriesIds[0])?.tenantId;
  const referenceMessage = reference ? messages.find((message) => message.messageId === reference.messageId) : null;
  const referenceHit = referenceMessage?.searchResult?.hits.find((hit) => hit.evidence.videoChunkId === reference?.videoChunkId) ?? null;
  const referenceScopeValid = !reference || Boolean(referenceHit && referenceHit.evidence.tenantId === (sourceTenantId ?? TENANT_ID)
    && (!selectedSeriesIds.length || selectedSeriesIds.includes(referenceHit.evidence.seriesId)));
  const scopeValid = (!requiresExplicitScope || selectedSeriesIds.length > 0)
    && selectedSeriesIds.every((id) => series.some((item) => item.seriesId === id && item.tenantId === sourceTenantId));
  const canAttach = Boolean(modelSelected && !modelSaving && !loading && (!conversationId || !deletedConversationIdsRef.current.has(conversationId)) && mode === "live");
  const canSend = Boolean(query.trim() && canAttach && !creatingConversation && !pendingFiles && !searching && scopeValid && referenceScopeValid && attachments.items.every((item) => item.status === "ready"));

  useEffect(() => {
    scopeTouchedRef.current = false;
  }, [initialSeriesId, suppliedSeries?.tenantId, activeSource.key]);

  useEffect(() => {
    if (scopeTouchedRef.current) return;
    const preferred = embedded && initialSeriesId ? initialSeries : series[0];
    if (!embedded && !requiresExplicitScope) return;
    const next = preferred ? [preferred.seriesId] : [];
    setSelectedSeriesIds((current) => current.length === next.length && current.every((id, index) => id === next[index]) ? current : next);
  }, [embedded, initialSeriesId, initialSeries?.status, requiresExplicitScope, workspaceSeries, suppliedSeries]);

  useLayoutEffect(() => {
    const composer = composerRef.current;
    if (!composer) return;
    composer.style.height = "auto";
    if (query) composer.style.height = `${composer.scrollHeight}px`;
  }, [query]);

  const evidenceMessage = selection?.conversationId === conversationId ? messages.find((message) => message.messageId === selection.messageId) : null;
  const selectedBlock = evidenceMessage ? messageRenderBlocks(evidenceMessage).find((block) => block.type === "video" && block.videoChunkId === selection?.chunkId) : null;
  const selectedHit = selectedBlock ? evidenceMessage?.searchResult?.hits.find((hit) => hit.evidence.videoChunkId === selection?.chunkId) ?? null : null;
  const referenceBlock = referenceMessage && reference ? messageRenderBlocks(referenceMessage).find((block) => block.type === "video" && block.videoChunkId === reference.videoChunkId) : null;
  const referenceRange = referenceHit && referenceBlock?.type === "video"
    ? { startSeconds: referenceBlock.startSeconds, endSeconds: referenceBlock.endSeconds }
    : null;
  const activeConversation = conversations.find((item) => item.conversationId === conversationId) ?? null;

  useEffect(() => {
    if (displayMode !== "debug") return;
    let active = true;
    const refresh = () => {
      const requestId = ++messageOptionsRequestRef.current;
      void getAgentMessageOptions().then((options) => {
        if (!active || requestId !== messageOptionsRequestRef.current) return;
        setMessageOptionOverrides((current) => {
          const overrides = current === null
            ? storedMessageOptionOverrides(options)
            : boundedMessageOptionOverrides(current, options);
          if (overrides.maxLoopTurns) customLoopTurnsRef.current = overrides.maxLoopTurns;
          else if (current === null || customLoopTurnsRef.current < options.maxLoopTurns.minimum || customLoopTurnsRef.current > options.maxLoopTurns.maximum) {
            customLoopTurnsRef.current = options.maxLoopTurns.defaultValue;
          }
          return overrides;
        });
        setMessageOptionConfig(options);
        setMessageOptionsError(null);
      }).catch((failure) => {
        if (!active || requestId !== messageOptionsRequestRef.current) return;
        setMessageOptionsError(errorMessage(failure, localeRef.current));
      });
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => {
      active = false;
      window.removeEventListener("focus", refresh);
    };
  }, [displayMode, optionScope]);

  useEffect(() => {
    if (!messageOptionOverrides) return;
    try {
      localStorage.setItem(messageOptionsStorageKey(), JSON.stringify({ overrides: messageOptionOverrides }));
    } catch { /* Browser storage is optional; the current tab still keeps the selection. */ }
  }, [messageOptionOverrides]);

  const replaceConversations = useCallback((items: AgentConversationView[]) => {
    conversationsRef.current = items;
    setConversations(items);
  }, []);

  const refreshConversations = useCallback(async (showLoading = false) => {
    const requestId = ++conversationListRequestRef.current;
    if (showLoading) setConversationsLoading(true);
    try {
      const items = await loadConversations(localeRef.current);
      if (conversationListRequestRef.current !== requestId) return;
      replaceConversations(items);
      setConversationListError(null);
    } catch (failure) {
      if (conversationListRequestRef.current === requestId) setConversationListError(translate(localeRef.current, "读取对话记录失败：{value0}", { value0: errorMessage(failure, localeRef.current) }));
    } finally {
      if (conversationListRequestRef.current === requestId) setConversationsLoading(false);
    }
  }, [replaceConversations]);

  useEffect(() => { setSelection(null); setReference(null); }, [conversationId]);

  useEffect(() => {
    if (confirmingDeleteId) deleteCancelRef.current?.focus();
  }, [confirmingDeleteId]);

  useEffect(() => {
    const target = focusAfterMutationRef.current;
    if (target === "composer" && !loading && !error) {
      focusAfterMutationRef.current = null;
      setConversationStatus(t("对话已打开"));
      composerRef.current?.focus();
    } else if (target === "composer" && !loading && error) {
      focusAfterMutationRef.current = null;
      setConversationStatus(error ? t("打开对话失败：{value0}", { value0: error }) : t("打开对话失败"));
      if (conversationFailureRef.current) conversationFailureRef.current.focus();
      else (embedded ? conversationPickerRef.current?.querySelector<HTMLElement>("summary") : historyToggleRef.current)?.focus();
    } else if (target === "picker" && !creatingConversation && !deletingConversationId) {
      focusAfterMutationRef.current = null;
      (embedded ? conversationPickerRef.current?.querySelector<HTMLElement>("summary") : historyToggleRef.current)?.focus();
    }
  }, [conversationId, creatingConversation, deletingConversationId, embedded, error, loading]);

  useEffect(() => { void refreshConversations(true); }, [refreshConversations]);

  useEffect(() => {
    if (conversationId) void refreshConversations();
  }, [conversationId, refreshConversations]);

  useEffect(() => {
    if (conversationId && messages.length > 0) void refreshConversations();
  }, [conversationId, messages.length, refreshConversations]);

  useEffect(() => {
    const wasSearching = wasSearchingRef.current;
    wasSearchingRef.current = searching;
    if (wasSearching && !searching && conversationId) {
      void refreshConversations();
      const latestAssistant = [...messages].reverse().find((message) => message.role === "ASSISTANT");
      setConversationStatus(latestAssistant?.status === "COMPLETED" && !error ? t("回答已完成") : t("回答未完成"));
    }
  }, [conversationId, error, messages, refreshConversations, searching]);

  useEffect(() => {
    scrollToBottomIfFollowing();
  }, [messages, searching, scrollToBottomIfFollowing]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSend) return;
    const extraContext = captureExtraContext?.() ?? null;
    followThread();
    setConversationStatus(t("正在发送问题"));
    const options: AgentMessageRunOptions = displayMode === "debug" && messageOptionOverrides
      ? { ...messageOptionOverrides, maxLoopTurns: messageOptionOverrides.maxLoopTurns || undefined }
      : {};
    const revision = draftRevisionRef.current;
    const context = draftContextRef.current;
    const submittedIds = attachments.items.map((item) => item.id);
    void send(query, selectedSeriesIds, reference, options, extraContext, {
      ...(sourceTenantId ? { sourceTenantId } : {}),
      attachments: attachments.items.map((item) => ({ mediaUri: item.attachment!.mediaUri })),
      onAccepted: () => {
        if ((context.conversationId !== null && draftContextRef.current.conversationId !== context.conversationId)
          || identityStorageKey("agent-draft") !== context.identityKey) return;
        if (draftRevisionRef.current === revision) {
          setQuery("");
          setReference(null);
        }
        attachments.clear(submittedIds);
        setConversationStatus(t("问题已发送，正在处理"));
      },
    });
  };

  const closeConversationPicker = useCallback((restoreFocus = false) => {
    conversationPickerRef.current?.removeAttribute("open");
    setHistoryDrawerOpen(false);
    if (!embedded && !compactLayout && restoreFocus) setSidebarOpen(false);
    if (restoreFocus) (embedded ? conversationPickerRef.current?.querySelector<HTMLElement>("summary") : historyToggleRef.current)?.focus();
  }, [embedded, compactLayout]);

  const selectConversation = useCallback((conversation: Pick<AgentConversationView, "conversationId" | "title">) => {
    if (creatingConversation || deletingConversationId) return;
    const nextConversationId = conversation.conversationId;
    setConfirmingDeleteId(null);
    setConversationActionError(null);
    closeConversationPicker();
    if (nextConversationId === conversationId) {
      composerRef.current?.focus();
      return;
    }
    followThread();
    focusAfterMutationRef.current = "composer";
    setConversationStatus(t("正在打开对话：{value0}", { value0: conversation.title }));
    setRequestedConversationId(nextConversationId);
  }, [closeConversationPicker, conversationId, creatingConversation, deletingConversationId, followThread, t]);

  useEffect(() => {
    if (!navigation || consumedNavigationRef.current === navigation.key || creatingConversation || deletingConversationId) return;
    selectConversation(navigation);
    consumedNavigationRef.current = navigation.key;
    onNavigationConsumed?.();
  }, [navigation, onNavigationConsumed, selectConversation, creatingConversation, deletingConversationId]);

  const createConversation = async () => {
    if (mode !== "live" || loading || creatingConversation || deletingConversationId) return;
    const currentConversationCanBeReused = Boolean(
      !error
      && conversationId
      && modelId === draftModelId
      && !deletedConversationIdsRef.current.has(conversationId)
      && messages.length === 0
      && (!activeConversation || activeConversation.messageCount === 0),
    );
    if (currentConversationCanBeReused) {
      closeConversationPicker();
      composerRef.current?.focus();
      return;
    }
    setConfirmingDeleteId(null);
    setConversationActionError(null);
    clearConversation();
    followThread();
    focusAfterMutationRef.current = "composer";
    setRequestedConversationId("");
    setConversationStatus(t("已新建对话"));
    closeConversationPicker();
  };

  const deleteConversation = async (conversation: AgentConversationView) => {
    const deletingCurrentConversation = conversation.conversationId === conversationId;
    if (mode !== "live" || loading || creatingConversation || deletingConversationId || (deletingCurrentConversation && searching)) return;
    setDeletingConversationId(conversation.conversationId);
    setConversationActionError(null);
    let deleted = false;
    try {
      await deleteAgentConversation(conversation.conversationId);
      deleted = true;
      deletedConversationIdsRef.current.add(conversation.conversationId);
      conversationListRequestRef.current += 1;
      setConversationsLoading(false);
      const remaining = conversationsRef.current.filter((item) => item.conversationId !== conversation.conversationId);
      replaceConversations(remaining);
      setConfirmingDeleteId(null);
      setConversationStatus(t("已删除对话：{value0}", { value0: conversation.title }));
      if (deletingCurrentConversation) {
        clearConversation();
        const next = remaining[0];
        followThread();
        focusAfterMutationRef.current = "composer";
        setRequestedConversationId(next?.conversationId ?? "");
        closeConversationPicker();
      } else {
        focusAfterMutationRef.current = "picker";
        void refreshConversations();
      }
    } catch (failure) {
      setConversationActionError(t("{value0}：{value1}", { value0: deleted ? t("对话已删除，但新对话创建失败") : t("删除对话失败"), value1: errorMessage(failure, locale) }));
      if (!deleted) setConfirmingDeleteId(conversation.conversationId);
      if (deleted) focusAfterMutationRef.current = "picker";
      if (conversationPickerRef.current) conversationPickerRef.current.open = true;
    } finally {
      setDeletingConversationId(null);
    }
  };

  const showWelcome = !messages.length && !loading && (embedded || !searching);
  const newConversationButton = (
    <button
      className={styles.newConversation}
      type="button"
      onClick={() => void createConversation()}
      disabled={mode !== "live" || loading || creatingConversation || Boolean(deletingConversationId)}
      aria-label={t("新建对话")}
    >
      {creatingConversation ? <LoaderCircle size={15} className={styles.spinning} aria-hidden="true" /> : <Plus size={15} aria-hidden="true" />}
      <span>{creatingConversation ? t("正在新建") : t("新对话")}</span>
    </button>
  );
  const conversationMenu = (
    <section className={styles.conversationMenu} aria-label={t("对话记录")}>
      <header>
        <div><strong>{t("对话记录")}</strong><small>{conversations.length ? t("{value0} 个对话", { value0: conversations.length }) : t("暂无记录")}</small></div>
        {conversationsLoading ? <LoaderCircle size={14} className={styles.spinning} aria-label={t("正在读取对话记录")} /> : null}
        {embedded ? <button className={styles.conversationMenuClose} type="button" onClick={() => closeConversationPicker(true)} aria-label={t("关闭对话记录")}>
          <X size={14} aria-hidden="true" />
        </button> : null}
      </header>
      {conversationActionError || conversationListError ? <p className={styles.conversationError} role="alert">{conversationActionError ?? conversationListError}</p> : null}
      <ul className={styles.conversationList}>
        {conversations.map((conversation) => {
          const current = conversation.conversationId === conversationId;
          const deleting = deletingConversationId === conversation.conversationId;
          const cannotDelete = current && searching;
          const mutationLocked = loading || creatingConversation || Boolean(deletingConversationId);
          const accessibleName = conversationAccessibleName(conversation, locale, formatLocale);
          return (
            <li key={conversation.conversationId} className={current ? styles.conversationCurrent : undefined}>
              <button
                className={styles.conversationChoice}
                title={`${conversation.title} · ${conversationTime(conversation, formatLocale)}`}
                type="button"
                onClick={() => selectConversation(conversation)}
                aria-current={current ? "true" : undefined}
                aria-label={t("打开对话 {value0}", { value0: accessibleName })}
                disabled={creatingConversation || Boolean(deletingConversationId)}
              >
                <span><strong>{conversation.title}</strong><time>{conversationTime(conversation, formatLocale)}</time></span>
                <small>{conversation.lastMessagePreview || (conversation.messageCount ? t("{value0} 条消息", { value0: conversation.messageCount }) : t("还没有消息"))}</small>
              </button>
              <button
                className={styles.deleteConversation}
                type="button"
                onClick={(event) => {
                  deleteTriggerRef.current = event.currentTarget;
                  setConfirmingDeleteId(conversation.conversationId);
                }}
                disabled={mode !== "live" || mutationLocked || cannotDelete}
                aria-label={t("删除对话 {value0}", { value0: accessibleName })}
                title={cannotDelete ? t("当前回答进行中，完成后才能删除") : mutationLocked ? t("请等待当前对话操作完成") : t("删除对话")}
              >
                {deleting ? <LoaderCircle size={14} className={styles.spinning} aria-hidden="true" /> : <Trash2 size={14} aria-hidden="true" />}
              </button>
              {confirmingDeleteId === conversation.conversationId ? (
                <div className={styles.deleteConfirmation} role="group" aria-label={t("确认删除对话 {value0}", { value0: accessibleName })} aria-describedby={deleteWarningId}>
                  <p id={deleteWarningId}>{t("删除后，这个对话及其中的全部消息都无法恢复。")}</p>
                  <div>
                    <button ref={deleteCancelRef} type="button" aria-describedby={deleteWarningId} onClick={() => {
                      setConfirmingDeleteId(null);
                      deleteTriggerRef.current?.focus();
                    }}>{t("取消")}</button>
                    <button
                      type="button"
                      disabled={mutationLocked || cannotDelete}
                      onClick={() => void deleteConversation(conversation)}
                      aria-label={t("确认删除对话 {value0}", { value0: accessibleName })}
                      aria-describedby={deleteWarningId}
                      title={cannotDelete ? t("当前回答进行中，完成后才能删除") : undefined}
                    >{t("确认删除")}</button>
                  </div>
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
      {!conversationsLoading && !conversations.length ? <p className={styles.conversationEmpty}>{t("还没有可显示的对话。")}</p> : null}
    </section>
  );
  const sidebarContent = <div className={styles.sidebarContent} onKeyDown={(event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    if (confirmingDeleteId) { setConfirmingDeleteId(null); deleteTriggerRef.current?.focus(); }
    else closeConversationPicker(true);
  }}>
    <header className={styles.sidebarHeading}><h1>{t("Seekino 助手")}</h1>
      {compactLayout ? <button type="button" className={styles.historyToggle} aria-label={t("关闭对话记录")} onClick={() => closeConversationPicker(true)}><PanelLeftClose size={18} aria-hidden="true" /></button> : null}
    </header>
    {newConversationButton}
    {conversationMenu}
  </div>;

  const resizeSidebar = (clientX: number) => {
    const drag = sidebarDragRef.current;
    if (!drag) return;
    setSidebarWidth(Math.round(Math.max(SIDEBAR_MIN_WIDTH, Math.min(maxSidebarWidth, drag.startWidth + clientX - drag.startX))));
  };
  const endSidebarResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    sidebarDragRef.current = null;
    setSidebarResizing(false);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const attachmentControls = <>
    <button className={styles.addAttachment} type="button" disabled={!canAttach} onClick={() => attachmentInputRef.current?.click()} aria-label={t("添加图片")} title={t("添加图片（也可粘贴）")}><Plus size={21} aria-hidden="true" /></button>
    <input ref={attachmentInputRef} type="file" hidden multiple accept={AGENT_ATTACHMENT_ACCEPT} aria-label={t("选择聊天图片")} disabled={!canAttach} onChange={(event) => {
      const files = Array.from(event.target.files ?? []);
      event.target.value = "";
      void addFiles(files);
      composerRef.current?.focus();
    }} />
  </>;
  const namedSeries = (embedded || requiresExplicitScope) && selectedSeriesIds.length === 1
    ? series.find((item) => item.seriesId === selectedSeriesIds[0])
    : undefined;
  const scopeLabel = selectedSeriesIds.length
    ? namedSeries?.name ?? t("已选 {value0} 个系列", { value0: selectedSeriesIds.length })
    : requiresExplicitScope ? t(emptyPublicScope ? "公开系列" : "请选择要检索的公开系列") : t("全部视频系列");
  const scopePicker = <>
    <details
      className={styles.scopePicker}
      ref={scopePickerRef}
      onToggle={(event) => {
        if (event.currentTarget.open) answerSettingsRef.current?.removeAttribute("open");
      }}
    >
      <summary aria-label={t("选择检索视频系列")} aria-describedby={emptyPublicScope ? scopeStatusId : undefined}>
        <span>{scopeLabel}</span>
        <ChevronDown size={13} aria-hidden="true" />
      </summary>
      <div className={styles.scopeOptions} role="group" aria-label={t("检索视频系列多选")}>
        {requiresExplicitScope ? <p>{t("每次可选择同一分享者的系列")}</p> : <button type="button" onClick={() => { scopeTouchedRef.current = true; setSelectedSeriesIds([]); }}>{t("检索全部视频系列")}</button>}
        {series.map((item) => (
          <label key={item.seriesId}>
            <input
              type="checkbox"
              checked={selectedSeriesIds.includes(item.seriesId)}
              onChange={(event) => {
                scopeTouchedRef.current = true;
                setSelectedSeriesIds((current) => event.target.checked
                  ? [...current.filter((id) => series.some((candidate) => candidate.seriesId === id && candidate.tenantId === item.tenantId)), item.seriesId]
                  : current.filter((id) => id !== item.seriesId));
              }}
            />
            <span>{item.name}</span>
            <small>{t("{value0} 个视频可检索", { value0: item.readyVideoCount })}</small>
          </label>
        ))}
        {!series.length && !workspaceError && <p>{t(workspaceLoading ? "正在读取视频系列…" : "资料库还没有视频系列")}</p>}
      </div>
    </details>
    {emptyPublicScope && <span id={scopeStatusId} className={styles.scopeEmpty} role="status">{t("暂无可体验的公开系列")}</span>}
  </>;
  const answerSettingsPicker = displayMode === "debug" ? <details
    className={`${styles.scopePicker} ${styles.answerSettingsPicker}`}
    ref={answerSettingsRef}
    onToggle={(event) => {
      if (event.currentTarget.open) scopePickerRef.current?.removeAttribute("open");
    }}
  >
    <summary aria-label={t("配置本次回答")}>
      <SlidersHorizontal size={13} aria-hidden="true" />
      <span>{t("回答设置")}</span>
      <small>{messageOptionSummary}</small>
      <ChevronDown size={13} aria-hidden="true" />
    </summary>
    <div className={`${styles.scopeOptions} ${styles.answerSettings}`} role="group" aria-label={t("本次回答设置")}>
      <header>
        <div><strong>{t("本次回答")}</strong><small>{t("自动使用全局运行限制；自定义可限制最多轮次。")}</small></div>
        <button
          type="button"
          disabled={!messageOptionConfig || !messageOptions || searching}
          onClick={() => {
            if (!messageOptionConfig) return;
            customLoopTurnsRef.current = messageOptionConfig.maxLoopTurns.defaultValue;
            setMessageOptionOverrides({});
          }}
        >{t("恢复默认")}</button>
      </header>
      {messageOptionConfig && messageOptions ? (
        <>
          <div className={styles.answerMode}>
            <span><strong>{t("运行轮次")}</strong><small>{t("自定义最多可设 {count} 轮", { count: messageOptionConfig.maxLoopTurns.maximum })}</small></span>
            <div className={styles.answerModeChoices} role="group" aria-label={t("运行轮次模式")}>
              <button type="button" aria-pressed={messageOptions.maxLoopTurns === 0} onClick={() => {
                if (messageOptions.maxLoopTurns > 0) customLoopTurnsRef.current = messageOptions.maxLoopTurns;
                setMessageOptionOverrides((current) => {
                  if (!current) return current;
                  const next = { ...current };
                  delete next.maxLoopTurns;
                  return next;
                });
              }}>{t("自动")}</button>
              <button type="button" aria-pressed={messageOptions.maxLoopTurns > 0} onClick={() => {
                setMessageOptionOverrides((current) => current && ({ ...current, maxLoopTurns: customLoopTurnsRef.current }));
              }}>{t("自定义")}</button>
            </div>
          </div>
          {messageOptions.maxLoopTurns > 0 ? (
            <label className={styles.answerSettingRow}>
              <span><strong>{t("最多轮次")}</strong><small>{t("检索与核验的循环上限")}</small></span>
              <span className={styles.answerSettingValue}>{messageOptions.maxLoopTurns}</span>
              <input
                aria-label={t("最多运行轮次")}
                type="range"
                min={messageOptionConfig.maxLoopTurns.minimum}
                max={messageOptionConfig.maxLoopTurns.maximum}
                value={messageOptions.maxLoopTurns}
                onChange={(event) => {
                  const maxLoopTurns = event.currentTarget.valueAsNumber;
                  customLoopTurnsRef.current = maxLoopTurns;
                  setMessageOptionOverrides((current) => current && ({ ...current, maxLoopTurns }));
                }}
              />
            </label>
          ) : null}
        </>
      ) : <p role={messageOptionsError ? "alert" : "status"}>{messageOptionsError ? t("暂时无法读取设置：{value0}", { value0: messageOptionsError }) : t("正在读取服务端限制…")}</p>}
    </div>
  </details> : null;
  // Only a real running answer can be stopped. While a finished turn is still closing its POST,
  // the composer stays visible but disabled, matching the hook's own send guard.
  const canStop = messages.some((message) => message.role === "ASSISTANT" && message.status === "PROCESSING");
  const sendControl = canStop ? (
    <button className={styles.sendButton} type="button" disabled={cancelling} onClick={() => void stop?.()} aria-label={cancelling ? t("正在停止") : t("停止生成")} title={cancelling ? t("正在停止") : t("停止生成")}>
      {cancelling ? <LoaderCircle size={18} className={styles.spinning} aria-hidden="true" /> : <Square size={16} aria-hidden="true" />}
    </button>
  ) : (
    <button className={styles.sendButton} type="submit" disabled={!canSend} aria-label={t("发送问题")}>
      <ArrowUp size={19} aria-hidden="true" />
    </button>
  );
  const modelSettingsLink = (text: boolean) => {
    const label = t(text ? "配置模型" : "回答模型设置");
    const content = <><SlidersHorizontal size={15} aria-hidden="true" />{text ? label : null}</>;
    const className = text ? styles.configureModel : styles.modelSettingsLink;
    const section = text ? "models" : "model-roles";
    return inRouter ? <SettingsLink className={className} section={section} aria-label={label} title={label}>{content}</SettingsLink>
      : <a className={className} href={`/settings?section=${section}`} aria-label={label} title={label}>{content}</a>;
  };
  const modelChooser = !canConfigure ? null : hasModels ? <>
    <div className={styles.modelPicker}>
      <PersonalModelSelect connections={connections} value={modelId} label={t("回答模型")} placeholder={t("选择模型")}
        disabled={modelSaving || loading || creatingConversation} busy={modelSaving} onChange={(id) => void selectModel(id)} />
    </div>
    {modelSettingsLink(false)}
  </> : modelSettingsLink(true);

  const Root = embedded ? "section" : "main";
  return (
    <Root className={`${styles.page} ${embedded ? styles.embeddedPage : styles.standalonePage}`} data-draft={!embedded && showWelcome} data-sidebar={!embedded && !compactLayout && sidebarOpen} data-resizing={!embedded && sidebarResizing ? "true" : undefined} style={sidebarStyle}>
      {embedded ? (
        <header className={styles.pageHeader}>
          <div className={styles.chatHeading}>
            <h2 className={styles.chatTitle} title={activeConversation?.title ?? t("当前对话")}>
              {activeConversation?.title ?? t("当前对话")}
            </h2>
          </div>
          <div className={styles.conversationActions}>
            <p className="sr-only" role="status" aria-live="polite">{conversationStatus}</p>
            <details
              className={styles.conversationPicker}
              ref={conversationPickerRef}
              onKeyDown={(event) => {
                if (event.key !== "Escape") return;
                event.preventDefault();
                if (confirmingDeleteId) {
                  setConfirmingDeleteId(null);
                  deleteTriggerRef.current?.focus();
                  return;
                }
                closeConversationPicker(true);
              }}
            >
              <summary aria-label={t("打开对话记录")}>
                <History size={15} aria-hidden="true" />
                <span>{t("对话记录")}</span>
                <ChevronDown size={13} aria-hidden="true" />
              </summary>
              {conversationMenu}
            </details>
            {newConversationButton}
          </div>
        </header>
      ) : <>
        {compactLayout ? <dialog ref={historyDialogRef} className={styles.historyDrawer} aria-label={t("对话侧栏")}
          onCancel={(event) => { event.preventDefault(); closeConversationPicker(true); }}
          onClose={() => setHistoryDrawerOpen(false)}
          onClick={(event) => { if (event.target === event.currentTarget) closeConversationPicker(true); }}>
          {historyDrawerOpen ? sidebarContent : null}
        </dialog> : <aside id="search-history-sidebar" className={styles.historySidebar} aria-label={t("对话侧栏")} hidden={!sidebarOpen}>
          {sidebarOpen ? <div
            className={styles.historyResize}
            role="separator"
            tabIndex={0}
            aria-label={t("调整对话侧栏宽度")}
            aria-orientation="vertical"
            aria-valuemin={SIDEBAR_MIN_WIDTH}
            aria-valuemax={Math.round(maxSidebarWidth)}
            aria-valuenow={Math.round(visibleSidebarWidth)}
            title={t("调整对话侧栏宽度")}
            onPointerDown={(event) => {
              if (event.button !== 0) return;
              event.preventDefault();
              event.currentTarget.focus({ preventScroll: true });
              sidebarDragRef.current = { startX: event.clientX, startWidth: visibleSidebarWidth };
              setSidebarResizing(true);
              event.currentTarget.setPointerCapture?.(event.pointerId);
            }}
            onPointerMove={(event) => { if (sidebarDragRef.current) resizeSidebar(event.clientX); }}
            onPointerUp={endSidebarResize}
            onPointerCancel={endSidebarResize}
            onLostPointerCapture={() => { sidebarDragRef.current = null; setSidebarResizing(false); }}
            onKeyDown={(event) => {
              if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
              event.preventDefault();
              const delta = event.key === "ArrowRight" ? 24 : -24;
              setSidebarWidth(Math.round(Math.max(SIDEBAR_MIN_WIDTH, Math.min(maxSidebarWidth, visibleSidebarWidth + delta))));
            }}
          /> : null}
          {sidebarContent}
        </aside>}
        <header className={styles.pageHeader}>
          {compactLayout ? <h1 className="sr-only">{t("Seekino 助手")}</h1> : null}
          <button ref={historyToggleRef} className={styles.historyToggle} type="button" aria-label={t(!compactLayout && sidebarOpen ? "收起对话记录" : "打开对话记录")}
            aria-expanded={compactLayout ? historyDrawerOpen : sidebarOpen}
            onClick={() => compactLayout ? setHistoryDrawerOpen(true) : setSidebarOpen((open) => !open)}>
            <PanelLeft size={19} aria-hidden="true" />
          </button>
          <span className={styles.currentTitle}>{!showWelcome ? activeConversation?.title ?? t("Seekino 助手") : ""}</span>
          <p className="sr-only" role="status" aria-live="polite">{conversationStatus}</p>
        </header>
      </>}

      <div className={selectedHit && !onOpenVideo ? `${styles.workspace} ${styles.withEvidence}` : styles.workspace}>
        <section className={styles.agentPanel} aria-label={t("与 Seekino 助手对话")}>
          <div className={styles.thread} ref={threadRef} data-kino-stage="" onScroll={onThreadScroll}>
            {showWelcome ? (
              <div className={styles.welcome}>
                <Kino interactive perch dozeAfterMs={45_000} width={64} scale={3} title={t("戳一戳 Kino")} />
                <p>{embedded ? t("看到哪里，问到哪里。回答里的时间点可以直接点击跳转。") : t("描述记得的画面、台词或人物关系，一起把那一刻找回来。")}</p>
                <div className={styles.examples} aria-label={t("试试这样问")}>
                  {(embedded ? EMBEDDED_EXAMPLE_QUESTIONS : EXAMPLE_QUESTIONS).map((example) => <button key={example} type="button" disabled={mode !== "live" || loading} onClick={() => {
                    pendingExampleRef.current = true;
                    draftRevisionRef.current += 1;
                    setQuery(t(example));
                  }}>{t(example)}</button>)}
                </div>
              </div>
            ) : null}
            <SeekContext.Provider value={seekContext}>
              {messages.map((message) => {
                const kinoHost = message.messageId === kinoHostId;
                return <MessageRow
                  key={message.messageId}
                  message={message}
                  steps={progress[message.messageId] ?? EMPTY_STEPS}
                  reasoning={reasoning[message.messageId] ?? ""}
                  displayMode={displayMode}
                  kinoHost={kinoHost}
                  kinoAwake={kinoHost && connecting}
                  selectedChunkId={selection?.messageId === message.messageId ? selectedHit?.evidence.videoChunkId ?? null : null}
                  series={series}
                  onOpenVideo={onOpenVideo}
                  onSelect={setSelection}
                />;
              })}
            </SeekContext.Provider>
            {loading || connecting ? <p className={styles.connectionState} role="status">{kinoHostId ? null : <Kino state="loading" width={38} scale={2} ground={false} delayed perch />}{loading ? t("正在恢复会话…") : t("正在连接助手…")}</p> : null}
            {error && !messages.some((message) => message.status === "FAILED" && message.lastError?.message === error) ? <p ref={conversationFailureRef} className={`${styles.messageError} ${styles.conversationError}`} role="alert" tabIndex={-1}><Kino state="oops" width={38} scale={2} ground={false} /><span>{error}</span></p> : null}
          </div>

          <div className={styles.composerArea}>
            {composerContext}
            <form className={styles.composer} onSubmit={submit}>
              {attachments.items.length || reference ? <div className={styles.draftContext}>
              {attachments.items.length ? <ul className={styles.draftAttachments} aria-label={t("待发送图片")}>
                {attachments.items.map((item) => <li key={item.id} className={styles.draftAttachment}>
                  <img src={item.previewUrl} alt={item.file.name || t("待发送图片")} />
                  <button type="button" className={styles.removeAttachment} aria-label={t("移除 {value0}", { value0: item.file.name || t("图片") })} onClick={() => {
                    attachments.remove(item.id);
                    composerRef.current?.focus();
                  }}><X size={13} aria-hidden="true" /></button>
                  <span className={styles.attachmentName} title={item.file.name}>{item.file.name || t("图片")}</span>
                  {item.status === "uploading" ? <span className={styles.attachmentStatus} role="status"><LoaderCircle size={12} className={styles.spinning} aria-hidden="true" />{t("上传中")}</span>
                    : item.status === "failed" ? <div className={styles.attachmentFailure}>
                      <span role="alert">{item.error}</span>
                      <button type="button" onClick={() => attachments.retry(item.id)} aria-label={t("重试上传 {value0}", { value0: item.file.name || t("图片") })}>{t("重试")}</button>
                    </div> : <span className={styles.attachmentStatus}>{t("已上传")}</span>}
                </li>)}
              </ul> : null}
              {reference ? <div className={styles.referenceChip}><span>{t("已引用{reference}", { reference: referenceHit && referenceRange ? ` ${referenceHit.evidence.seriesName} · ${episodeLabel(referenceHit.evidence.episodeOrder)} · ${formatTimeRange(referenceRange.startSeconds, referenceRange.endSeconds)}` : t("选中片段") })}</span><button type="button" onClick={() => { draftRevisionRef.current += 1; setReference(null); }} aria-label={t("取消引用")}><X size={13} /></button></div> : null}
              </div> : null}
              <textarea
                ref={composerRef}
                data-standalone-composer={embedded ? undefined : true}
                value={query}
                onChange={(event) => { draftRevisionRef.current += 1; setQuery(event.target.value); }}
                onPaste={(event) => {
                  const files = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
                  if (!canAttach || !files.length) return;
                  if (!event.clipboardData.getData("text/plain")) event.preventDefault();
                  void addFiles(files);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                placeholder={embedded ? undefined : showWelcome ? t("从一个问题开始…") : t("继续提问…")}
                rows={1}
                aria-label={t("向 Seekino 助手提问")}
              />
              <div className={styles.composerToolbar}>
                {embedded ? (
                  <div ref={composerToolsRef} className={styles.composerTools} onKeyDown={(event) => {
                    if (event.key !== "Escape" || !composerToolsOpen) return;
                    event.preventDefault();
                    event.stopPropagation();
                    setComposerToolsOpen(false);
                    composerToolsRef.current?.querySelector("button")?.focus();
                  }}>
                    <button type="button" className={styles.toolsToggle} aria-label={t("展开输入选项")} aria-expanded={composerToolsOpen} onClick={() => setComposerToolsOpen((open) => !open)}>
                      <ChevronRight size={18} aria-hidden="true" />
                    </button>
                    <div className={styles.composerControls} hidden={!composerToolsOpen}>
                      {attachmentControls}{scopePicker}{answerSettingsPicker}{modelChooser}
                    </div>
                  </div>
                ) : <div className={styles.composerControls}>{attachmentControls}</div>}
                {sendControl}
              </div>
            </form>
            {!embedded ? <div className={styles.composerOptions}>
              <div className={styles.composerOptionsStart}>
                {scopePicker}
                {answerSettingsPicker}
              </div>
              {modelChooser}
            </div> : null}
            {canConfigure && modelCatalog && "error" in modelCatalog && <p className={styles.messageError} role="alert">
              {t("读取模型设置失败：{value0}", { value0: errorMessage(modelCatalog.error, locale) })}
            </p>}
            {canConfigure && (modelError || modelSelectionInvalid) ? (
              <p className={`${styles.messageError} ${styles.modelError}`} role="alert">
                {modelError ?? t("已保存的模型当前不可用，请重新选择。")}
              </p>
            ) : null}
            <p className={styles.keyboardHint}>{t("Enter 发送 · Shift + Enter 换行")}</p>
            {!workspaceLoading && ((!scopeValid && !emptyPublicScope) || !referenceScopeValid) ? <p className={styles.messageError} role="alert">
              {workspaceError && !series.length
                ? t("读取视频系列失败：{message}", { message: errorMessage(workspaceError, locale) })
                : t("所选课程暂不可访问，请重新选择范围。")}
            </p> : null}
            {embedded && initialSeriesId && !workspaceLoading && initialSeries?.status !== "READY" ? (
              <p className={styles.scopeNotice} role="status">
                <span className={styles.scopeChip}>{t("检索范围：")}{selectedSeriesIds.length ? selectedSeriesIds.map((id) => series.find((item) => item.seriesId === id)?.name ?? id).join(locale === "en" ? ", " : "、") : t("全部系列")}</span>
                <span className={styles.scopeWarning}><TriangleAlert size={13} aria-hidden="true" />{t("本系列未完成解析")}</span>
                {canManage && initialSeries?.access === "member" ? inRouter ? <Link to="/library">{t("去解析")}</Link> : <a href="/library">{t("去解析")}</a> : null}
              </p>
            ) : null}
          </div>
        </section>

        {!onOpenVideo && selectedHit && evidenceMessage && selectedBlock?.type === "video" ? (
          <section className={styles.evidenceColumn} aria-label={t("片段详情")} onKeyDown={(event) => {
            if (event.key === "Escape") setSelection(null);
          }}>
            <header className={styles.evidenceHeader}>
              <strong>{t("片段详情")}</strong>
              <button type="button" onClick={() => setSelection(null)} aria-label={t("关闭片段详情")}><X size={18} /></button>
            </header>
            <EvidencePlayer hit={selectedHit} displayMode={displayMode} playbackRange={{ startSeconds: selectedBlock.startSeconds, endSeconds: selectedBlock.endSeconds }} />
            <button className={`button-secondary ${styles.referenceButton}`} type="button" onClick={() => {
              draftRevisionRef.current += 1;
              setReference({ messageId: evidenceMessage.messageId, videoChunkId: selectedHit.evidence.videoChunkId });
              if (requiresExplicitScope || selectedHit.evidence.tenantId !== TENANT_ID || selectedSeriesIds.length > 0) {
                scopeTouchedRef.current = true;
                setSelectedSeriesIds([selectedHit.evidence.seriesId]);
              }
              setSelection(null);
              composerRef.current?.focus();
            }}>{t("引用此片段追问")}</button>
          </section>
        ) : null}
      </div>
    </Root>
  );
});
