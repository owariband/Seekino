import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { useI18n } from "../../app/i18n";
import { translate, type Locale } from "../../shared/messages";
import { errorMessage, getUsageOverview, shouldRetryApiRequest, TENANT_ID } from "../../api/client";
import type { UsageOverviewVO, UsageTotals } from "../../api/contracts";
import { useWorkspace } from "../../app/workspace";
import { formatMoneyCny, formatTokens } from "../../shared/format";
import styles from "./usage.module.css";

const displayTokens = (value: number | null, locale: Locale) => value === null ? "—" : formatTokens(value, locale);
const displayApiCost = (cost: UsageTotals["cost"], truncated: boolean, locale: Locale) => {
  const t = (message: string) => translate(locale, message);
  if (!cost) return t("未计价");
  if (cost.excludedCallCount > 0 && cost.pricedCallCount === 0 && cost.unpricedCallCount === 0) return t("不适用（Codex订阅）");
  if (cost.pricedCallCount === 0 && cost.unpricedCallCount > 0) return t("未计价");
  const subtotal = truncated || cost.unpricedCallCount > 0 || cost.estimatedAmount === null;
  const amount = subtotal ? cost.knownAmount : cost.estimatedAmount;
  return amount === null ? t("未计价") : `${formatMoneyCny(amount)}${subtotal ? t("（小计）") : ""}`;
};

const operationLabels: Record<string, string> = {
  candidate_transcript: "对白识别",
  video_timeline: "时间线解析",
  neutral_visual_evidence: "视觉核验",
  fallback_video_timeline: "备用时间线解析",
  text_embedding: "文本向量化",
  video_embedding: "视频向量化",
  understand: "理解",
  refine: "Refine",
  index: "索引发布",
  sdk_turn_run: "原生 Agent 执行",
};
const categoryLabels: Record<string, string> = { parse: "解析", embedding: "向量化", chat: "Chat" };
const taskTypeLabels = { video_parse: "解析任务", chat_retrieval: "Chat" };

