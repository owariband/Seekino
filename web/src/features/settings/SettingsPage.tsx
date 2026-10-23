import { Check, Database, Moon, Server, ShieldCheck, Sun } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

import { ApiClientError, errorMessage, getAgentGlobalSettings, updateAgentGlobalSettings } from "../../api/client";
import type { AgentGlobalSettings, AgentGlobalSettingsValues, AgentReasoningEffort } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import type { MessageParams } from "../../shared/messages";
import { useTheme } from "../../app/theme";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
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
  ["none", "关闭推理"], ["low", "低"], ["medium", "中"], ["high", "高"], ["max", "最大"],
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

function GlobalAgentSettings({ mode }: { mode: string }) {
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
      if (value !== "none" && value !== "low" && value !== "medium" && value !== "high" && value !== "max") {
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
    <section className={styles.settingsSection}>
      <div className={styles.sectionIntro}><span>04</span><div><h2>{t("全局 Agent 配置")}</h2><p>{t("保存到服务端 config.yaml，供所有工作区后续新请求使用。")}</p></div></div>
      <div className={styles.globalSettings}>
        <p className={styles.globalSettingsNote}>{t("这里修改的是全局默认值。除并行任务上限需要重启外，新参数用于后续新请求；正在生成的回答沿用原参数。Chat 页明确调整的本次回答设置优先。直接编辑 config.yaml 后，重新打开页面或点击“重新读取”即可查看最新值。")}</p>
        {restartPending && settings ? (
          <p className={styles.restartNotice} role="status">
            {t("并行任务上限已保存为 {saved}，当前服务仍使用 {active}；重启服务后生效。", { saved: settings.values.maximum_concurrent_kicks, active: settings.activeValues.maximum_concurrent_kicks })}
          </p>
        ) : null}
        <div className={styles.globalSettingsToolbar}>
          <strong>{t("运行参数")}</strong>
          <button className="button-quiet" type="button" disabled={loading || saving} onClick={() => { setNotice(null); setReload((current) => current + 1); }}>{t(dirty ? "重新读取（放弃未保存更改）" : "重新读取")}</button>
        </div>
        {loading && !settings ? <p role="status">{t("正在读取全局配置…")}</p> : null}
        {settings ? (
          <form key={formVersion} onSubmit={(event) => void save(event)} onInput={() => { setDirty(true); setNotice(null); }} onChange={() => { setDirty(true); setNotice(null); }}>
            <label className={styles.globalSettingToggle}>
              <span><strong>{t("启用调用次数限制")}</strong><small>{t("开启后，限制模型和工具调用次数；Chat 页的本次回答设置优先。")}</small></span>
              <input name="limits_enabled" type="checkbox" defaultChecked={settings.values.limits_enabled} disabled={saving || loading || mode !== "live"} />
            </label>
            {settingGroups.map((group, index) => (
              <details className={styles.globalSettingGroup} key={group.title} open={index === 0}>
                <summary>{t(group.title)}<span>{t("{count} 项", { count: group.fields.length })}</span></summary>
                {group.title === "超时与并发" ? <p className={styles.globalSettingsNote}>{t("单次模型请求另有启动时确定的超时上限。提高阶段超时不会自动提高该上限；修改模型请求配置后需重启服务。")}</p> : null}
                {group.title === "模型上下文" ? <p className={styles.globalSettingsNote}>{t("模型配置中的输出上限仍是最终上限；若要提高该上限，修改模型配置后需重启服务。")}</p> : null}
                {group.title === "工具输出" ? <p className={styles.globalSettingsNote}>{t("read 工具按片段内容的字符范围读取，不按行读取；工具结果还受上面的字节上限约束。上限调得过小会增加工具调用次数和回答耗时。")}</p> : null}
                <div className={styles.globalSettingFields}>
                  {group.fields.map(([key, label, unit]) => (
                    <label className={styles.globalSettingField} key={key}>
                      <span><strong>{t(label)}</strong>{key === "maximum_concurrent_kicks" ? <em className={styles.restartBadge}>{t("重启后生效")}</em> : null}<small>{key}</small></span>
                      <span className={styles.globalSettingInput}><input className="field" name={key} type="number" required min={key === "max_tool_result_bytes" ? 512 : 1} max={key === "maximum_parallel_queries" ? 8 : key === "max_catalog_items_per_page" ? 100 : undefined} step={key.endsWith("seconds") ? "any" : 1} defaultValue={settings.values[key]} disabled={saving || loading || mode !== "live"} /><small>{t(unit)}</small></span>
                    </label>
                  ))}
                </div>
              </details>
            ))}
            <details className={styles.globalSettingGroup}>
              <summary>{t("模型思考")}<span>{t("{count} 项", { count: reasoningFields.length })}</span></summary>
              <p className={styles.globalSettingsNote}>{t("分别设置 Query 和 Supervisor 的思考强度；关闭时不使用思考模式。保存后用于新的请求。")}</p>
              <div className={styles.globalSettingFields}>
                {reasoningFields.map(([key, label]) => (
                  <label className={styles.globalSettingField} key={key}>
                    <span><strong>{t(label)}</strong><small>{key}</small></span>
                    <span className={styles.globalSettingInput}>
                      <select className="field" name={key} defaultValue={settings.values[key]} disabled={saving || loading || mode !== "live"} style={{ width: 110 }}>
                        {reasoningOptions.map(([value, text]) => <option key={value} value={value}>{t(text)}</option>)}
                      </select>
                    </span>
                  </label>
                ))}
              </div>
            </details>
            <p className={styles.globalSettingsNote}>{t("相关参数之间的约束由服务端检查；保存失败时当前输入会保留，便于修改。")}</p>
            <div className={styles.globalSettingsActions}><button className="button-primary" type="submit" disabled={saving || loading || !dirty || mode !== "live"}>{t(saving ? "正在保存…" : "保存全局配置")}</button></div>
          </form>
        ) : null}
        {error ? <p className={styles.settingsError} role="alert">{t(error.message, { ...error.params, error: errorMessage(error.failure, locale) })}</p> : null}
        {notice ? <p className={styles.settingsSuccess} role="status">{t(notice)}</p> : null}
      </div>
    </section>
  );
}

