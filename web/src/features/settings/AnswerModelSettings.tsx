import { Cable, Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { errorMessage, getPersonalModelSettings, removePersonalModelConnection,
  savePersonalModelConnection, savePersonalModelDefault, testPersonalModelConnection } from "../../api/client";
import type { PersonalModelConnection, PersonalModelSettings } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
import { useActiveChatSettings } from "./activeChatSettings";
import { PersonalModelSelect } from "./PersonalModelSelect";
import { MASKED_API_KEY, PersonalConnectionForm, protocols } from "./PersonalConnectionForm";
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
  const [emptyDraftId] = useState(() => crypto.randomUUID());
  const [chosenEditor, setEdit] = useState<{ id: string; previous?: PersonalModelConnection } | null>(null);
  const edit = chosenEditor ?? (settings?.connections.length === 0 ? { id: emptyDraftId, previous: undefined } : null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [refresh, setRefresh] = useState(0);
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
  const yamlDefault = settings?.connections.flatMap((connection) => connection.models).find((model) => model.isYamlDefault && model.available !== false);
  const personalChoices = settings?.connections.filter((c) => c.models.some((m) => m.available !== false)).map((c) => <optgroup key={c.connectionId} label={c.name}>
    {c.models.filter((m) => m.available !== false).map((m) => <option value={m.id} key={m.id}>{m.modelId}</option>)}
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
            saved(value); setFeedback(t("已删除连接")); if (!value.connections.length) setEdit(null);
          })} test={(model) => void action((signal) => testPersonalModelConnection(c.connectionId, model, signal), () => setFeedback(t("已收到模型回复")))} />)}</div>
      </>}
      {!edit && <div className={styles.modelSelection}>
        <label className={styles.field}><span>{t("新对话默认模型")}</span>
          <select aria-label={t("新对话默认模型")} disabled={busy} value={settings.defaultModelId}
            onChange={(e) => { const id = e.target.value; void action((signal) => savePersonalModelDefault(id, signal), saved); }}>
            <option value="default">{yamlDefault ? t("最近选择或 YAML 默认（{value0}）", { value0: yamlDefault.modelId }) : t("使用最近选择的模型")}</option>{personalChoices}
          </select>
        </label>
      </div>}
      {chat && !edit && settings.connections.length > 0 && <div className={styles.modelSelection}>
        <label className={styles.field}><span>{t("当前对话模型")}</span>
          <PersonalModelSelect connections={settings.connections} value={chat.modelId} label={t("当前对话模型")}
            placeholder={t("选择模型")} disabled={busy || chat.modelSaving || chat.loading} onChange={(id) => void chat.selectModel(id)} />
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
  const selected = c.models.find((m) => m.id === choice && m.available !== false) ?? c.models.find((m) => m.available !== false) ?? c.models[0];
  const fromYaml = c.origin === "yaml";
  const protocolLabel = c.protocol === "codex" ? "Codex CLI" : protocols[c.protocol].label;
  return <article className={styles.card}>
    <div className={styles.connectionHeading}>
      <h4 className={styles.title}><Cable size={18} aria-hidden="true" />{c.name}</h4>
      {fromYaml && <span className={styles.source}>{t("来自 YAML")}</span>}
    </div>
    <div className={styles.meta}>{protocolLabel}{c.baseUrl ? ` · ${c.baseUrl}` : ""}</div>
    {c.protocol !== "codex" && <div className={styles.credential}>
      <span>API Key</span>{c.hasApiKey ? <span aria-label={t("API Key 已配置")} className={styles.keyMask}>{MASKED_API_KEY}</span>
        : <span>{t("未设置")}</span>}
    </div>}
    <div className={styles.row}>
      {c.models.length > 1 ? <select aria-label={t("测试使用的模型")} value={selected?.id ?? ""} disabled={busy} onChange={(e) => setChoice(e.target.value)}>
        {c.models.map((m) => <option value={m.id} key={m.id} disabled={m.available === false}>{m.label ? `${m.label} · ${m.modelId}` : m.modelId}</option>)}</select> : <span className={styles.meta}>{selected?.modelId}</span>}
      <div className={styles.actions}>
        {c.protocol !== "codex" && <button type="button" className={styles.button} disabled={busy || !selected || selected.available === false}
          onClick={() => selected && test(selected.id)}>{t("测试连接")}</button>}
        {!fromYaml && <>
          <button type="button" className={styles.button} disabled={busy} onClick={edit}>{t("编辑")}</button>
          <button type="button" className={styles.button} disabled={busy} onClick={() => setConfirm(true)}>{t("删除")}</button>
        </>}
      </div>
    </div>
    {confirm && <div className={styles.row}><span>{t("删除这个模型服务？")}</span>
      <div className={styles.actions}><button type="button" className={styles.button} onClick={() => setConfirm(false)} disabled={busy}>{t("取消")}</button>
        <button type="button" className={styles.danger} onClick={remove} disabled={busy}>{t("确认删除")}</button></div>
    </div>}
  </article>;
}
