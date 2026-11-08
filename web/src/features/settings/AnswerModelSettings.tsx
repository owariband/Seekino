import { AudioLines, Cable, ChevronRight, Layers, Pencil, Plus, SquareTerminal, Trash2, type LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiClientError, errorMessage, getPersonalModelSettings, removePersonalModelConnection,
  savePersonalModelConnection, saveModelRole, testPersonalModelConnection } from "../../api/client";
import type { PersonalModelConnection, PersonalModelSettings } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
import { useActiveChatSettings } from "./activeChatSettings";
import { PersonalModelSelect } from "./PersonalModelSelect";
import { MASKED_API_KEY, PersonalConnectionForm, modelKinds, protocolName } from "./PersonalConnectionForm";
import { ModelRoleSettings } from "./ModelRoleSettings";
import { chatConnections } from "./usePersonalChatModels";
import styles from "./personal.module.css";

type SectionProps = { section?: "services" | "roles" };
export function AnswerModelSettings({ active, section = "services" }: SectionProps & { active: boolean }) {
  const { scopeKey, canConfigure } = useAuth();
  const { mode } = useWorkspace();
  const { t } = useI18n();
  if (!active || !canConfigure) return null;
  if (mode !== "live") return <p role="status" className={styles.feedback}>{t(mode === "offline" ? "当前资料连接不可用。" : "正在连接…")}</p>;
  return <ConnectionSettings key={`${scopeKey}:${section}`} section={section} />;
}

