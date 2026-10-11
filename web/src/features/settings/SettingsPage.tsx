import { Check, Database, Moon, Server, ShieldCheck, Sun } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

import { ApiClientError, errorMessage, getAgentGlobalSettings, updateAgentGlobalSettings } from "../../api/client";
import type { AgentGlobalSettings, AgentGlobalSettingsValues, AgentReasoningEffort } from "../../api/contracts";
import { useTheme } from "../../app/theme";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
import styles from "./settings.module.css";

type ReasoningSettingKey = "query_reasoning_effort" | "evaluator_reasoning_effort" | "chat_reasoning_effort";
type NumericSettingKey = Exclude<keyof AgentGlobalSettingsValues, "limits_enabled" | ReasoningSettingKey>;
type SettingField = readonly [NumericSettingKey, string, string];

const reasoningFields: ReadonlyArray<readonly [ReasoningSettingKey, string]> = [
  ["query_reasoning_effort", "Query 检索"],
  ["evaluator_reasoning_effort", "Evaluator 审核"],
  ["chat_reasoning_effort", "Chat 回答"],
];
const reasoningOptions: ReadonlyArray<readonly [AgentReasoningEffort, string]> = [
  ["none", "关闭"], ["low", "低"], ["high", "高"], ["max", "最大"],
];

