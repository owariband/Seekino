import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { getUsageOverview, shouldRetryApiRequest, TENANT_ID } from "../../api/client";
import type { UsageOverviewVO } from "../../api/contracts";
import { useWorkspace } from "../../app/workspace";
import { formatTokens } from "../../shared/format";
import styles from "./usage.module.css";

const displayTokens = (value: number | null) => value === null ? "—" : formatTokens(value);

const operationLabels: Record<string, string> = {
  candidate_transcript: "对白识别",
  video_timeline: "时间线解析",
  neutral_visual_evidence: "视觉核验",
  fallback_video_timeline: "备用时间线解析",
  text_embedding: "文本向量化",
  video_embedding: "视频向量化",
};
const categoryLabels: Record<string, string> = { parse: "解析", embedding: "向量化", chat: "Chat" };
const taskTypeLabels = { video_parse: "解析任务", chat_retrieval: "Chat" };

export function UsagePage() {
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

  return (
    <main className="page">
      <header className={styles.header}>
        <h1 className="page-title">模型消耗</h1>
        <button className="button-secondary" type="button" onClick={() => void usageQuery.refetch()} disabled={usageQuery.isFetching}>
          <RefreshCw size={15} className={usageQuery.isFetching ? styles.spinning : undefined} />刷新用量
        </button>
      </header>
      <p className={styles.scope}>本地已记录用量 · 最近 30 天 · 解析、向量化与 Chat</p>
      {overview ? <p className={styles.scope}>统计窗口：{new Date(overview.fromStartTime).toLocaleDateString("zh-CN")} 至 {new Date(overview.toStartTime).toLocaleDateString("zh-CN")} · 读取于 {new Date(overview.fetchedAt).toLocaleString("zh-CN")}</p> : null}

      <div className={styles.toolbar}>
        {isDebug ? <label className={styles.seriesFilter}><span>视频系列</span><select aria-label="视频系列" value={seriesId} onChange={(event) => setSeriesId(event.target.value)}>
          <option value="">全部视频系列</option>
          {series.map((id) => <option key={id} value={id}>{id}</option>)}
        </select></label> : null}
        <span>{available ? overview.truncated ? `当前已读取 ${displayTokens(overview.totals.callCount)} 条调用记录（结果截断）` : `${displayTokens(overview.totals.callCount)} 条已记录调用` : overview ? "统计暂不可用" : usageQuery.isError ? "用量读取失败" : "正在读取用量…"}</span>
      </div>

      <dl className={styles.totals} aria-label="模型消耗概况">
        <div><dt>{knownTokenSubtotal ? "已知 Token 小计" : "累计 Token"}</dt><dd>{totals ? displayTokens(totals.totalTokens ?? totals.knownTokens) : "—"}</dd><small>{knownTokenSubtotal ? "记录不完整，不能代表总消耗" : "仅包含已记录调用"}</small></div>
        <div><dt>输入 / 输出 Token</dt><dd>{totals ? `${displayTokens(totals.inputTokens)} / ${displayTokens(totals.outputTokens)}` : "—"}</dd><small>{isDebug ? "输入包含缓存，输出包含推理" : "已记录的输入与输出"}</small></div>
        <div><dt>已记录调用</dt><dd>{totals ? displayTokens(totals.callCount) : "—"}</dd><small>仅统计已记录调用；历史未记录用量无法还原</small></div>
      </dl>

      {usageQuery.error instanceof Error ? <div className={styles.error} role="alert"><CircleAlert size={16} /><div><strong>用量更新失败</strong><p>{usageQuery.error.message}{overview ? "。以下保留上次读取的状态与记录。" : ""}</p></div></div> : null}
      {overview && !available ? <div className={styles.notice} role="status"><CircleAlert size={16} /><div><p>{overview.message ?? "模型消耗暂时无法读取，请稍后刷新重试。"}</p></div></div> : null}
      {totals && (totals.missingUsageCount || overview?.truncated) ? <div className={styles.notice} role="status"><CircleAlert size={16} /><div>
        {totals.missingUsageCount ? <p>{totals.missingUsageCount} 条调用记录缺少完整 Token 用量，显示已知小计。</p> : null}
        {overview?.truncated ? <p>已达到本次读取上限，当前结果被截断，不能代表统计窗口内的全部消耗。</p> : null}
      </div></div> : null}

      {isDebug ? <div className={styles.viewBar}>
        <div role="group" aria-label="用量统计方式">
          <button type="button" aria-pressed={view === "models"} className={view === "models" ? styles.viewActive : undefined} onClick={() => setView("models")}>按模型</button>
          <button type="button" aria-pressed={view === "runs"} className={view === "runs" ? styles.viewActive : undefined} onClick={() => setView("runs")}>任务与 Chat</button>
        </div>
        <span>仅统计已记录调用</span>
      </div> : null}

      {usageQuery.isPending ? <div className={styles.empty} role="status"><RefreshCw size={22} className={styles.spinning} /><p>正在读取模型消耗…</p></div> : null}
      {available && overview.totals.callCount === 0 && !overview.truncated ? <div className={styles.empty}><h2>当前窗口暂无已记录调用</h2><p>已记录的模型调用会显示在这里；未记录的历史用量不会补记为零。</p><Link className="button-secondary" to="/library">前往资料库</Link></div> : null}

      {isDebug && available && overview.totals.callCount !== 0 && view === "models" ? <section className={styles.tablePanel} aria-label="按模型统计用量">
        <div className={styles.tableScroll} role="region" aria-label="模型用量明细" tabIndex={0}>
          <table className={styles.table}>
            <thead><tr><th>模型 / 服务商</th><th>用途</th><th>调用记录</th><th>输入 Token</th><th>输出 Token</th><th>缓存输入</th><th>缓存写入</th><th>推理 Token</th><th>总 Token</th></tr></thead>
            <tbody>{overview.models.map((item) => <tr key={JSON.stringify([item.provider, item.model, item.category, item.operation])}>
              <th scope="row">{item.model ?? "未知模型"}<small>{item.provider ?? "未知服务商"}</small></th>
              <td>{item.operation ? operationLabels[item.operation] ?? item.operation : "未知用途"}<small>{item.category ? categoryLabels[item.category] ?? item.category : "未分类"}</small></td>
              <td>{displayTokens(item.callCount)}</td><td>{displayTokens(item.inputTokens)}</td><td>{displayTokens(item.outputTokens)}</td><td>{displayTokens(item.cachedInputTokens)}</td><td>{displayTokens(item.cacheWriteInputTokens)}</td><td>{displayTokens(item.reasoningTokens)}</td><td>{displayTokens(item.totalTokens)}{item.totalTokens === null ? <small className={styles.warning}>已知小计 {displayTokens(item.knownTokens)}</small> : null}</td>
            </tr>)}</tbody>
          </table>
          {overview.models.length === 0 ? <p className={styles.noRows}>暂无已记录的模型明细。</p> : null}
        </div>
        <p className={styles.tableNote}>缓存读写与推理 Token 为用量子集，不重复计入总量；— 表示未知。</p>
      </section> : null}

      {isDebug && available && overview.totals.callCount !== 0 && view === "runs" ? <section className={styles.tablePanel} aria-label="任务与 Chat 用量">
        <div className={styles.tableScroll} role="region" aria-label="任务与 Chat 明细" tabIndex={0}>
          <table className={styles.table}>
            <thead><tr><th>任务 / Chat</th><th>类型</th><th>调用记录</th><th>总 Token</th><th>记录状态</th></tr></thead>
            <tbody>{overview.runs.map((run) => <tr key={JSON.stringify([run.taskType, run.runId])}>
              <th scope="row">{run.runId}<small>{run.seriesId ?? "无视频系列"}{run.videoId ? ` · ${run.videoId}` : ""}</small></th>
              <td>{taskTypeLabels[run.taskType]}</td><td>{displayTokens(run.callCount)}</td><td>{displayTokens(run.totalTokens)}{run.totalTokens === null ? <small>已知小计 {displayTokens(run.knownTokens)}</small> : null}</td>
              <td className={run.missingUsageCount || overview.truncated ? styles.warning : undefined}>{overview.truncated ? "已读取部分" : run.missingUsageCount ? "记录不完整" : "已记录"}{run.missingUsageCount ? <small>{run.missingUsageCount} 条缺少完整用量</small> : null}</td>
            </tr>)}</tbody>
          </table>
          {overview.runs.length === 0 ? <p className={styles.noRows}>暂无已关联的任务与 Chat 明细。</p> : null}
        </div>
      </section> : null}
    </main>
  );
}