export function SettingsPage() {
  const { t, locale, setLocale } = useI18n();
  const { theme, setTheme } = useTheme();
  const { mode, displayMode } = useWorkspace();
  const isDebug = displayMode === "debug";
  const { activeSource, activeTenantId, accountIdentity, accountConnection, accountStatus, accountError, retryAccount, canConfigure } = useAuth();
  const showDebugSettings = canConfigure && isDebug;
  const shownApiUrl = new URL(activeSource.connection.baseUrl || "/", window.location.origin).href.replace(/\/$/, "");
  const accountLabel = accountStatus === "unconfigured" ? t("未连接账号服务")
    : accountStatus === "checking" ? t("正在连接")
    : accountStatus === "unavailable" ? t("连接不可用")
    : accountIdentity && !accountIdentity.isGuest ? accountIdentity.email : t("未登录");

  return (
    <main className="page">
      <header>
        <h1 className="page-title">{t("设置")}</h1>
      </header>

      <div className={styles.layout}>
        <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>01</span><div><h2>{t("外观")}</h2><p>{t("白天清透，夜里安静；两套主题使用同一组青绿色语义。")}</p></div></div>
          <div className={styles.themeChoices} role="radiogroup" aria-label={t("主题模式")}>
            <button type="button" role="radio" aria-checked={theme === "light"} className={theme === "light" ? styles.themeActive : undefined} onClick={() => setTheme("light")}>
              <div className={styles.themePreviewLight}><i /><i /><i /></div>
              <span><Sun size={15} />{t("白天模式")}</span>
              {theme === "light" ? <Check size={15} /> : null}
            </button>
            <button type="button" role="radio" aria-checked={theme === "dark"} className={theme === "dark" ? styles.themeActive : undefined} onClick={() => setTheme("dark")}>
              <div className={styles.themePreviewDark}><i /><i /><i /></div>
              <span><Moon size={15} />{t("黑夜模式")}</span>
              {theme === "dark" ? <Check size={15} /> : null}
            </button>
          </div>
        </section>

        <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>02</span><div><h2>{t("语言")}</h2><p>{t("选择界面语言，立即生效并保存在此浏览器。")}</p></div></div>
          <div className={styles.languageChoices} role="radiogroup" aria-label={t("界面语言")}>
            <button type="button" role="radio" aria-checked={locale === "zh"} className={locale === "zh" ? styles.languageActive : undefined} onClick={() => setLocale("zh")}>
              <strong>ZH</strong>{" "}<span lang="zh-CN">中文</span>{locale === "zh" ? <Check size={16} aria-hidden="true" /> : null}
            </button>
            <button type="button" role="radio" aria-checked={locale === "en"} className={locale === "en" ? styles.languageActive : undefined} onClick={() => setLocale("en")}>
              <strong>EN</strong>{" "}<span lang="en">English</span>{locale === "en" ? <Check size={16} aria-hidden="true" /> : null}
            </button>
          </div>
        </section>

        <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>03</span><div><h2>{t("连接")}</h2><p>{t("账号登录和资料来源分别管理，登录不会移动本机资料。")}</p></div></div>
          <dl className={styles.connectionList}>
            <div><dt><Database size={16} />{t("资料来源")}</dt><dd>{t(activeSource.kind === "local" ? "本机资料" : activeSource.kind === "public" ? "公开资料" : "我的资料")}</dd></div>
            <div><dt><Server size={16} />{t("资料连接")}</dt><dd data-mode={mode}><i />{t(mode === "live" ? "已连接" : mode === "offline" ? "不可用" : "正在检查")}</dd></div>
            {showDebugSettings ? <div><dt><Database size={16} />{t("资料地址")}</dt><dd>{shownApiUrl}</dd></div> : null}
            {showDebugSettings ? <div><dt><ShieldCheck size={16} />{t("资料空间")}</dt><dd>{activeTenantId}</dd></div> : null}
            <div><dt><ShieldCheck size={16} />{t("账号")}</dt><dd>{accountLabel}</dd></div>
            {showDebugSettings && accountConnection ? <div><dt><Server size={16} />{t("账号地址")}</dt><dd>{new URL(accountConnection.baseUrl || "/", window.location.origin).href.replace(/\/$/, "")}</dd></div> : null}
          </dl>
          {accountStatus === "unavailable" ? <p className={styles.modeNote}>{accountError || t("账号服务暂时不可用。")} <button className="button-secondary" type="button" onClick={retryAccount}>{t("重新连接")}</button></p> : null}
          {mode === "offline" ? (
            <p className={styles.modeNote}>{t("当前资料连接不可用。请检查服务和网络；恢复连接后会重新读取资料和任务状态。")}</p>
          ) : null}
        </section>

        {showDebugSettings ? <GlobalAgentSettings mode={mode} /> : null}

        {showDebugSettings ? <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>05</span><div><h2>{t("数据边界")}</h2><p>{t("前端只通过公开 HTTP 接口访问媒体，不尝试读取内部 artifact URI。")}</p></div></div>
          <div className={styles.boundaries}>
            <article><strong>{t("上传确认")}</strong><p>{t("分片经过 SHA-256 校验，并由服务端确认后才进入已上传进度。")}</p></article>
            <article><strong>{t("检索理由")}</strong><p>{t("只根据 Evidence、召回通道与排名生成可核验摘要，不展示或伪造私有思维链。")}</p></article>
            <article><strong>{t("断线恢复")}</strong><p>{t("SSE 只负责通知；恢复后重新获取 MySQL 快照，并以服务端状态为准。")}</p></article>
          </div>
        </section> : null}
      </div>
    </main>
  );
}
