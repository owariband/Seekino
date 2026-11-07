import {
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronRight,
  Clapperboard,
  Database,
  Moon,
  Palette,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Sun,
  X,
  type LucideIcon,
} from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

import type { ThemeMode } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import { useTheme } from "../../app/theme";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
import { useWatchPreferences } from "../player/watchPreferences";
import { AnswerModelSettings } from "./AnswerModelSettings";
import { GlobalAgentSettings } from "./GlobalAgentSettings";
import styles from "./settings.module.css";

type SettingsSection = "appearance" | "watch" | "models" | "connection" | "agent";
type SettingsSectionDefinition = {
  id: SettingsSection;
  label: string;
  summary: string;
  description: string;
  icon: LucideIcon;
};
const settingsSections: ReadonlyArray<SettingsSectionDefinition> = [
  { id: "appearance", label: "外观与语言", summary: "主题与界面语言", description: "主题与界面语言会立即生效，并保存在此浏览器。", icon: Palette },
  { id: "watch", label: "观看与 Kino", summary: "Kino 与播放偏好", description: "调整 Kino 侧栏的自动淡化与发送消息时的播放行为。", icon: Clapperboard },
  { id: "models", label: "回答模型", summary: "服务默认与当前对话", description: "查看服务提供的回答模型。", icon: Sparkles },
  { id: "connection", label: "账号与连接", summary: "资料来源、账号与状态", description: "账号登录与资料来源分别管理，登录不会移动本机资料。", icon: Server },
  { id: "agent", label: "Agent 运行", summary: "全局默认与运行参数", description: "保存到服务端 config.yaml，供所有工作区后续新请求使用。", icon: SlidersHorizontal },
];

const themeOptions = [
  { value: "light", label: "白天模式", icon: Sun },
  { value: "dark", label: "黑夜模式", icon: Moon },
] as const satisfies ReadonlyArray<{ value: ThemeMode; label: string; icon: LucideIcon }>;
const localeOptions = [
  { value: "zh", code: "ZH", label: "中文", lang: "zh-CN" },
  { value: "en", code: "EN", label: "English", lang: "en" },
] as const;

function isSettingsSection(value: string | null): value is SettingsSection {
  return settingsSections.some((section) => section.id === value);
}

function SectionShell({
  definition,
  hidden,
  headingRef,
  children,
}: {
  definition: SettingsSectionDefinition;
  hidden: boolean;
  headingRef: (node: HTMLHeadingElement | null) => void;
  children: ReactNode;
}) {
  const { t } = useI18n();
  const Icon = definition.icon;
  return (
    <section className={styles.panel} hidden={hidden} aria-labelledby={`settings-${definition.id}-heading`}>
      <header className={styles.panelHeader}>
        <span className={styles.panelIcon}><Icon size={18} aria-hidden="true" /></span>
        <div className={styles.panelHeadingText}>
          <h2 id={`settings-${definition.id}-heading`} className={styles.panelHeading} tabIndex={-1} ref={headingRef}>
            {t(definition.label)}
          </h2>
          <p className={styles.panelDescription}>{t(definition.description)}</p>
        </div>
      </header>
      {children}
    </section>
  );
}

function AppearanceContent() {
  const { t, locale, setLocale } = useI18n();
  const { theme, setTheme } = useTheme();
  const themeGroupName = useId();
  const localeGroupName = useId();

  return (
    <div className={styles.group}>
      <div className={`${styles.row} ${styles.rowStackNarrow}`}>
        <div className={styles.rowText}>
          <span className={styles.rowLabel}>{t("主题模式")}</span>
        </div>
        <div className={styles.segmented} role="radiogroup" aria-label={t("主题模式")}>
          {themeOptions.map((option) => {
            const Icon = option.icon;
            const checked = theme === option.value;
            return <label className={styles.segment} key={option.value}>
              <input className={styles.segmentInput} type="radio" name={themeGroupName} value={option.value}
                checked={checked} aria-label={t(option.label)} onChange={() => setTheme(option.value)} />
              <Icon size={15} aria-hidden="true" />
              <span>{t(option.label)}</span>
              {checked ? <Check className={styles.segmentCheck} size={14} aria-hidden="true" /> : null}
            </label>;
          })}
        </div>
      </div>
      <div className={`${styles.row} ${styles.rowStackNarrow}`}>
        <div className={styles.rowText}>
          <span className={styles.rowLabel}>{t("界面语言")}</span>
        </div>
        <div className={styles.segmented} role="radiogroup" aria-label={t("界面语言")}>
          {localeOptions.map((option) => {
            const checked = locale === option.value;
            return <label className={styles.segment} key={option.value}>
              <input className={styles.segmentInput} type="radio" name={localeGroupName} value={option.value}
                checked={checked} aria-label={`${option.code} ${option.label}`} onChange={() => setLocale(option.value)} />
              <strong aria-hidden="true">{option.code}</strong>
              <span lang={option.lang}>{option.label}</span>
              {checked ? <Check className={styles.segmentCheck} size={14} aria-hidden="true" /> : null}
            </label>;
          })}
        </div>
      </div>
    </div>
  );
}

