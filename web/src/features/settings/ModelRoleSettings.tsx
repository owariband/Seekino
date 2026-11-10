import { ChevronDown, Clapperboard, MessageCircle, Search } from "lucide-react";

import type { ModelRole, ModelRoleSetting, PersonalModelSettings } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import styles from "./personal.module.css";

const groups = [
  { label: "对话", icon: MessageCircle, roles: ["chat"], note: "用于之后新建的对话。" },
  { label: "视频解析", icon: Clapperboard, roles: ["vision", "transcription", "ocr"], note: "重启 API 后用于新任务，已创建的解析任务沿用原选择。" },
  { label: "检索", icon: Search, roles: ["embedding", "rerank"], note: "检索配置在重启 API 后生效。更换向量模型或维度时，需要切换或重建匹配的索引。" },
] as const;

const labels: Record<ModelRole, string> = {
  chat: "新对话默认模型",
  vision: "画面理解",
  transcription: "语音转写",
  ocr: "画面文字识别（OCR）",
  embedding: "索引向量",
  rerank: "检索重排",
};

type Props = {
  settings: PersonalModelSettings;
  busy: boolean;
  choose: (role: ModelRole, id: string | null) => void;
};

function RoleRow({ current, connections, busy, choose }: {
  current: ModelRoleSetting;
  connections: PersonalModelSettings["connections"];
} & Pick<Props, "busy" | "choose">) {
  const { t } = useI18n();
  const { role, modelId, overridden } = current;
  const candidates = connections.map((connection) => ({
    connection,
    models: connection.models.filter((model) => model.available !== false && model.roles?.includes(role)),
  })).filter((group) => group.models.length);
  const source = connections.find((connection) => connection.models.some((model) => model.id === modelId));
  const selected = source?.models.find((model) => model.id === modelId);
  const unavailable = overridden && modelId !== null && !candidates.some((group) => group.models.some((model) => model.id === modelId));
  const modelName = selected?.label || selected?.modelId || t(unavailable ? "已选模型当前不可用" : "未指定");
  const sourceName = source && (overridden ? source.name : t("原配置 · {model}", { model: source.name }));
  const selectId = `model-role-${role}`;
  const description = [sourceName && `${selectId}-source`, !candidates.length && `${selectId}-empty`].filter(Boolean).join(" ");

  return <div className={styles.roleRow}>
    <label htmlFor={selectId}>{t(labels[role])}</label>
    <div className={styles.roleControl}>
      <div className={styles.roleChoice}>
        <select id={selectId} data-menu value={overridden ? modelId ?? "" : ""}
          title={source && selected ? `${source.name} / ${selected.modelId}` : modelName}
          aria-describedby={description || undefined}
          disabled={busy || (!candidates.length && !overridden)}
          onChange={(event) => choose(role, event.currentTarget.value || null)}>
          <option value="">{overridden ? t("恢复原配置") : selected && source
            ? t("原配置 · {model}", { model: `${source.name} / ${selected.modelId}` }) : t("未指定")}</option>
          {unavailable && <option value={modelId} disabled>{t("已选模型当前不可用")}</option>}
          {candidates.map(({ connection, models }) => <optgroup key={connection.connectionId} label={connection.name}>
            {models.map((model) => <option key={model.id} value={model.id}>
              {model.label && model.label !== model.modelId ? `${model.label} · ${model.modelId}` : model.modelId}
            </option>)}
          </optgroup>)}
        </select>
        <span className={styles.roleFace}>
          <span className={styles.roleValue}>
            <strong aria-hidden="true">{modelName}</strong>
            {sourceName && <small id={`${selectId}-source`}>{sourceName}
              {selected && selected.modelId !== modelName && ` · ${selected.modelId}`}
              {unavailable && ` · ${t("已选模型当前不可用")}`}
            </small>}
          </span>
          <ChevronDown size={15} aria-hidden="true" />
        </span>
      </div>
      {!candidates.length && <small id={`${selectId}-empty`}>{t("尚无可用于此用途的模型。")}</small>}
    </div>
  </div>;
}

export function ModelRoleSettings({ settings, busy, choose }: Props) {
  const { t } = useI18n();
  if (!settings.roles) return <p role="status" className={styles.feedback}>{t("当前服务尚未提供用途设置。")}</p>;
  const roles = settings.roles;
  return <div className={styles.roleGroups}>
    {groups.map(({ label, icon: Icon, roles: members, note }) => {
      const rows = members.flatMap((role) => roles.filter((row) => row.role === role));
      if (!rows.length) return null;
      return <section key={label}>
        <h3 className={styles.roleHeading}><Icon size={16} aria-hidden="true" />{t(label)}</h3>
        <div className={styles.roleRows}>
          {rows.map((current) => <RoleRow key={current.role} current={current}
            connections={settings.connections} busy={busy} choose={choose} />)}
        </div>
        <p className={styles.roleNote}>{t(note)}</p>
      </section>;
    })}
  </div>;
}
