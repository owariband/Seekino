import { ChevronDown } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { ApiClientError, errorMessage, getAgentGlobalSettings, updateAgentGlobalSettings } from "../../api/client";
import type { AgentGlobalSettings, AgentGlobalSettingsValues, AgentReasoningEffort } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import type { MessageParams } from "../../shared/messages";
import styles from "./settings.module.css";

type ReasoningSettingKey = "query_reasoning_effort" | "supervisor_reasoning_effort";
type NumericSettingKey = Exclude<keyof AgentGlobalSettingsValues, "limits_enabled" | ReasoningSettingKey>;
type SettingField = readonly [NumericSettingKey, string, string];
type SettingsError = { message: string; params?: MessageParams; failure?: unknown };

const reasoningFields: ReadonlyArray<readonly [ReasoningSettingKey, string]> = [
  ["query_reasoning_effort", "Query 检索"],
  ["supervisor_reasoning_effort", "Supervisor 回答"],
];
const reasoningOptions: ReadonlyArray<readonly [AgentReasoningEffort, string]> = [
  ["none", "关闭推理"], ["low", "低"], ["high", "高"], ["max", "最大"],
];

const settingGroups: ReadonlyArray<{ title: string; fields: ReadonlyArray<SettingField> }> = [
  { title: "回答与检索", fields: [
    ["history_message_limit", "历史消息数量", "条"],
    ["max_model_calls_per_turn", "每轮默认模型调用", "次"],
    ["maximum_model_calls_per_turn", "每轮模型调用上限", "次"],
    ["max_tool_calls_per_turn", "每轮默认工具调用", "次"],
    ["maximum_tool_calls_per_turn", "每轮工具调用上限", "次"],
  ] },
  { title: "超时与并发", fields: [
    ["stage_timeout_seconds", "常规阶段超时", "秒"],
    ["supervisor_timeout_seconds", "Supervisor 超时", "秒"],
    ["execution_timeout_seconds", "整次执行超时", "秒"],
    ["maximum_concurrent_kicks", "并行任务上限", "个"],
    ["maximum_parallel_queries", "Query 并发上限", "个"],
  ] },
  { title: "模型上下文", fields: [
    ["max_output_tokens", "模型输出上限", "Token"],
    ["max_prompt_chars", "提示词字符上限", "字符"],
    ["context_window_tokens", "上下文窗口", "Token"],
    ["context_safety_margin_tokens", "上下文安全余量", "Token"],
    ["auto_compact_tokens", "自动压缩阈值", "Token"],
    ["compact_target_tokens", "压缩后目标", "Token"],
  ] },
  { title: "工具输出", fields: [
    ["max_tool_result_bytes", "单次工具结果上限", "字节"],
    ["max_read_chars_per_call", "read 单次最多读取字符数", "字符"],
    ["max_catalog_items_per_page", "每页候选目录上限", "条"],
  ] },
];

