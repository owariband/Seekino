import { Plus, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { discoverModelIds, errorMessage } from "../../api/client";
import type { PersonalModelConnection, PersonalModelConnectionInput, PersonalModelProtocol, RegistryModelKind } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import styles from "./personal.module.css";

export const MASKED_API_KEY = "••••••••";

const kinds: RegistryModelKind[] = ["generative", "transcription", "embedding", "rerank"];
const generative: RegistryModelKind[] = ["generative", "rerank"];
export const modelKinds: Record<string, string> = {
  generative: "生成模型", transcription: "语音转写", embedding: "向量模型", rerank: "检索重排",
  diarization: "说话人分离", speaker_embedding: "说话人向量",
};
export const protocols: Record<PersonalModelProtocol, { label: string; url: string; kinds: RegistryModelKind[] }> = {
  openai_chat: { label: "OpenAI 兼容接口", url: "https://api.openai.com/v1", kinds },
  openai_responses: { label: "OpenAI Responses", url: "https://api.openai.com/v1", kinds: generative },
  anthropic_messages: { label: "Anthropic Messages", url: "https://api.anthropic.com", kinds: generative },
  gemini: { label: "Google Gemini", url: "https://generativelanguage.googleapis.com/v1beta", kinds: generative },
  openai_audio: { label: "OpenAI Audio Transcriptions", url: "https://api.openai.com/v1", kinds: ["transcription"] },
  ollama_embedding: { label: "Ollama Embeddings", url: "http://localhost:11434", kinds: ["embedding"] },
  ark: { label: "Volcengine Ark", url: "https://ark.cn-beijing.volces.com/api/v3", kinds },
};
const nativeProtocols = { codex: "Codex CLI", openai_vision: "OpenAI Vision", llama_cpp_embedding: "llama.cpp",
  whisperx: "WhisperX", pyannote: "pyannote", campplus: "CAM++", claude_sdk: "Claude Code" };
export function protocolName(protocol: PersonalModelConnection["protocol"]) {
  return protocol in protocols ? protocols[protocol as PersonalModelProtocol].label : nativeProtocols[protocol as keyof typeof nativeProtocols];
}

type Row = { key: string; modelId: string; kind: RegistryModelKind; supportsVision: boolean; dimensions: string };
function modelRow(kind: RegistryModelKind, modelId = ""): Row {
  return { key: crypto.randomUUID(), modelId, kind, supportsVision: false, dimensions: "" };
}

export function PersonalConnectionForm({ id, connection, busy, cancel, save }: {
  id: string; connection?: PersonalModelConnection; busy: boolean; cancel?: () => void;
  save: (data: PersonalModelConnectionInput) => void;
}) {
  const { t, locale } = useI18n();
  const native = connection?.editable === false ? connection : undefined;
  const nativeKinds: RegistryModelKind[] = native?.protocol === "whisperx" ? ["transcription"]
    : native?.protocol === "llama_cpp_embedding" ? ["embedding"] : generative;
  const initial = connection?.protocol && connection.protocol in protocols ? connection.protocol as PersonalModelProtocol : "openai_chat";
  const [protocol, setProtocol] = useState(initial);
  const allowedKinds = connection?.modelKinds?.length && (native || connection.protocol === protocol)
    ? connection.modelKinds : native ? nativeKinds : protocols[protocol].kinds;
  const [name, setName] = useState(connection?.name ?? "");
  const [url, setUrl] = useState(connection?.baseUrl ?? protocols[initial].url);
  const [key, setKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [rows, setRows] = useState<Row[]>(() => {
    const items = new Map<string, Row>();
    for (const m of connection?.models ?? []) {
      const kind = (m.kind ?? "generative") as RegistryModelKind;
      const identity = JSON.stringify([m.modelId, kind]);
      const row = items.get(identity) ?? modelRow(kind, m.modelId);
      row.supportsVision ||= m.supportsVision ?? connection?.supportsVision ?? false;
      row.dimensions = m.dimensions ? String(m.dimensions) : "";
      items.set(identity, row);
    }
    return items.size ? Array.from(items.values()) : [modelRow(allowedKinds[0]!)];
  });
  const [error, setError] = useState("");
  const [suggestions, setSuggestions] = useState<string[] | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    setSuggestions(null); setDiscovering(false); setError("");
    return () => request.current?.abort();
  }, [protocol, url, key, clearKey]);

  function update(row: Row, values: Partial<Row>) {
    setRows((items) => items.map((item) => item.key === row.key ? { ...item, ...values } : item));
  }
  function changeProtocol(next: PersonalModelProtocol) {
    if (next === "ark" || !url.trim() || url === protocols[protocol].url) setUrl(protocols[next].url);
    setProtocol(next);
    const nextKinds = connection?.protocol === next && connection.modelKinds?.length ? connection.modelKinds : protocols[next].kinds;
    setRows((items) => items.map((r) => nextKinds.includes(r.kind) ? r : { ...r, kind: nextKinds[0]!, supportsVision: false }));
  }
  function credentials() {
    return clearKey ? { apiKey: "" } : key.trim() ? { apiKey: key.trim() } : {};
  }
  function validDestination() {
    if (!native && connection?.hasApiKey && !key.trim() && !clearKey && (connection.protocol !== protocol || connection.baseUrl !== url.trim().replace(/\/+$/, ""))) {
      setError(t("接口已更改，请重新输入密钥或选择清除密钥。"));
      return false;
    }
    return true;
  }
  async function discover() {
    if (!validDestination()) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setError(""); setDiscovering(true); setSuggestions(null);
    try {
      const result = await discoverModelIds(id, { protocol, baseUrl: url.trim(), revision: connection?.revision ?? 0, ...credentials() }, controller.signal);
      if (!controller.signal.aborted) setSuggestions(result.modelIds);
    } catch (e) {
      if (!controller.signal.aborted) setError(errorMessage(e, locale));
    } finally {
      if (!controller.signal.aborted) setDiscovering(false);
    }
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!validDestination()) return;
    const models = rows.map((r) => ({ modelId: r.modelId.trim(), kind: r.kind,
      ...(r.kind === "generative" ? { supportsVision: r.supportsVision } : {}),
      ...(r.kind === "embedding" ? { dimensions: Number(r.dimensions) } : {}) }));
    if (!name.trim() || models.some((m) => !m.modelId)) { setError(t("请填写服务名称和至少一个模型 ID。")); return; }
    if (new Set(models.map((m) => JSON.stringify([m.modelId, m.kind]))).size !== models.length) {
      setError(t("同一类型的模型 ID 不能重复。")); return;
    }
    setError("");
    save({ name: name.trim(), protocol: native?.protocol ?? protocol, baseUrl: url.trim(), models,
      modelIds: Array.from(new Set(models.map((m) => m.modelId))), supportsVision: models.some((m) => m.supportsVision),
      revision: connection?.revision ?? 0, ...credentials() });
  }
  return <form className={styles.form} onSubmit={submit}>
    {native ? <div className={styles.connectionHeading}><strong>{native.name}</strong><span className={styles.meta}>{protocolName(native.protocol)}</span></div> : <><div className={styles.row}>
      <label className={styles.field}><span>{t("服务名称")}</span><input required maxLength={255} value={name} disabled={busy} onChange={(e) => setName(e.target.value)} /></label>
      <label className={styles.field}><span>{t("接口协议")}</span><select value={protocol} disabled={busy} onChange={(e) => changeProtocol(e.target.value as PersonalModelProtocol)}>
        {Object.entries(protocols).map(([code, p]) => <option key={code} value={code}>{t(p.label)}</option>)}
      </select></label>
    </div>
    <label className={styles.field}><span>Base URL</span><input dir="ltr" type="url" required spellCheck={false} readOnly={protocol === "ark"} disabled={busy} value={url} onChange={(e) => setUrl(e.target.value)} /></label>
    <label className={styles.field}><span>API Key</span><input dir="ltr" type="password" aria-label="API Key" autoComplete="new-password" maxLength={8192} disabled={busy || clearKey}
      value={key} onChange={(e) => setKey(e.target.value)} placeholder={connection?.hasApiKey && !clearKey ? MASKED_API_KEY : t("服务无需密钥时可留空")} />
      {connection?.hasApiKey && !clearKey && <small>{t("已保存，留空保留原密钥")}</small>}
    </label>
    {connection?.hasApiKey && <label className={styles.checks}><input type="checkbox" checked={clearKey} disabled={busy}
      onChange={(e) => { setClearKey(e.target.checked); setKey(""); }} />{t("清除已保存的密钥")}</label>}
    </>}
    <div className={styles.registryHeading}><h4>{t("已注册模型")}</h4>
      {!native && protocol !== "ark" && <button type="button" className={styles.button} disabled={busy || discovering || !url.trim()} onClick={() => void discover()}>
        {t(discovering ? "正在获取…" : "获取模型列表")}</button>}
    </div>
    {suggestions !== null && (suggestions.length
      ? <label className={styles.field}><span>{t("从服务添加模型")}</span><select value="" disabled={busy || rows.length >= 64} onChange={(e) => {
        const value = e.target.value; if (!value) return;
        const empty = rows.find((r) => !r.modelId); if (empty) update(empty, { modelId: value });
        else setRows([...rows, modelRow(allowedKinds[0]!, value)]);
      }}><option value="">{t("选择模型")}</option>{suggestions.map((name) => <option key={name} value={name}>{name}</option>)}</select></label>
      : <p className={styles.meta} role="status">{t("未获取到模型 ID，可手动填写。")}</p>)}
    {rows.map((r, index) => <fieldset className={styles.modelRow} key={r.key} disabled={busy}>
      <div className={styles.modelInputs}>
        <label className={styles.field}><span>{t("模型 ID")}</span><input dir="ltr" required maxLength={255} spellCheck={false} aria-label={index === 0 ? t("模型 ID") : t("模型 ID {n}", { n: index + 1 })} value={r.modelId} onChange={(e) => update(r, { modelId: e.target.value })} /></label>
        <label className={styles.field}><span>{t("模型类型")}</span><select value={r.kind} onChange={(e) => update(r, { kind: e.target.value as RegistryModelKind })}>
          {allowedKinds.map((kind) => <option key={kind} value={kind}>{t(modelKinds[kind]!)}</option>)}
        </select></label>
        {rows.length > 1 && <button type="button" className={styles.iconButton} aria-label={t("移除模型 {n}", { n: index + 1 })} onClick={() => setRows(rows.filter((item) => item.key !== r.key))}><X size={16} /></button>}
      </div>
      {r.kind === "generative" && native?.protocol !== "claude_sdk" && <label className={styles.checks}><input type="checkbox" checked={r.supportsVision} onChange={(e) => update(r, { supportsVision: e.target.checked })} />{t("模型支持图片输入")}</label>}
      {r.kind === "embedding" && <label className={styles.field}><span>{t("向量维度")}</span><input type="number" required min={1} max={2048} step={1} value={r.dimensions} onChange={(e) => update(r, { dimensions: e.target.value })} /></label>}
    </fieldset>)}
    <button type="button" className={styles.addModel} disabled={busy || rows.length >= 64} onClick={() => setRows([...rows, modelRow(allowedKinds[0]!)])}><Plus size={15}/>{t("添加模型")}</button>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    <div className={styles.actions}>{cancel && <button type="button" className={styles.button} disabled={busy} onClick={cancel}>{t("取消")}</button>}
      <button className={styles.primary} type="submit" disabled={busy}>{t("保存连接")}</button></div>
  </form>;
}