function ConnectionSettings({ section }: SectionProps) {
  const { t, locale } = useI18n();
  const chat = useActiveChatSettings();
  const [settings, setSettings] = useState<PersonalModelSettings | null>(null);
  const [emptyDraftId] = useState(() => crypto.randomUUID());
  const [chosenEditor, setEdit] = useState<{ id: string; previous?: PersonalModelConnection } | null>(null);
  const edit = chosenEditor ?? (settings?.connections.length === 0 ? { id: emptyDraftId, previous: undefined } : null);
  const [expanded, setExpanded] = useState<string | null | undefined>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [conflict, setConflict] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const localeRef = useRef(locale);
  localeRef.current = locale;

  useEffect(() => {
    const lifetime = new AbortController();
    controller.current = lifetime;
    setError("");
    getPersonalModelSettings(lifetime.signal).then((saved) => {
      if (lifetime.signal.aborted) return;
      setSettings(saved);
    }).catch((e) => { if (!lifetime.signal.aborted) setError(errorMessage(e, localeRef.current)); });
    return () => { lifetime.abort(); };
  }, [refresh]);

  async function action<T>(run: (signal: AbortSignal) => Promise<T>, done: (value: T) => void) {
    const signal = controller.current?.signal;
    if (!signal || signal.aborted || inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(""); setFeedback(""); setConflict(false);
    try {
      const result = await run(signal);
      if (!signal.aborted) done(result);
    } catch (e) {
      if (!signal.aborted) {
        setError(errorMessage(e, localeRef.current));
        setConflict(e instanceof ApiClientError && e.status === 409);
      }
    } finally {
      inFlight.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }
  function saved(value: PersonalModelSettings) {
    setSettings(value);
    window.dispatchEvent(new Event("seekino:model-connections-changed"));
  }
  const open = (previous?: PersonalModelConnection) => {
    setError(""); setFeedback(""); setConflict(false);
    setEdit({ id: previous?.connectionId ?? crypto.randomUUID(), previous });
  };
  return <div className={styles.workspace}>
    {error && <p className={styles.error} role="alert">{error} {(!settings || conflict) && <button type="button" className={styles.button}
      onClick={() => { setEdit(null); setConflict(false); setRefresh((n) => n + 1); }}>{t(conflict ? "重新载入设置" : "重试")}</button>}</p>}
    {feedback && <p className={styles.feedback} role="status">{feedback}</p>}
    {!settings && !error && <p className={styles.feedback} role="status">{t("正在读取模型服务…")}</p>}
    {settings && <>
      {section === "services" && (edit ? <>
        <div className={styles.toolbar}><h3 className={styles.lead}>{t(edit.previous ? "编辑模型服务" : "添加模型服务")}</h3></div>
        <PersonalConnectionForm key={edit.id} id={edit.id} connection={edit.previous} busy={busy} cancel={settings.connections.length ? () => setEdit(null) : undefined}
          save={(body) => void action((signal) => savePersonalModelConnection(edit.id, body, signal), (value) => {
            saved(value); setExpanded(edit.id); setEdit(null); setFeedback(t("已保存连接"));
          })} />
      </> : <>
        <div className={styles.toolbar}><h3 className={styles.lead}>{t("{n} 个服务连接", { n: settings.connections.length })}</h3>
          <button type="button" className={styles.primary} disabled={busy} onClick={() => open()}><Plus size={16} />{t("添加模型服务")}</button>
        </div>
        <div className={styles.list}>{settings.connections.map((c, index) => <ConnectionCard key={c.connectionId} connection={c} busy={busy}
          expanded={expanded === undefined ? index === 0 : expanded === c.connectionId}
          toggle={(open) => setExpanded(open ? c.connectionId : null)}
          edit={() => open(c)} remove={() => void action((signal) => removePersonalModelConnection(c.connectionId, c.revision, signal), (value) => {
            saved(value); setExpanded(undefined); setFeedback(t("已删除连接")); if (!value.connections.length) setEdit(null);
          })} test={(model) => void action((signal) => testPersonalModelConnection(c.connectionId, model, signal), () => setFeedback(t("已收到模型回复")))} />)}</div>
      </>)}
      {section === "roles" && <ModelRoleSettings settings={settings} busy={busy}
        choose={(role, id) => {
          if (settings.revision === undefined) return;
          void action((signal) => saveModelRole(role, id, settings.revision!, signal), (next) => {
            saved(next); setFeedback(t("模型用途已保存"));
          });
        }} />}
      {section === "roles" && chat && chatConnections(settings.connections).length > 0 && <div className={styles.modelSelection}>
        <label className={styles.field}><span>{t("当前对话模型")}</span>
          <PersonalModelSelect connections={chatConnections(settings.connections)} value={chat.modelId} label={t("当前对话模型")}
            placeholder={t("选择模型")} disabled={busy || chat.modelSaving || chat.loading} onChange={(id) => void chat.selectModel(id)} />
        </label>
        {chat.modelError && <p className={styles.error} role="alert">{chat.modelError}</p>}
      </div>}
    </>}
  </div>;
}

const protocolIcons: Partial<Record<PersonalModelConnection["protocol"], LucideIcon>> = {
  openai_audio: AudioLines, whisperx: AudioLines,
  ollama_embedding: Layers, llama_cpp_embedding: Layers,
  pyannote: AudioLines, campplus: AudioLines,
  codex: SquareTerminal, claude_sdk: SquareTerminal,
};

function ConnectionCard({ connection: c, expanded, toggle, busy, edit, remove, test }: {
  expanded: boolean; toggle: (open: boolean) => void;
  connection: PersonalModelConnection; busy: boolean; edit: () => void; remove: () => void; test: (model: string) => void;
}) {
  const { t } = useI18n();
  const [confirm, setConfirm] = useState(false);
  const [choice, setChoice] = useState("");
  const testModels = c.models.filter((m) => m.testable ?? ((m.roles?.includes("chat") ?? true) && c.protocol !== "codex"));
  const selected = testModels.find((m) => m.id === choice) ?? testModels.find((m) => m.available !== false) ?? testModels[0];
  const editable = c.editable ?? c.protocol !== "codex";
  const hasCredentials = c.hasApiKey || ["openai_chat", "openai_responses", "anthropic_messages", "gemini", "ark", "openai_audio"].includes(c.protocol);
  const nameId = `service-name-${c.connectionId}`;
  const protocolId = `service-protocol-${c.connectionId}`;
  const countId = `service-count-${c.connectionId}`;
  const Icon = protocolIcons[c.protocol] ?? Cable;
  return <article className={styles.card}>
    <h4 className={styles.connectionTitle} aria-labelledby={nameId}><button className={styles.disclosure} type="button"
      aria-labelledby={nameId} aria-describedby={`${protocolId} ${countId}`}
      aria-expanded={expanded} aria-controls={`models-${c.connectionId}`} onClick={() => { setConfirm(false); toggle(!expanded); }}>
      <span className={styles.serviceIcon}><Icon size={17} aria-hidden="true" /></span>
      <span id={nameId} className={styles.serviceName}>{c.name}</span>
      <small id={protocolId} className={styles.protocol}>{t(protocolName(c.protocol))}</small>
      <small id={countId} className={styles.count}>{t("{n} 个模型", { n: c.models.length })}</small>
      <ChevronRight className={styles.chevron} size={16} aria-hidden="true" />
    </button></h4>
    <div className={styles.serviceDetails} id={`models-${c.connectionId}`} hidden={!expanded}>
    <div className={styles.endpointLine}>
    {c.baseUrl && <div className={styles.meta} dir="ltr">{c.baseUrl}</div>}
    {hasCredentials && <div className={styles.credential}><span>API Key</span>
      {c.hasApiKey ? <span aria-label={t("API Key 已配置")} className={styles.keyMask}>{MASKED_API_KEY}</span> : <span>{t("未设置")}</span>}
    </div>}
    </div>
    <div className={styles.modelList}>{c.models.map((m) => <div key={m.id} className={styles.modelItem}>
      <span dir="ltr">{m.modelId}</span><small>{t(modelKinds[m.kind ?? "generative"] ?? m.kind ?? "")}
        {m.supportsVision ? ` · ${t("图像")}` : ""}{m.dimensions ? ` · ${m.dimensions}D` : ""}</small>
    </div>)}</div>
    <div className={styles.row}>
      {testModels.length > 1 && <select
        aria-label={t("测试使用的模型")} value={selected?.id ?? ""} disabled={busy} onChange={(e) => setChoice(e.target.value)}>
        {testModels.map((m) => <option value={m.id} key={m.id} disabled={m.available === false}>{m.label ?? m.modelId}</option>)}
      </select>}
      <div className={styles.actions}>
        {selected && <button type="button" className={styles.button} disabled={busy || selected.available === false}
          onClick={() => test(selected.id)}>{t("测试连接")}</button>}
        {!editable && !["campplus", "pyannote"].includes(c.protocol) &&
          <button type="button" className={styles.button} disabled={busy} onClick={edit}>{t("管理模型")}</button>}
        {editable && <>
          <button type="button" className={styles.button} disabled={busy} onClick={edit}><Pencil size={13} aria-hidden="true" />{t("编辑")}</button>
          <button type="button" className={styles.remove} disabled={busy} onClick={() => setConfirm(true)}><Trash2 size={13} aria-hidden="true" />{t("删除")}</button>
        </>}
      </div>
    </div>
    {confirm && <div className={styles.row}><span>{t("删除这个模型服务？")}</span>
      <div className={styles.actions}><button type="button" className={styles.button} onClick={() => setConfirm(false)} disabled={busy}>{t("取消")}</button>
        <button type="button" className={styles.danger} onClick={remove} disabled={busy}>{t("确认删除")}</button></div>
    </div>}
    </div>
  </article>;
}
