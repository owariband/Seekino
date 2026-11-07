import { Cable, Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { errorMessage, getAgentMessageOptions, getPersonalModelSettings, removePersonalModelConnection,
  savePersonalModelConnection, savePersonalModelDefault, testPersonalModelConnection } from "../../api/client";
import type { PersonalModelConnection, PersonalModelSettings } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
import { useActiveChatSettings } from "./activeChatSettings";
import { PersonalConnectionForm, protocols } from "./PersonalConnectionForm";
import styles from "./personal.module.css";

export function AnswerModelSettings({ active }: { active: boolean }) {
  const { scopeKey } = useAuth();
  const { mode } = useWorkspace();
  const { t } = useI18n();
  if (!active) return null;
  if (mode !== "live") return <p role="status" className={styles.feedback}>{t(mode === "offline" ? "当前资料连接不可用。" : "正在连接…")}</p>;
  return <ConnectionSettings key={scopeKey} />;
}

function ConnectionSettings() {
  const { t, locale } = useI18n();
  const chat = useActiveChatSettings();
  const [settings, setSettings] = useState<PersonalModelSettings | null>(null);
  const [legacy, setLegacy] = useState<{ id: string; label: string }[]>([]);
  const [edit, setEdit] = useState<{ id: string; previous?: PersonalModelConnection } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [refresh, setRefresh] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const localeRef = useRef(locale);
  localeRef.current = locale;
  const fromChat = Boolean(chat);

  useEffect(() => {
    const lifetime = new AbortController();
    controller.current = lifetime;
    setError("");
    getPersonalModelSettings(lifetime.signal).then((saved) => {
      if (lifetime.signal.aborted) return;
      setSettings(saved);
      if (!saved.connections.length) setEdit({ id: crypto.randomUUID() });
    }).catch((e) => { if (!lifetime.signal.aborted) setError(errorMessage(e, localeRef.current)); });
    // Old chats can still have a platform model selected.
    if (fromChat) void getAgentMessageOptions().then((options) => {
      if (!lifetime.signal.aborted) setLegacy((options.chatModels ?? []).filter((m) => m.id !== "default" && !m.id.startsWith("personal:")));
    }).catch(() => {});
    return () => { lifetime.abort(); };
  }, [fromChat, refresh]);

  async function action<T>(run: (signal: AbortSignal) => Promise<T>, done: (value: T) => void) {
    const signal = controller.current?.signal;
    if (!signal || signal.aborted || inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(""); setFeedback("");
    try {
      const result = await run(signal);
      if (!signal.aborted) done(result);
    } catch (e) {
      if (!signal.aborted) setError(errorMessage(e, localeRef.current));
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
    setError(""); setFeedback("");
    setEdit({ id: previous?.connectionId ?? crypto.randomUUID(), previous });
  };
  const personalChoices = settings?.connections.map((c) => <optgroup key={c.connectionId} label={c.name}>
    {c.models.map((m) => <option value={m.id} key={m.id}>{m.modelId}</option>)}
  </optgroup>);
  return <div className={styles.workspace}>
    {error && <p className={styles.error} role="alert">{error} {!settings && <button type="button" className={styles.button}
      onClick={() => setRefresh((n) => n + 1)}>{t("重试")}</button>}</p>}
    {feedback && <p className={styles.feedback} role="status">{feedback}</p>}
    {!settings && !error && <p className={styles.feedback} role="status">{t("正在读取模型服务…")}</p>}
    {settings && <>
      {edit ? <>
        <div className={styles.toolbar}><h3 className={styles.lead}>{t(edit.previous ? "编辑模型服务" : "添加模型服务")}</h3></div>
        <PersonalConnectionForm key={edit.id} connection={edit.previous} busy={busy} cancel={settings.connections.length ? () => setEdit(null) : undefined}
          save={(body) => void action((signal) => savePersonalModelConnection(edit.id, body, signal), (value) => {
            saved(value); setEdit(null); setFeedback(t("已保存连接"));
          })} />
      </> : <>
        <div className={styles.toolbar}><h3 className={styles.lead}>{t("我的模型服务")}</h3>
          <button type="button" className={styles.primary} disabled={busy} onClick={() => open()}><Plus size={16} />{t("添加模型服务")}</button>
        </div>
        <div className={styles.list}>{settings.connections.map((c) => <ConnectionCard key={c.connectionId} connection={c} busy={busy}
          edit={() => open(c)} remove={() => void action((signal) => removePersonalModelConnection(c.connectionId, c.revision, signal), (value) => {
            saved(value); setFeedback(t("已删除连接")); if (!value.connections.length) open();
          })} test={(model) => void action((signal) => testPersonalModelConnection(c.connectionId, model, signal), () => setFeedback(t("已收到模型回复")))} />)}</div>
      </>}
      {!edit && <div className={styles.modelSelection}>
        <label className={styles.field}><span>{t("新对话默认模型")}</span>
          <select disabled={busy} value={settings.defaultModelId}
            onChange={(e) => { const id = e.target.value; void action((signal) => savePersonalModelDefault(id, signal), saved); }}>
            <option value="default">{t("跟随服务默认")}</option>{personalChoices}
          </select>
        </label>
      </div>}
      {chat && (!edit || !settings.connections.length) && <div className={styles.modelSelection}>
        <label className={styles.field}><span>{t("当前对话模型")}</span>
          <select disabled={busy || chat.modelSaving || chat.loading} value={chat.modelId} onChange={(e) => void chat.selectModel(e.target.value)}>
            <option value="default">{t("跟随服务默认")}</option>{personalChoices}
            {legacy.length > 0 && <optgroup label={t("平台提供的模型")}>{legacy.map((m) => <option value={m.id} key={m.id}>{m.label}</option>)}</optgroup>}
            {chat.modelId !== "default" && !legacy.some((m) => m.id === chat.modelId) && !settings.connections.some((c) => c.models.some((m) => m.id === chat.modelId))
              && <option disabled value={chat.modelId}>{t("已保存的模型（当前不可用）")}</option>}
          </select>
        </label>
        {chat.modelError && <p className={styles.error} role="alert">{chat.modelError}</p>}
      </div>}
    </>}
  </div>;
}

function ConnectionCard({ connection: c, busy, edit, remove, test }: {
  connection: PersonalModelConnection; busy: boolean; edit: () => void; remove: () => void; test: (model: string) => void;
}) {
  const { t } = useI18n();
  const [confirm, setConfirm] = useState(false);
  const [choice, setChoice] = useState("");
  const selected = c.models.find((m) => m.id === choice) ?? c.models[0];
  return <article className={styles.card}>
    <h4 className={styles.title}><Cable size={18} aria-hidden="true" />{c.name}</h4>
    <div className={styles.meta}>{protocols[c.protocol].label} · {c.baseUrl}</div>
    <div className={styles.row}>
      {c.models.length > 1 ? <select aria-label={t("测试使用的模型")} value={selected?.id ?? ""} disabled={busy} onChange={(e) => setChoice(e.target.value)}>
        {c.models.map((m) => <option value={m.id} key={m.id}>{m.modelId}</option>)}</select> : <span className={styles.meta}>{selected?.modelId}</span>}
      <div className={styles.actions}>
        <button type="button" className={styles.button} disabled={busy || !selected} onClick={() => selected && test(selected.id)}>{t("测试连接")}</button>
        <button type="button" className={styles.button} disabled={busy} onClick={edit}>{t("编辑")}</button>
        <button type="button" className={styles.button} disabled={busy} onClick={() => setConfirm(true)}>{t("删除")}</button>
      </div>
    </div>
    {confirm && <div className={styles.row}><span>{t("删除这个模型服务？")}</span>
      <div className={styles.actions}><button type="button" className={styles.button} onClick={() => setConfirm(false)} disabled={busy}>{t("取消")}</button>
        <button type="button" className={styles.danger} onClick={remove} disabled={busy}>{t("确认删除")}</button></div>
    </div>}
  </article>;
}
