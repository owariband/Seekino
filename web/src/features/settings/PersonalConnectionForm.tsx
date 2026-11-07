import { useState, type FormEvent } from "react";
import type { PersonalModelConnection, PersonalModelConnectionInput, PersonalModelProtocol } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import styles from "./personal.module.css";

export const protocols = {
  openai_chat: { label: "OpenAI Chat Completions", url: "https://api.openai.com/v1" },
  openai_responses: { label: "OpenAI Responses", url: "https://api.openai.com/v1" },
  anthropic_messages: { label: "Anthropic Messages", url: "https://api.anthropic.com" },
  gemini: { label: "Google Gemini", url: "https://generativelanguage.googleapis.com/v1beta" },
} satisfies Record<PersonalModelProtocol, { label: string; url: string }>;

export function PersonalConnectionForm({ connection, busy, cancel, save }: {
  connection?: PersonalModelConnection;
  busy: boolean;
  cancel?: () => void;
  save: (data: PersonalModelConnectionInput) => void;
}) {
  const { t } = useI18n();
  const [protocol, setProtocol] = useState<PersonalModelProtocol>(connection?.protocol ?? "openai_chat");
  const [name, setName] = useState(connection?.name ?? "");
  const [url, setUrl] = useState(connection?.baseUrl ?? protocols[protocol].url);
  const [key, setKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [models, setModels] = useState(connection?.models.map((m) => m.modelId).join("\n") ?? "");
  const [vision, setVision] = useState(connection?.supportsVision ?? false);
  const [error, setError] = useState("");
  const destinationChanged = connection && (connection.protocol !== protocol || connection.baseUrl !== url.trim().replace(/\/+$/, ""));
  const selectProtocol = (value: PersonalModelProtocol) => {
    if (!url.trim() || url === protocols[protocol].url) setUrl(protocols[value].url);
    setProtocol(value);
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const modelIds = Array.from(new Set(models.split("\n").map((m) => m.trim()).filter(Boolean)));
    if (!name.trim() || !modelIds.length) { setError(t("请填写服务名称和至少一个模型 ID。")); return; }
    if (connection?.hasApiKey && destinationChanged && !key.trim() && !clearKey) {
      setError(t("接口已更改，请重新输入密钥或选择清除密钥。")); return;
    }
    setError("");
    save({ name: name.trim(), protocol, baseUrl: url.trim(), modelIds, supportsVision: vision,
      revision: connection?.revision ?? 0, ...(clearKey ? { apiKey: "" } : key.trim() ? { apiKey: key.trim() } : {}) });
  };
  return <form className={styles.form} onSubmit={submit}>
    <div className={styles.row}>
      <label className={styles.field}><span>{t("接口协议")}</span>
        <select value={protocol} disabled={busy} onChange={(e) => selectProtocol(e.target.value as PersonalModelProtocol)}>
          {Object.entries(protocols).map(([id, item]) => <option key={id} value={id}>{item.label}</option>)}
        </select>
      </label>
      <label className={styles.field}><span>{t("服务名称")}</span>
        <input autoComplete="off" required maxLength={255} disabled={busy} value={name} onChange={(e) => setName(e.target.value)} />
      </label>
    </div>
    <label className={styles.field}><span>Base URL</span>
      <input dir="ltr" type="url" required spellCheck={false} maxLength={2048} disabled={busy} value={url} onChange={(e) => setUrl(e.target.value)} />
      <small>{t("可以填写官方、兼容或自托管服务的 API 地址。")}</small>
    </label>
    <label className={styles.field}><span>API Key</span>
      <input dir="ltr" type="password" autoComplete="new-password" maxLength={8192} disabled={busy || clearKey}
        value={key} onChange={(e) => setKey(e.target.value)}
        placeholder={t(connection?.hasApiKey ? "已保存，留空保留原密钥" : "服务无需密钥时可留空")} />
    </label>
    {connection?.hasApiKey ? <label className={styles.checks}><input type="checkbox" checked={clearKey} disabled={busy}
      onChange={(e) => { setClearKey(e.target.checked); setKey(""); }} />{t("清除已保存的密钥")}</label> : null}
    <label className={styles.field}><span>{t("模型 ID")}</span>
      <textarea dir="ltr" required spellCheck={false} rows={2} value={models} disabled={busy} onChange={(e) => setModels(e.target.value)} />
      <small>{t("填写服务提供的模型 ID，每行一个。")}</small>
    </label>
    <label className={styles.checks}><input type="checkbox" checked={vision} disabled={busy} onChange={(e) => setVision(e.target.checked)} />
      {t("模型支持图片输入")}</label>
    {error ? <p role="alert" className={styles.error}>{error}</p> : null}
    <div className={styles.actions}>{cancel ? <button type="button" className={styles.button} onClick={cancel} disabled={busy}>{t("取消")}</button> : null}
      <button type="submit" className={styles.primary} disabled={busy}>{t("保存连接")}</button>
    </div>
  </form>;
}