const settingGroups: ReadonlyArray<{ title: string; fields: ReadonlyArray<SettingField> }> = [
  { title: "回答与检索", fields: [
    ["history_message_limit", "历史消息数量", "条"],
    ["default_top_k", "默认候选片段", "段"],
    ["maximum_top_k", "候选片段上限", "段"],
    ["max_loop_turns", "启用调用限制时的最多轮次", "轮"],
    ["max_model_calls_per_turn", "每轮默认模型调用", "次"],
    ["maximum_model_calls_per_turn", "每轮模型调用上限", "次"],
    ["max_tool_calls_per_turn", "每轮默认工具调用", "次"],
    ["maximum_tool_calls_per_turn", "每轮工具调用上限", "次"],
  ] },
  { title: "超时与并发", fields: [
    ["stage_timeout_seconds", "常规阶段超时", "秒"],
    ["evaluator_stage_timeout_seconds", "审核阶段超时", "秒"],
    ["execution_timeout_seconds", "整次执行超时", "秒"],
    ["maximum_concurrent_kicks", "并行任务上限", "个"],
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
  const [settings, setSettings] = useState<AgentGlobalSettings | null>(null);
  const [reload, setReload] = useState(0);
  const [formVersion, setFormVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
      if (active) setError(`读取全局配置失败：${errorMessage(failure)}`);
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
          setError(`请为 ${key} 输入有效数字。`);
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
        setError(`请为 ${key} 选择有效的思考模式。`);
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
        ? "配置文件已在其他地方修改。请重新读取后再保存，避免覆盖新设置。"
        : `保存全局配置失败：${errorMessage(failure)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className={styles.settingsSection}>
      <div className={styles.sectionIntro}><span>03</span><div><h2>全局 Agent 配置</h2><p>保存到服务端 config.yaml，供所有工作区后续新请求使用。</p></div></div>
      <div className={styles.globalSettings}>
        <p className={styles.globalSettingsNote}>这里修改的是全局默认值。除并行任务上限需要重启外，新参数用于后续新请求；正在生成的回答沿用原参数。Chat 页明确调整的本次回答设置优先。直接编辑 config.yaml 后，重新打开页面或点击“重新读取”即可查看最新值。</p>
        {restartPending && settings ? (
          <p className={styles.restartNotice} role="status">
            并行任务上限已保存为 <strong>{settings.values.maximum_concurrent_kicks}</strong>，当前服务仍使用 <strong>{settings.activeValues.maximum_concurrent_kicks}</strong>；重启服务后生效。
          </p>
        ) : null}
        <div className={styles.globalSettingsToolbar}>
          <strong>运行参数</strong>
          <button className="button-quiet" type="button" disabled={loading || saving} onClick={() => { setNotice(null); setReload((current) => current + 1); }}>重新读取{dirty ? "（放弃未保存更改）" : ""}</button>
        </div>
        {loading && !settings ? <p role="status">正在读取全局配置…</p> : null}
        {settings ? (
          <form key={formVersion} onSubmit={(event) => void save(event)} onInput={() => { setDirty(true); setNotice(null); }} onChange={() => { setDirty(true); setNotice(null); }}>
            <label className={styles.globalSettingToggle}>
              <span><strong>启用调用次数限制</strong><small>开启后，Chat 的自动模式限制轮次、模型和工具调用次数；自定义轮次按本次设置执行。</small></span>
              <input name="limits_enabled" type="checkbox" defaultChecked={settings.values.limits_enabled} disabled={saving || loading || mode !== "live"} />
            </label>
            {settingGroups.map((group, index) => (
              <details className={styles.globalSettingGroup} key={group.title} open={index === 0}>
                <summary>{group.title}<span>{group.fields.length} 项</span></summary>
                {group.title === "超时与并发" ? <p className={styles.globalSettingsNote}>单次模型请求另有启动时确定的超时上限。提高阶段超时不会自动提高该上限；修改模型请求配置后需重启服务。</p> : null}
                {group.title === "模型上下文" ? <p className={styles.globalSettingsNote}>模型配置中的输出上限仍是最终上限；若要提高该上限，修改模型配置后需重启服务。</p> : null}
                {group.title === "工具输出" ? <p className={styles.globalSettingsNote}>read 工具按片段内容的字符范围读取，不按行读取；工具结果还受上面的字节上限约束。上限调得过小会增加工具调用次数和回答耗时。</p> : null}
                <div className={styles.globalSettingFields}>
                  {group.fields.map(([key, label, unit]) => (
                    <label className={styles.globalSettingField} key={key}>
                      <span><strong>{label}</strong>{key === "maximum_concurrent_kicks" ? <em className={styles.restartBadge}>重启后生效</em> : null}<small>{key}</small></span>
                      <span className={styles.globalSettingInput}><input className="field" name={key} type="number" required min={key === "max_tool_result_bytes" ? 512 : 1} max={key === "max_loop_turns" ? 3 : key === "maximum_top_k" || key === "max_catalog_items_per_page" ? 100 : undefined} step={key.endsWith("seconds") ? "any" : 1} defaultValue={settings.values[key]} disabled={saving || loading || mode !== "live"} /><small>{unit}</small></span>
                    </label>
                  ))}
                </div>
              </details>
            ))}
            <details className={styles.globalSettingGroup}>
              <summary>模型思考<span>{reasoningFields.length} 项</span></summary>
              <p className={styles.globalSettingsNote}>分别设置 Query、Evaluator 和 Chat 的思考强度；关闭时不使用思考模式。保存后用于新的请求。</p>
              <div className={styles.globalSettingFields}>
                {reasoningFields.map(([key, label]) => (
                  <label className={styles.globalSettingField} key={key}>
                    <span><strong>{label}</strong><small>{key}</small></span>
                    <span className={styles.globalSettingInput}>
                      <select className="field" name={key} defaultValue={settings.values[key]} disabled={saving || loading || mode !== "live"} style={{ width: 110 }}>
                        {reasoningOptions.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
                      </select>
                    </span>
                  </label>
                ))}
              </div>
            </details>
            <p className={styles.globalSettingsNote}>相关参数之间的约束由服务端检查；保存失败时当前输入会保留，便于修改。</p>
            <div className={styles.globalSettingsActions}><button className="button-primary" type="submit" disabled={saving || loading || !dirty || mode !== "live"}>{saving ? "正在保存…" : "保存全局配置"}</button></div>
          </form>
        ) : null}
        {error ? <p className={styles.settingsError} role="alert">{error}</p> : null}
        {notice ? <p className={styles.settingsSuccess} role="status">{notice}</p> : null}
      </div>
    </section>
  );
}

export function SettingsPage() {
  const { theme, setTheme } = useTheme();
  const { mode, displayMode } = useWorkspace();
  const isDebug = displayMode === "debug";
  const { activeSource, activeTenantId, accountIdentity, accountConnection, accountStatus, accountError, retryAccount } = useAuth();
  const shownApiUrl = new URL(activeSource.connection.baseUrl || "/", window.location.origin).href.replace(/\/$/, "");
  const accountLabel = accountStatus === "unconfigured" ? "未连接账号服务"
    : accountStatus === "checking" ? "正在连接"
    : accountStatus === "unavailable" ? "连接不可用"
    : accountIdentity && !accountIdentity.isGuest ? accountIdentity.email : "未登录";

  return (
    <main className="page">
      <header>
        <h1 className="page-title">设置</h1>
      </header>

      <div className={styles.layout}>
        <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>01</span><div><h2>外观</h2><p>白天清透，夜里安静；两套主题使用同一组青绿色语义。</p></div></div>
          <div className={styles.themeChoices} role="radiogroup" aria-label="主题模式">
            <button type="button" role="radio" aria-checked={theme === "light"} className={theme === "light" ? styles.themeActive : undefined} onClick={() => setTheme("light")}>
              <div className={styles.themePreviewLight}><i /><i /><i /></div>
              <span><Sun size={15} />白天模式</span>
              {theme === "light" ? <Check size={15} /> : null}
            </button>
            <button type="button" role="radio" aria-checked={theme === "dark"} className={theme === "dark" ? styles.themeActive : undefined} onClick={() => setTheme("dark")}>
              <div className={styles.themePreviewDark}><i /><i /><i /></div>
              <span><Moon size={15} />黑夜模式</span>
              {theme === "dark" ? <Check size={15} /> : null}
            </button>
          </div>
        </section>

        <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>02</span><div><h2>连接</h2><p>账号登录和资料来源分别管理，登录不会移动本机资料。</p></div></div>
          <dl className={styles.connectionList}>
            <div><dt><Database size={16} />资料来源</dt><dd>{activeSource.kind === "local" ? "本机资料" : activeSource.kind === "public" ? "公开资料" : "我的资料"}</dd></div>
            <div><dt><Server size={16} />资料连接</dt><dd data-mode={mode}><i />{mode === "live" ? "已连接" : mode === "offline" ? "不可用" : "正在检查"}</dd></div>
            {isDebug ? <div><dt><Database size={16} />资料地址</dt><dd>{shownApiUrl}</dd></div> : null}
            {isDebug ? <div><dt><ShieldCheck size={16} />资料空间</dt><dd>{activeTenantId}</dd></div> : null}
            <div><dt><ShieldCheck size={16} />账号</dt><dd>{accountLabel}</dd></div>
            {isDebug && accountConnection ? <div><dt><Server size={16} />账号地址</dt><dd>{new URL(accountConnection.baseUrl || "/", window.location.origin).href.replace(/\/$/, "")}</dd></div> : null}
          </dl>
          {accountStatus === "unavailable" ? <p className={styles.modeNote}>{accountError || "账号服务暂时不可用。"} <button className="button-secondary" type="button" onClick={retryAccount}>重新连接</button></p> : null}
          {mode === "offline" ? (
            <p className={styles.modeNote}>当前资料连接不可用。请检查服务和网络；恢复连接后会重新读取资料和任务状态。</p>
          ) : null}
        </section>

        {isDebug ? <GlobalAgentSettings mode={mode} /> : null}

        {isDebug ? <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>04</span><div><h2>数据边界</h2><p>前端只通过公开 HTTP 接口访问媒体，不尝试读取内部 artifact URI。</p></div></div>
          <div className={styles.boundaries}>
            <article><strong>上传确认</strong><p>分片经过 SHA-256 校验，并由服务端确认后才进入已上传进度。</p></article>
            <article><strong>检索理由</strong><p>只根据 Evidence、召回通道与排名生成可核验摘要，不展示或伪造私有思维链。</p></article>
            <article><strong>断线恢复</strong><p>SSE 只负责通知；恢复后重新获取 MySQL 快照，并以服务端状态为准。</p></article>
          </div>
        </section> : null}
      </div>
    </main>
  );
}