export function GlobalAgentSettings({ mode }: { mode: string }) {
  const { t, locale } = useI18n();
  const [settings, setSettings] = useState<AgentGlobalSettings | null>(null);
  const [reload, setReload] = useState(0);
  const [formVersion, setFormVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<SettingsError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const restartPending = Boolean(
    settings?.restartRequiredFields.includes("maximum_concurrent_kicks")
    && settings.values.maximum_concurrent_kicks !== settings.activeValues.maximum_concurrent_kicks,
  );
  const primaryGroup = settingGroups[0]!;
  const advancedGroups = settingGroups.slice(1);
  const advancedFieldCount = advancedGroups.reduce((count, group) => count + group.fields.length, 0);
  const disabled = saving || loading || mode !== "live";

  useEffect(() => {
    let active = true;
    if (mode !== "live") {
      setLoading(false);
      return () => { active = false; };
    }
    setLoading(true);
    void getAgentGlobalSettings().then((result) => {
      if (!active) return;
      setSettings(result);
      setFormVersion((current) => current + 1);
      setDirty(false);
      setError(null);
    }).catch((failure) => {
      if (active) setError({ message: "读取全局配置失败：{error}", failure });
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [reload, mode]);

  useEffect(() => {
    if (dirty || saving || mode !== "live") return;
    const refresh = () => setReload((current) => current + 1);
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [dirty, saving, mode]);

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!settings || saving || loading || mode !== "live") return;
    const form = new FormData(event.currentTarget);
    const changes: Record<string, number | boolean | AgentReasoningEffort> = {};
    for (const group of settingGroups) {
      for (const [key] of group.fields) {
        const raw = form.get(key);
        const value = Number(raw);
        if (typeof raw !== "string" || !raw.trim() || !Number.isFinite(value)) {
          setError({ message: "请为 {key} 输入有效数字。", params: { key } });
          return;
        }
        if (value !== settings.values[key]) changes[key] = value;
      }
    }
    const limitsEnabled = form.has("limits_enabled");
    if (limitsEnabled !== settings.values.limits_enabled) changes.limits_enabled = limitsEnabled;
    for (const [key] of reasoningFields) {
      const value = form.get(key);
      if (value !== "none" && value !== "low" && value !== "high" && value !== "max") {
        setError({ message: "请为 {key} 选择有效的思考模式。", params: { key } });
        return;
      }
      if (value !== settings.values[key]) changes[key] = value;
    }
    if (!Object.keys(changes).length) {
      setDirty(false);
      setNotice("配置没有变化。");
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await updateAgentGlobalSettings(changes as Partial<AgentGlobalSettingsValues>, settings.revision);
      setSettings(result);
      setFormVersion((current) => current + 1);
      setDirty(false);
      const pending = result.restartRequiredFields.includes("maximum_concurrent_kicks")
        && result.values.maximum_concurrent_kicks !== result.activeValues.maximum_concurrent_kicks;
      setNotice(pending
        ? "已保存。除并行任务上限外，新请求会使用更新后的配置；并行任务上限重启服务后生效。"
        : "已保存。新的请求会使用更新后的全局配置。");
    } catch (failure) {
      setError(failure instanceof ApiClientError && failure.status === 409
        ? { message: "配置文件已在其他地方修改。请重新读取后再保存，避免覆盖新设置。" }
        : { message: "保存全局配置失败：{error}", failure });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.agentSettings}>
      <h3 className={styles.agentTitle}>{t("全局 Agent 配置")}</h3>
      <p className={styles.agentNote}>{t("这里修改的是全局默认值。除并行任务上限需要重启外，新参数用于后续新请求；正在生成的回答沿用原参数。Chat 页明确调整的本次回答设置优先。直接编辑 config.yaml 后，重新打开页面或点击“重新读取”即可查看最新值。")}</p>
      {restartPending && settings ? <p className={styles.restartNotice} role="status">
        {t("并行任务上限已保存为 {saved}，当前服务仍使用 {active}；重启服务后生效。", { saved: settings.values.maximum_concurrent_kicks, active: settings.activeValues.maximum_concurrent_kicks })}
      </p> : null}
      <div className={styles.agentToolbar}>
        <strong>{t("运行参数")}</strong>
        <button className="button-quiet" type="button" disabled={loading || saving}
          onClick={() => { setNotice(null); setReload((current) => current + 1); }}>
          {t(dirty ? "重新读取（放弃未保存更改）" : "重新读取")}
        </button>
      </div>

      {mode === "live" && loading && !settings ? <p className={styles.agentState} role="status">{t("正在读取全局配置…")}</p> : null}
      {mode !== "live" && !settings ? <p className={styles.modeNote} role="status">
        {t(mode === "offline" ? "资料连接不可用，暂时无法读取全局配置。" : "正在检查资料连接…")}
      </p> : null}

      {settings ? <form key={formVersion} onSubmit={(event) => void save(event)}
        onInput={() => { setDirty(true); setNotice(null); }}
        onChange={() => { setDirty(true); setNotice(null); }}>
        <div className={styles.agentCard}>
          <label className={styles.agentToggle}>
            <span className={styles.rowText}>
              <strong>{t("启用调用次数限制")}</strong>
              <small>{t("开启后，限制模型和工具调用次数；Chat 页的本次回答设置优先。")}</small>
            </span>
            <input className={styles.switch} name="limits_enabled" type="checkbox" role="switch"
              aria-label={t("启用调用次数限制")} defaultChecked={settings.values.limits_enabled} disabled={disabled} />
          </label>

          <details className={styles.agentDisclosure} open>
            <summary className={styles.agentSummary}>
              <span>{t(primaryGroup.title)}</span>
              <span className={styles.agentSummaryMeta}>{t("{count} 项", { count: primaryGroup.fields.length })}
                <ChevronDown size={15} aria-hidden="true" /></span>
            </summary>
            <div className={styles.agentFields}>
              {primaryGroup.fields.map(([key, label, unit]) => (
                <label className={styles.agentField} key={key}>
                  <span className={styles.agentFieldText}>
                    <strong>{t(label)}</strong>
                    {key === "maximum_concurrent_kicks" ? <em className={styles.restartBadge}>{t("重启后生效")}</em> : null}
                    <small>{key}</small>
                  </span>
                  <span className={styles.agentInputWrap}>
                    <input className={`field ${styles.agentInput}`} name={key} type="number" required
                      min={1} step={key.endsWith("seconds") ? "any" : 1} defaultValue={settings.values[key]}
                      disabled={disabled} aria-label={t(label)} />
                    <small>{t(unit)}</small>
                  </span>
                </label>
              ))}
            </div>
          </details>

          <details className={styles.agentDisclosure}>
            <summary className={styles.agentSummary}>
              <span>{t("高级")}</span>
              <span className={styles.agentSummaryMeta}>{t("{count} 项", { count: advancedFieldCount })}
                <ChevronDown size={15} aria-hidden="true" /></span>
            </summary>
            <div className={styles.advancedBody}>
              {advancedGroups.map((group) => (
                <section className={styles.agentSubgroup} key={group.title}>
                  <h4 className={styles.agentSubgroupTitle}>{t(group.title)}</h4>
                  {group.title === "超时与并发" ? <p className={styles.agentGroupNote}>{t("单次模型请求另有启动时确定的超时上限。提高阶段超时不会自动提高该上限；修改模型请求配置后需重启服务。")}</p> : null}
                  {group.title === "模型上下文" ? <p className={styles.agentGroupNote}>{t("模型配置中的输出上限仍是最终上限；若要提高该上限，修改模型配置后需重启服务。")}</p> : null}
                  {group.title === "工具输出" ? <p className={styles.agentGroupNote}>{t("read 工具按片段内容的字符范围读取，不按行读取；工具结果还受上面的字节上限约束。上限调得过小会增加工具调用次数和回答耗时。")}</p> : null}
                  <div className={styles.agentFields}>
                    {group.fields.map(([key, label, unit]) => (
                      <label className={styles.agentField} key={key}>
                        <span className={styles.agentFieldText}>
                          <strong>{t(label)}</strong>
                          {key === "maximum_concurrent_kicks" ? <em className={styles.restartBadge}>{t("重启后生效")}</em> : null}
                          <small>{key}</small>
                        </span>
                        <span className={styles.agentInputWrap}>
                          <input className={`field ${styles.agentInput}`} name={key} type="number" required
                            min={key === "max_tool_result_bytes" ? 512 : 1}
                            max={key === "maximum_parallel_queries" ? 8 : key === "max_catalog_items_per_page" ? 100 : undefined}
                            step={key.endsWith("seconds") ? "any" : 1} defaultValue={settings.values[key]}
                            disabled={disabled} aria-label={t(label)} />
                          <small>{t(unit)}</small>
                        </span>
                      </label>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </details>

          <details className={styles.agentDisclosure}>
            <summary className={styles.agentSummary}>
              <span>{t("模型思考")}</span>
              <span className={styles.agentSummaryMeta}>{t("{count} 项", { count: reasoningFields.length })}
                <ChevronDown size={15} aria-hidden="true" /></span>
            </summary>
            <div className={styles.agentFields}>
              <p className={styles.agentGroupNote}>{t("分别设置 Query 和 Supervisor 的思考强度；关闭时不使用思考模式。保存后用于新的请求。")}</p>
              {reasoningFields.map(([key, label]) => (
                <label className={styles.agentField} key={key}>
                  <span className={styles.agentFieldText}><strong>{t(label)}</strong><small>{key}</small></span>
                  <span className={styles.agentInputWrap}>
                    <select className={`field ${styles.agentSelect}`} name={key} defaultValue={settings.values[key]}
                      disabled={disabled} aria-label={t(label)}>
                      {reasoningOptions.map(([value, text]) => <option key={value} value={value}>{t(text)}</option>)}
                    </select>
                  </span>
                </label>
              ))}
              <p className={styles.agentGroupNote}>{t("Supervisor 使用单独绑定的模型并配置了推理强度时，以模型绑定为准。")}</p>
            </div>
          </details>
        </div>

        <p className={styles.agentNote}>{t("相关参数之间的约束由服务端检查；保存失败时当前输入会保留，便于修改。")}</p>
        <div className={styles.agentActions}>
          <button className="button-primary" type="submit" disabled={disabled || !dirty}>{t(saving ? "正在保存…" : "保存全局配置")}</button>
        </div>
      </form> : null}

      {mode !== "live" && settings ? <p className={styles.modeNote}>{t("当前资料连接不可用，暂时无法保存全局配置。")}</p> : null}
      {error ? <p className={styles.settingsError} role="alert">{t(error.message, { ...error.params, error: errorMessage(error.failure, locale) })}</p> : null}
      {notice ? <p className={styles.settingsSuccess} role="status">{t(notice)}</p> : null}
    </div>
  );
}