export function UsagePage() {
  const { t, locale, formatLocale } = useI18n();
  const { displayMode } = useWorkspace();
  const isDebug = displayMode === "debug";
  const queryClient = useQueryClient();
  const [seriesId, setSeriesId] = useState("");
  const effectiveSeriesId = isDebug ? seriesId : "";
  const [view, setView] = useState<"models" | "runs">("models");
  const usageQuery = useQuery({
    queryKey: ["usage-overview", TENANT_ID, effectiveSeriesId],
    queryFn: ({ signal }) => getUsageOverview({ seriesId: effectiveSeriesId }, signal),
    retry: shouldRetryApiRequest,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
  const overview = usageQuery.data;
  const available = overview?.status === "available";
  const totals = available ? overview.totals : undefined;
  const allOverview = queryClient.getQueryData<UsageOverviewVO>(["usage-overview", TENANT_ID, ""]);
  const series = [...new Set((allOverview?.runs ?? overview?.runs ?? []).flatMap((run) => run.seriesId ? [run.seriesId] : []))];
  if (seriesId && !series.includes(seriesId)) series.push(seriesId);
  const knownTokenSubtotal = totals?.totalTokens === null || overview?.truncated;
  const knownCostSubtotal = Boolean(overview?.truncated || (totals?.cost && (
    totals.cost.unpricedCallCount > 0 || totals.cost.pricedCallCount > 0 && totals.cost.estimatedAmount === null
  )));

  return (
    <main className="page">
      <header className={styles.header}>
        <h1 className="page-title">{t("模型消耗")}</h1>
        <button className="button-secondary" type="button" onClick={() => void usageQuery.refetch()} disabled={usageQuery.isFetching}>
          <RefreshCw size={15} className={usageQuery.isFetching ? styles.spinning : undefined} />{t("刷新用量")}
        </button>
      </header>
      <p className={styles.scope}>{t("本地已记录用量 · 最近 30 天 · 解析、向量化与 Chat")}</p>
      {overview ? <p className={styles.scope}>{t("统计窗口：{start} 至 {end} · 读取于 {fetched}", { start: new Date(overview.fromStartTime).toLocaleDateString(formatLocale), end: new Date(overview.toStartTime).toLocaleDateString(formatLocale), fetched: new Date(overview.fetchedAt).toLocaleString(formatLocale) })}</p> : null}

      <div className={styles.toolbar}>
        {isDebug ? <label className={styles.seriesFilter}><span>{t("视频系列")}</span><select aria-label={t("视频系列")} value={seriesId} onChange={(event) => setSeriesId(event.target.value)}>
          <option value="">{t("全部视频系列")}</option>
          {series.map((id) => <option key={id} value={id}>{id}</option>)}
        </select></label> : null}
        <span>{available ? overview.truncated ? t("当前已读取 {value0} 条调用记录（结果截断）", { value0: displayTokens(overview.totals.callCount, locale) }) : t("{value0} 条已记录调用", { value0: displayTokens(overview.totals.callCount, locale) }) : overview ? t("统计暂不可用") : usageQuery.isError ? t("用量读取失败") : t("正在读取用量…")}</span>
      </div>

      <dl className={styles.totals} aria-label={t("模型消耗概况")}>
        <div><dt>{knownTokenSubtotal ? t("已知 Token 小计") : t("累计 Token")}</dt><dd>{totals ? displayTokens(totals.totalTokens ?? totals.knownTokens, locale) : "—"}</dd><small>{knownTokenSubtotal ? t("记录不完整，不能代表总消耗") : t("仅包含已记录调用")}</small><small>{t("输入 / 输出 Token：{tokens}", { tokens: totals ? `${displayTokens(totals.inputTokens, locale)} / ${displayTokens(totals.outputTokens, locale)}` : "—" })}</small></div>
        <div><dt>{knownCostSubtotal ? t("已计价API小计") : t("API预估费用")}</dt><dd>{totals ? displayApiCost(totals.cost, overview?.truncated ?? false, locale) : "—"}</dd><small>{t("按配置单价估算，人民币显示，不含 Codex 订阅费用")}</small></div>
        <div><dt>{t("已记录调用")}</dt><dd>{totals ? displayTokens(totals.callCount, locale) : "—"}</dd><small>{t("仅统计已记录调用；历史未记录用量无法还原")}</small></div>
      </dl>
      <p className={styles.tableNote}>{t("Codex 仅统计 Token；Token 不能直接换算为每周额度百分比。")}</p>
      {totals?.cost?.unpricedCallCount ? <p className={styles.warning}>{t("{count} 条 API 调用未计价，费用仅显示已计价部分。", { count: totals.cost.unpricedCallCount })}</p> : null}

      {usageQuery.error instanceof Error ? <div className={styles.error} role="alert"><CircleAlert size={16} /><div><strong>{t("用量更新失败")}</strong><p>{errorMessage(usageQuery.error, locale)}{overview ? t("。以下保留上次读取的状态与记录。") : ""}</p></div></div> : null}
      {overview && !available ? <div className={styles.notice} role="status"><CircleAlert size={16} /><div><p>{overview.message ?? t("模型消耗暂时无法读取，请稍后刷新重试。")}</p></div></div> : null}
      {totals && (totals.missingUsageCount || overview?.truncated) ? <div className={styles.notice} role="status"><CircleAlert size={16} /><div>
        {totals.missingUsageCount ? <p>{t("{count} 条调用记录缺少完整 Token 用量，显示已知小计。", { count: totals.missingUsageCount })}</p> : null}
        {overview?.truncated ? <p>{t("已达到本次读取上限，当前结果被截断，不能代表统计窗口内的全部消耗。")}</p> : null}
      </div></div> : null}

      {isDebug ? <div className={styles.viewBar}>
        <div role="group" aria-label={t("用量统计方式")}>
          <button type="button" aria-pressed={view === "models"} className={view === "models" ? styles.viewActive : undefined} onClick={() => setView("models")}>{t("按模型")}</button>
          <button type="button" aria-pressed={view === "runs"} className={view === "runs" ? styles.viewActive : undefined} onClick={() => setView("runs")}>{t("任务与 Chat")}</button>
        </div>
        <span>{t("仅统计已记录调用")}</span>
      </div> : null}

      {usageQuery.isPending ? <div className={styles.empty} role="status"><RefreshCw size={22} className={styles.spinning} /><p>{t("正在读取模型消耗…")}</p></div> : null}
      {available && overview.totals.callCount === 0 && !overview.truncated ? <div className={styles.empty}><h2>{t("当前窗口暂无已记录调用")}</h2><p>{t("已记录的模型调用会显示在这里；未记录的历史用量不会补记为零。")}</p><Link className="button-secondary" to="/library">{t("前往资料库")}</Link></div> : null}

      {isDebug && available && overview.totals.callCount !== 0 && view === "models" ? <section className={styles.tablePanel} aria-label={t("按模型统计用量")}>
        <div className={styles.tableScroll} role="region" aria-label={t("模型用量明细")} tabIndex={0}>
          <table className={styles.table}>
            <thead><tr><th>{t("模型 / 服务商")}</th><th>{t("来源（src）")}</th><th>{t("用途")}</th><th>{t("调用记录")}</th><th>{t("输入 Token")}</th><th>{t("输出 Token")}</th><th>{t("缓存输入")}</th><th>{t("缓存写入")}</th><th>{t("推理 Token")}</th><th>{t("总 Token")}</th><th>{t("API预估费用")}</th></tr></thead>
            <tbody>{overview.models.map((item) => <tr key={JSON.stringify([item.provider, item.src, item.model, item.category, item.operation])}>
              <th scope="row">{item.model ?? t("未知模型")}<small>{item.provider ?? t("未知服务商")}</small></th>
              <td>{item.src?.trim() || t("未记录")}</td>
              <td>{item.operation ? t(operationLabels[item.operation] ?? item.operation) : t("未知用途")}<small>{item.category ? t(categoryLabels[item.category] ?? item.category) : t("未分类")}</small></td>
              <td>{displayTokens(item.callCount, locale)}</td><td>{displayTokens(item.inputTokens, locale)}</td><td>{displayTokens(item.outputTokens, locale)}</td><td>{displayTokens(item.cachedInputTokens, locale)}</td><td>{displayTokens(item.cacheWriteInputTokens, locale)}</td><td>{displayTokens(item.reasoningTokens, locale)}</td><td>{displayTokens(item.totalTokens, locale)}{item.totalTokens === null ? <small className={styles.warning}>{t("已知小计 {tokens}", { tokens: displayTokens(item.knownTokens, locale) })}</small> : null}</td>
              <td>{displayApiCost(item.cost, overview.truncated, locale)}{item.cost?.unpricedCallCount ? <small>{t("{count} 条未计价", { count: item.cost.unpricedCallCount })}</small> : null}</td>
            </tr>)}</tbody>
          </table>
          {overview.models.length === 0 ? <p className={styles.noRows}>{t("暂无已记录的模型明细。")}</p> : null}
        </div>
        <p className={styles.tableNote}>{t("缓存读写与推理 Token 为用量子集，不重复计入总量；— 表示未知。")}</p>
      </section> : null}

      {isDebug && available && overview.totals.callCount !== 0 && view === "runs" ? <section className={styles.tablePanel} aria-label={t("任务与 Chat 用量")}>
        <div className={styles.tableScroll} role="region" aria-label={t("任务与 Chat 明细")} tabIndex={0}>
          <table className={styles.table}>
            <thead><tr><th>{t("任务 / Chat")}</th><th>{t("类型")}</th><th>{t("调用记录")}</th><th>{t("总 Token")}</th><th>{t("API预估费用")}</th><th>{t("记录状态")}</th></tr></thead>
            <tbody>{overview.runs.map((run) => <tr key={JSON.stringify([run.taskType, run.runId])}>
              <th scope="row">{run.runId}<small>{run.seriesId ?? t("无视频系列")}{run.videoId ? ` · ${run.videoId}` : ""}</small></th>
              <td>{t(taskTypeLabels[run.taskType])}</td><td>{displayTokens(run.callCount, locale)}</td><td>{displayTokens(run.totalTokens, locale)}{run.totalTokens === null ? <small>{t("已知小计 {tokens}", { tokens: displayTokens(run.knownTokens, locale) })}</small> : null}</td>
              <td>{displayApiCost(run.cost, overview.truncated, locale)}{run.cost?.unpricedCallCount ? <small>{t("{count} 条未计价", { count: run.cost.unpricedCallCount })}</small> : null}</td>
              <td className={run.missingUsageCount || overview.truncated ? styles.warning : undefined}>{overview.truncated ? t("已读取部分") : run.missingUsageCount ? t("记录不完整") : t("已记录")}{run.missingUsageCount ? <small>{t("{count} 条缺少完整用量", { count: run.missingUsageCount })}</small> : null}</td>
            </tr>)}</tbody>
          </table>
          {overview.runs.length === 0 ? <p className={styles.noRows}>{t("暂无已关联的任务与 Chat 明细。")}</p> : null}
        </div>
      </section> : null}
    </main>
  );
}
