import { CircleAlert, LoaderCircle } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { errorMessage, getAgentMessageOptions } from "../../api/client";
import { useI18n } from "../../app/i18n";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
import { chatModelOptions, type ChatModelOption } from "../search/chatModelOptions";
import { useActiveChatSettings } from "./activeChatSettings";
import styles from "./settings.module.css";

type ModelCatalog =
  | { status: "loading"; scope: string }
  | { status: "ready"; scope: string; models: ChatModelOption[] }
  | { status: "error"; scope: string; failure: unknown };

function ModelChoiceRow({
  interactive,
  checked,
  disabled,
  radioName,
  value,
  label,
  meta,
  onSelect,
}: {
  interactive: boolean;
  checked: boolean;
  disabled: boolean;
  radioName: string;
  value: string;
  label: string;
  meta: string | null;
  onSelect: () => void;
}) {
  const content = (
    <>
      {interactive ? <input className={styles.choiceInput} type="radio" name={radioName} value={value}
        checked={checked} disabled={disabled} aria-label={label} onChange={onSelect} /> : null}
      <span className={styles.choiceText}>
        <span className={styles.choiceLabel}>{label}</span>
        {meta ? <span className={styles.choiceMeta}>{meta}</span> : null}
      </span>
    </>
  );
  return interactive
    ? <label className={styles.choiceRow} data-checked={checked || undefined}>{content}</label>
    : <div className={styles.choiceRow} data-readonly="true">{content}</div>;
}

export function AnswerModelSettings({ active }: { active: boolean }) {
  const { t, locale } = useI18n();
  const { mode } = useWorkspace();
  const { scopeKey } = useAuth();
  const activeChat = useActiveChatSettings();
  const radioName = useId();
  const [opened, setOpened] = useState(active);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const requestRef = useRef(0);

  useEffect(() => {
    if (active) setOpened(true);
  }, [active]);

  useEffect(() => {
    if (!opened || mode !== "live") return;
    const requestId = ++requestRef.current;
    const scope = scopeKey;
    setCatalog({ status: "loading", scope });
    void getAgentMessageOptions().then((options) => {
      if (requestId !== requestRef.current) return;
      setCatalog({ status: "ready", scope, models: chatModelOptions(options) });
    }).catch((failure: unknown) => {
      if (requestId !== requestRef.current) return;
      setCatalog({ status: "error", scope, failure });
    });
    return () => { requestRef.current += 1; };
  }, [opened, mode, scopeKey, retryCount]);

  const scopedCatalog = catalog?.scope === scopeKey ? catalog : null;
  const live = mode === "live";
  const models = live && scopedCatalog?.status === "ready" ? scopedCatalog.models : null;
  const loading = live && (!scopedCatalog || scopedCatalog.status === "loading");
  const failure = live && scopedCatalog?.status === "error" ? scopedCatalog.failure : null;
  const choices = models ?? [];
  const defaultModel = choices.find((model) => model.id === "default");
  const availableModels = choices.filter((model) => model.id !== "default");
  const defaultLabel = defaultModel && defaultModel.label !== "default" ? defaultModel.label : null;
  const interactive = Boolean(activeChat && defaultModel);
  const busy = Boolean(activeChat && (activeChat.modelSaving || activeChat.loading));
  const activeModelId = activeChat?.modelId ?? "default";
  const modelError = activeChat?.modelError ?? null;
  const activeModel = choices.find((model) => model.id === activeModelId);
  const activeModelUnknown = Boolean(activeChat && defaultModel && activeModelId !== "default" && !activeModel);
  const chooseModel = (id: string) => {
    if (!activeChat || activeChat.modelSaving || activeChat.loading) return;
    void activeChat.selectModel(id).catch(() => { /* The hook exposes the failure through modelError. */ });
  };

  return (
    <>
      {activeChat?.modelSaving ? <p className={styles.modelSaving} role="status">
        <LoaderCircle className={styles.spinner} size={14} aria-hidden="true" />{t("正在保存回答模型…")}
      </p> : null}
      {!activeChat ? <p className={styles.modelState}>{t("在聊天中打开模型设置，可更改该对话的回答模型。")}</p> : null}
      {modelError ? <p className={styles.inlineAlert} role="alert">
        <CircleAlert size={15} aria-hidden="true" />{modelError}
      </p> : null}
      {activeModelUnknown && !modelError ? <p className={styles.inlineAlert} role="alert">
        <CircleAlert size={15} aria-hidden="true" />{t("已保存的模型当前不可用，请重新选择。")}
        <code>{activeModelId}</code>
      </p> : null}

      {mode === "checking" ? <p className={styles.modelState} role="status">{t("正在检查资料连接…")}</p> : null}
      {mode === "offline" ? <p className={styles.modelState}>{t("资料连接不可用，暂时无法读取模型列表。")}</p> : null}
      {live && loading ? <p className={styles.modelState} role="status">
        <LoaderCircle className={styles.spinner} size={14} aria-hidden="true" />{t("正在读取可用模型…")}
      </p> : null}
      {failure ? <div className={styles.modelFailure} role="alert">
        <span>{t("读取可用模型失败：{error}", { error: errorMessage(failure, locale) })}</span>
        <button className="button-secondary" type="button" onClick={() => setRetryCount((current) => current + 1)}>{t("重试")}</button>
      </div> : null}

      {models && !defaultModel ? <p className={styles.modelState}>{t("此服务未提供可选模型列表。")}</p> : null}
      {models && defaultModel ? (
        <div className={styles.modelChoices} role={interactive ? "radiogroup" : "group"}
          aria-label={t("回答模型")} aria-busy={busy || undefined}>
          <h3 className={styles.groupTitle}>{t(interactive ? "此对话的回答模型" : "服务默认")}</h3>
          <div className={styles.group}>
            <ModelChoiceRow interactive={interactive} checked={interactive && activeModelId === "default"}
              disabled={!interactive || busy} radioName={radioName} value="default" label={interactive ? t("跟随服务默认") : (defaultLabel ?? t("服务默认"))}
              meta={interactive ? defaultLabel : null} onSelect={() => chooseModel("default")} />
          </div>
          <h3 className={styles.groupTitle}>{t("可用模型")}</h3>
          <div className={styles.group}>
            {availableModels.length ? availableModels.map((model) => (
              <ModelChoiceRow key={model.id} interactive={interactive} checked={interactive && activeModelId === model.id}
                disabled={!interactive || busy} radioName={radioName} value={model.id} label={model.label} meta={null}
                onSelect={() => chooseModel(model.id)} />
            )) : <p className={styles.emptyModels}>{t("服务端没有提供其他可选模型。")}</p>}
          </div>
        </div>
      ) : null}
    </>
  );
}