function WatchContent() {
  const { t } = useI18n();
  const { pauseOnSend, setPauseOnSend, fadeDelay, setFadeDelay } = useWatchPreferences();
  const fadeInputId = useId();
  const fadeErrorId = useId();
  const fadeInputRef = useRef<HTMLInputElement>(null);
  const [fadeDraft, setFadeDraft] = useState(() => String(fadeDelay));
  const [fadeInvalid, setFadeInvalid] = useState(false);

  useEffect(() => {
    if (document.activeElement === fadeInputRef.current) return;
    setFadeDraft(String(fadeDelay));
    setFadeInvalid(false);
  }, [fadeDelay]);

  const commitFadeDelay = () => {
    const value = Number(fadeDraft);
    if (!fadeDraft.trim() || !Number.isFinite(value) || value < 1) {
      setFadeInvalid(true);
      return;
    }
    setFadeInvalid(false);
    setFadeDraft(String(value));
    setFadeDelay(value);
  };

  return (
    <>
      <div className={styles.group}>
        <label className={styles.row}>
          <span className={styles.rowText}>
            <span className={styles.rowLabel}>{t("发送问题时暂停视频")}</span>
            <span className={styles.rowDescription}>{t("发送消息时先暂停播放，回到播放器后继续。")}</span>
          </span>
          <input className={styles.switch} type="checkbox" role="switch" checked={pauseOnSend}
            aria-label={t("发送问题时暂停视频")} onChange={(event) => setPauseOnSend(event.currentTarget.checked)} />
        </label>
        <div className={`${styles.row} ${styles.rowStackNarrow}`}>
          <label className={styles.rowText} htmlFor={fadeInputId}>
            <span className={styles.rowLabel}>{t("自动淡化时间（秒）")}</span>
            <span className={styles.rowDescription}>{t("Kino 面板空闲后等待多久才淡化。")}</span>
          </label>
          <div className={styles.rowControl}>
            <input ref={fadeInputRef} id={fadeInputId} className={styles.numberInput} type="number" inputMode="decimal"
              min={1} step="any" value={fadeDraft} aria-label={t("自动淡化时间（秒）")}
              aria-invalid={fadeInvalid} aria-describedby={fadeInvalid ? fadeErrorId : undefined}
              onChange={(event) => { setFadeDraft(event.currentTarget.value); setFadeInvalid(false); }}
              onBlur={commitFadeDelay}
              onKeyDown={(event) => {
                if (event.key === "Enter") { event.preventDefault(); commitFadeDelay(); }
                else if (event.key === "Escape") {
                  event.preventDefault();
                  setFadeDraft(String(fadeDelay));
                  setFadeInvalid(false);
                  event.currentTarget.blur();
                }
              }} />
          </div>
        </div>
      </div>
      {fadeInvalid ? <p id={fadeErrorId} className={styles.fieldError} role="alert">{t("请输入不小于 1 秒的有效时长。")}</p> : null}
    </>
  );
}

