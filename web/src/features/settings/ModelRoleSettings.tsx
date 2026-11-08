import { useI18n } from "../../app/i18n";
import type { ModelRole, PersonalModelSettings } from "../../api/contracts";
import styles from "./personal.module.css";

const groups: { name: string; roles: ModelRole[] }[] = [
  { name: "对话", roles: ["chat"] },
  { name: "视频解析", roles: ["vision", "transcription", "ocr"] },
  { name: "检索", roles: ["embedding", "rerank"] },
];
const labels: Record<ModelRole, string> = {
  chat: "新对话默认模型", vision: "画面理解", transcription: "语音转写", ocr: "画面文字识别（OCR）", embedding: "索引向量", rerank: "检索重排",
};

export function ModelRoleSettings({ settings, busy, choose, openServices }: {
  settings: PersonalModelSettings; busy: boolean; choose: (role: ModelRole, id: string | null) => void; openServices?: () => void;
}) {
  const { t } = useI18n();
  if (!settings.roles) return <p role="status" className={styles.feedback}>{t("当前服务尚未提供用途设置。")}</p>;
  return <>
    {groups.map((group) => <section key={group.name} className={styles.roleGroup}>
      <h3 className={styles.lead}>{t(group.name)}</h3>
      {group.roles.map((role) => {
        const current = settings.roles!.find((row) => row.role === role);
        if (!current) return null;
        const candidates = settings.connections.map((c) => ({ name: c.name, id: c.connectionId,
          models: c.models.filter((m) => m.available !== false && m.roles?.includes(role)) })).filter((c) => c.models.length);
        const connection = settings.connections.find((c) => c.models.some((m) => m.id === current.modelId));
        const selected = connection?.models.find((m) => m.id === current.modelId);
        const currentName = selected && connection ? `${connection.name} / ${selected.modelId}` : "";
        const available = candidates.some((c) => c.models.some((m) => m.id === current.modelId));
        return <div key={role} className={styles.roleRow}>
          <label htmlFor={`model-role-${role}`}>{t(labels[role])}</label>
          <div className={styles.field}>
            <select id={`model-role-${role}`} value={current.overridden ? current.modelId ?? "" : ""}
              disabled={busy || (!candidates.length && !current.overridden)}
              onChange={(event) => choose(role, event.currentTarget.value || null)}>
              <option value="">{current.overridden ? t("恢复原配置") : currentName ? t("原配置 · {model}", { model: currentName }) : t("未指定")}</option>
              {current.overridden && current.modelId && !available && <option value={current.modelId} disabled>{t("已选模型当前不可用")}</option>}
              {candidates.map((c) => <optgroup key={c.id} label={c.name}>
                {c.models.map((m) => <option key={m.id} value={m.id}>{c.name} / {m.label ? `${m.label} · ${m.modelId}` : m.modelId}</option>)}
              </optgroup>)}
            </select>
            {!candidates.length && <small>{t("尚无可用于此用途的模型。")}</small>}
          </div>
        </div>;
      })}
      <p className={styles.roleNote}>{t(group.name === "对话" ? "用于之后新建的对话。"
        : group.name === "视频解析" ? "重启 API 后用于新任务，已创建的解析任务沿用原选择。"
        : "检索配置在重启 API 后生效。更换向量模型或维度时，需要切换或重建匹配的索引。")}</p>
    </section>)}
    {openServices && <button type="button" className={styles.addModel} onClick={openServices}>{t("管理模型服务")}</button>}
  </>;
}