function ConnectionContent({ showDebug }: { showDebug: boolean }) {
  const { t } = useI18n();
  const { mode } = useWorkspace();
  const { activeSource, activeTenantId, accountIdentity, accountConnection, accountStatus, accountError, retryAccount } = useAuth();
  const shownApiUrl = new URL(activeSource.connection.baseUrl || "/", window.location.origin).href.replace(/\/$/, "");
  const accountLabel = accountStatus === "unconfigured" ? t("未连接账号服务")
    : accountStatus === "checking" ? t("正在连接")
    : accountStatus === "unavailable" ? t("连接不可用")
    : accountIdentity && !accountIdentity.isGuest ? accountIdentity.email || t("已登录")
    : t(accountIdentity?.emailLoginEnabled === false ? "游客模式" : "未登录");

  return (
    <>
      <h3 className={styles.groupTitle}>{t("连接")}</h3>
      <dl className={styles.connectionList}>
        <div className={styles.connectionRow}>
          <dt><Database size={15} aria-hidden="true" />{t("资料来源")}</dt>
          <dd>{t(activeSource.kind === "local" ? "本机资料" : activeSource.kind === "public" ? "公开资料" : "我的资料")}</dd>
        </div>
        <div className={styles.connectionRow}>
          <dt><Server size={15} aria-hidden="true" />{t("资料连接")}</dt>
          <dd className={styles.statusValue} data-mode={mode}><i aria-hidden="true" />{t(mode === "live" ? "已连接" : mode === "offline" ? "不可用" : "正在检查")}</dd>
        </div>
        <div className={styles.connectionRow}>
          <dt><ShieldCheck size={15} aria-hidden="true" />{t("账号")}</dt>
          <dd>{accountLabel}</dd>
        </div>
        {showDebug ? <div className={styles.connectionRow}>
          <dt><Database size={15} aria-hidden="true" />{t("资料地址")}</dt>
          <dd className={styles.codeValue}>{shownApiUrl}</dd>
        </div> : null}
        {showDebug ? <div className={styles.connectionRow}>
          <dt><ShieldCheck size={15} aria-hidden="true" />{t("资料空间")}</dt>
          <dd className={styles.codeValue}>{activeTenantId}</dd>
        </div> : null}
        {showDebug && accountConnection ? <div className={styles.connectionRow}>
          <dt><Server size={15} aria-hidden="true" />{t("账号地址")}</dt>
          <dd className={styles.codeValue}>{new URL(accountConnection.baseUrl || "/", window.location.origin).href.replace(/\/$/, "")}</dd>
        </div> : null}
      </dl>

      {accountStatus === "unavailable" ? <div className={styles.modeNote}>
        <span>{accountError || t("账号服务暂时不可用。")}</span>
        <button className="button-secondary" type="button" onClick={retryAccount}>{t("重新连接")}</button>
      </div> : null}
      {mode === "offline" ? <p className={styles.modeNote}>{t("当前资料连接不可用。请检查服务和网络；恢复连接后会重新读取资料和任务状态。")}</p> : null}

      {showDebug ? <details className={styles.disclosure}>
        <summary className={styles.disclosureSummary}>
          <ShieldCheck size={15} aria-hidden="true" />
          <span>{t("数据边界")}</span>
          <ChevronDown className={styles.disclosureChevron} size={15} aria-hidden="true" />
        </summary>
        <div className={styles.disclosureBody}>
          <p>{t("前端只通过公开 HTTP 接口访问媒体，不尝试读取内部 artifact URI。")}</p>
          <div className={styles.factList}>
            <div><strong>{t("上传确认")}</strong><p>{t("分片经过 SHA-256 校验，并由服务端确认后才进入已上传进度。")}</p></div>
            <div><strong>{t("检索理由")}</strong><p>{t("只根据 Evidence、召回通道与排名生成可核验摘要，不展示或伪造私有思维链。")}</p></div>
            <div><strong>{t("断线恢复")}</strong><p>{t("SSE 只负责通知；恢复后重新获取 MySQL 快照，并以服务端状态为准。")}</p></div>
          </div>
        </div>
      </details> : null}
    </>
  );
}

export function SettingsPage({ onClose }: { onClose?: () => void } = {}) {
  const { t } = useI18n();
  const { mode, displayMode } = useWorkspace();
  const { canConfigure } = useAuth();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [compact, setCompact] = useState(() => window.matchMedia("(max-width: 760px)").matches);
  const [agentVisited, setAgentVisited] = useState(() =>
    searchParams.get("section") === "agent" && displayMode === "debug" && canConfigure,
  );
  const pendingFocusRef = useRef<"section" | "directory" | null>(null);
  const directoryHeadingRef = useRef<HTMLHeadingElement>(null);
  const headingRefs = useRef(new Map<SettingsSection, HTMLHeadingElement>());
  const showAgent = displayMode === "debug" && canConfigure;

  useEffect(() => {
    const media = window.matchMedia("(max-width: 760px)");
    const sync = () => setCompact(media.matches);
    media.addEventListener("change", sync);
    window.addEventListener("resize", sync);
    return () => {
      media.removeEventListener("change", sync);
      window.removeEventListener("resize", sync);
    };
  }, []);

  const rawSection = searchParams.get("section");
  const requestedSection = isSettingsSection(rawSection) ? rawSection : null;
  const sectionAvailable = requestedSection !== null && (requestedSection !== "agent" || showAgent);
  const section: SettingsSection | null = sectionAvailable ? requestedSection : compact ? null : "appearance";

  useEffect(() => {
    if (section === "agent" && showAgent) setAgentVisited(true);
  }, [section, showAgent]);

  useEffect(() => {
    const pending = pendingFocusRef.current;
    if (!pending) return;
    pendingFocusRef.current = null;
    if (!compact) return;
    if (pending === "directory") directoryHeadingRef.current?.focus();
    else if (section) headingRefs.current.get(section)?.focus();
  }, [compact, section]);

  const writeSection = (next: SettingsSection | null) => {
    const params = new URLSearchParams(searchParams);
    if (next) params.set("section", next);
    else params.delete("section");
    const search = params.toString();
    navigate({ pathname: location.pathname, search: search ? `?${search}` : "", hash: location.hash }, {
      replace: true,
      state: location.state,
    });
  };

  const openSection = (next: SettingsSection) => {
    if (requestedSection === next) return;
    if (next === "agent") setAgentVisited(true);
    if (compact) pendingFocusRef.current = "section";
    writeSection(next);
  };

  const showDirectory = () => {
    if (requestedSection === null) return;
    if (compact) pendingFocusRef.current = "directory";
    writeSection(null);
  };

  const visibleSections = settingsSections.filter((definition) => definition.id !== "agent" || showAgent);
  const agentMounted = showAgent && (agentVisited || section === "agent");
  const definitionFor = (id: SettingsSection) => settingsSections.find((definition) => definition.id === id)!;
  const headingRef = (id: SettingsSection) => (node: HTMLHeadingElement | null) => {
    if (node) headingRefs.current.set(id, node);
    else headingRefs.current.delete(id);
  };

  const Root = onClose ? "div" : "main";
  return (
    <Root className={`page ${styles.page}`}>
      <header className={styles.header}>
        <h1 className={styles.title}>{t("设置")}</h1>
        {onClose ? <button className={styles.closeButton} type="button" onClick={onClose} aria-label={t("关闭设置")} title={t("关闭设置")}>
          <X size={20} aria-hidden="true" />
        </button> : null}
      </header>

      <div className={styles.workspace} data-mobile-view={section ? "detail" : "directory"}>
        <nav className={styles.navigation} aria-label={t("设置分类")} hidden={compact && section !== null}>
          <h2 className={styles.navHeading} ref={directoryHeadingRef} tabIndex={-1}>{t("设置分类")}</h2>
          <div className={styles.navList}>
            {visibleSections.map((definition) => {
              const Icon = definition.icon;
              const active = section === definition.id;
              return <button className={styles.navItem} key={definition.id} type="button"
                aria-label={t(definition.label)} aria-current={active ? "page" : undefined}
                onClick={() => openSection(definition.id)}>
                <Icon className={styles.navIcon} size={18} aria-hidden="true" />
                <span className={styles.navText}>
                  <span className={styles.navLabel}>{t(definition.label)}</span>
                  <span className={styles.navSummary}>{t(definition.summary)}</span>
                </span>
                <ChevronRight className={styles.navChevron} size={16} aria-hidden="true" />
              </button>;
            })}
          </div>
        </nav>

        <div className={styles.detail} hidden={compact && section === null}>
          {compact && section ? <div className={styles.backRow}>
            <button className={styles.backButton} type="button" onClick={showDirectory} aria-label={t("返回设置分类")}>
              <ArrowLeft size={17} aria-hidden="true" />{t("设置分类")}
            </button>
          </div> : null}

          <SectionShell definition={definitionFor("appearance")} hidden={section !== "appearance"} headingRef={headingRef("appearance")}>
            <AppearanceContent />
          </SectionShell>
          <SectionShell definition={definitionFor("watch")} hidden={section !== "watch"} headingRef={headingRef("watch")}>
            <WatchContent />
          </SectionShell>
          <SectionShell definition={definitionFor("models")} hidden={section !== "models"} headingRef={headingRef("models")}>
            <AnswerModelSettings active={section === "models"} />
          </SectionShell>
          <SectionShell definition={definitionFor("connection")} hidden={section !== "connection"} headingRef={headingRef("connection")}>
            <ConnectionContent showDebug={showAgent} />
          </SectionShell>
          {agentMounted ? <SectionShell definition={definitionFor("agent")} hidden={section !== "agent"} headingRef={headingRef("agent")}>
            <GlobalAgentSettings mode={mode} />
          </SectionShell> : null}
        </div>
      </div>
    </Root>
  );
}
