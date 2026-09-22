import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { getParseUsage, listAllJobs, shouldRetryApiRequest, WORKSPACE_ID } from "../../api/client";
import type { ParseUsageVO } from "../../api/contracts";
import { formatCostCny, formatTokens } from "../../shared/format";
import { summarizeUsage, type UsageEntry } from "./usage";
import styles from "./usage.module.css";

const operationLabels: Record<string, string> = {
  candidate_transcript: "对白识别",
  video_timeline: "时间线解析",
  neutral_visual_evidence: "视觉核验",
  fallback_video_timeline: "备用时间线解析",
  text_embedding: "文本向量化",
  video_embedding: "视频向量化",
};

export function UsagePage() {
  const queryClient = useQueryClient();
  const [seriesId, setSeriesId] = useState("");
  const [view, setView] = useState<"models" | "tasks">("models");
  const usageQuery = useQuery({
    queryKey: ["usage-overview", WORKSPACE_ID],
    queryFn: async ({ signal }) => {
      const jobs = await listAllJobs(signal);
      const entries: UsageEntry[] = [];
      for (let offset = 0; offset < jobs.length; offset += 6) {
        signal.throwIfAborted();
        const batch = await Promise.all(jobs.slice(offset, offset + 6).map(async (job): Promise<UsageEntry> => {
          const queryKey = ["parse-usage", WORKSPACE_ID, job.jobId];
          const cached = queryClient.getQueryData<ParseUsageVO>(queryKey);
          const finalized = ["SUCCEEDED", "FAILED"].includes(job.status) && cached?.coverage === "recorded";
          try {
            const usage = await queryClient.fetchQuery({
              queryKey,
              queryFn: () => getParseUsage(job.jobId, signal),
              staleTime: finalized ? Infinity : 0,
              retry: shouldRetryApiRequest,
            });
            return { job, usage, error: null };
          } catch (error) {
            signal.throwIfAborted();
            return { job, usage: null, error: error instanceof Error ? error.message : "无法读取模型消耗" };
          }
        }));
        entries.push(...batch);
      }
      return entries;
    },
    retry: shouldRetryApiRequest,
    refetchInterval: (query) => query.state.data?.some(({ job }) => ["QUEUED", "RUNNING", "RETRY_WAIT"].includes(job.status)) ? 5_000 : 15_000,
  });

  const entries = usageQuery.data ?? [];
  const series = [...new Map(entries.map(({ job }) => [job.seriesId, job.seriesName])).entries()];
  const visible = entries.filter(({ job }) => !seriesId || job.seriesId === seriesId);
  const totals = summarizeUsage(visible);
  const failed = visible.filter(({ error }) => error);
  const incomplete = visible.filter(({ usage }) => usage && (usage.coverage !== "recorded" || usage.missingUsageCount || usage.unpricedCallCount));
  const unknown = visible.filter(({ usage }) => usage?.coverage === "unknown");
  const ready = usageQuery.isSuccess || usageQuery.data !== undefined;
  const knownTokenSubtotal = ready && totals.totalTokens === null && totals.knownTokens !== null;
  const knownCostSubtotal = ready && totals.estimatedCostCny === null && totals.knownCostCny !== null;
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["parse-usage", WORKSPACE_ID], refetchType: "none" });
    await usageQuery.refetch();
  };

  return (
    <main className="page">
      <header className={styles.header}>
        <h1 className="page-title">模型消耗</h1>
        <button className="button-secondary" type="button" onClick={() => void refresh()} disabled={usageQuery.isFetching}>
          <RefreshCw size={15} className={usageQuery.isFetching ? styles.spinning : undefined} />刷新用量
        </button>
      </header>
      <p className={styles.scope}>解析与向量化 · 包含重试 · 不含 Agent 聊天 · 费用为人民币预估</p>

      <div className={styles.toolbar}>
        <label className={styles.seriesFilter}><span>视频系列</span><select aria-label="视频系列" value={seriesId} onChange={(event) => setSeriesId(event.target.value)}>
          <option value="">全部视频系列</option>
          {series.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select></label>
        <span>{ready ? `${visible.length} 个解析任务` : usageQuery.isError ? "用量读取失败" : "正在读取用量…"}</span>
      </div>

      <dl className={styles.totals} aria-label="模型消耗概况">
        <div><dt>{knownTokenSubtotal ? "已记录 Token 小计" : "累计 Token"}</dt><dd>{ready ? formatTokens(totals.totalTokens ?? totals.knownTokens) : "—"}</dd><small>{knownTokenSubtotal ? "记录不完整，不能代表总消耗" : ready && totals.totalTokens === null ? "尚无完整用量记录" : "输入 + 输出"}</small></div>
        <div><dt>{knownCostSubtotal ? "已估算费用小计" : "累计预估费用"}</dt><dd>{ready ? formatCostCny(totals.estimatedCostCny ?? totals.knownCostCny) : "—"}</dd><small>{knownCostSubtotal ? "计价不完整，不能代表总费用" : ready && totals.estimatedCostCny === null ? "尚无完整计价记录" : "按后端配置单价估算"}</small></div>
        <div><dt>已记录模型调用</dt><dd>{ready && (visible.length === 0 || visible.some(({ usage }) => usage)) ? formatTokens(totals.callCount) : "—"}</dd><small>{ready ? `${visible.filter(({ usage }) => usage).length} / ${visible.length} 个任务读取成功` : usageQuery.isError ? "请刷新重试" : "正在连接 API"}</small></div>
      </dl>

      {usageQuery.error instanceof Error ? <div className={styles.error} role="alert"><CircleAlert size={16} /><div><strong>用量更新失败</strong><p>{usageQuery.error.message}{usageQuery.data ? "。以下保留上次读取的记录。" : ""}</p></div></div> : null}
      {failed.length || incomplete.length ? <div className={styles.notice} role="status"><CircleAlert size={16} /><div>
        {failed.length ? <p>{failed.length} 个任务的用量读取失败，可点击“刷新用量”重试。</p> : null}
        {incomplete.length ? <p>{incomplete.length} 个任务的用量或计价记录不完整，累计值不能作为完整总消耗。</p> : null}
        {unknown.length ? <p>{unknown.length} 个历史任务没有用量记录，无法还原其 Token 和费用。</p> : null}
      </div></div> : null}

      <div className={styles.viewBar}>
        <div role="group" aria-label="用量统计方式">
          <button type="button" aria-pressed={view === "models"} className={view === "models" ? styles.viewActive : undefined} onClick={() => setView("models")}>按模型</button>
          <button type="button" aria-pressed={view === "tasks"} className={view === "tasks" ? styles.viewActive : undefined} onClick={() => setView("tasks")}>按任务</button>
        </div>
        <span>{view === "models" ? "仅统计已记录的调用" : "每个任务包含其全部执行尝试"}</span>
      </div>

      {usageQuery.isPending ? <div className={styles.empty} role="status"><RefreshCw size={22} className={styles.spinning} /><p>正在从 API 读取模型消耗…</p></div> : null}
      {ready && visible.length === 0 ? <div className={styles.empty}><h2>暂无解析任务</h2><p>上传视频并开始解析后，用量会自动显示在这里。</p><Link className="button-secondary" to="/library">前往资料库</Link></div> : null}

      {ready && visible.length > 0 && view === "models" ? <section className={styles.tablePanel} aria-label="按模型统计用量">
        <div className={styles.tableScroll} role="region" aria-label="模型用量明细" tabIndex={0}>
          <table className={styles.table}>
            <thead><tr><th>模型 / 服务商</th><th>用途</th><th>调用</th><th>输入 Token</th><th>输出 Token</th><th>缓存输入</th><th>推理 Token</th><th>总 Token</th><th>预估费用</th></tr></thead>
            <tbody>{totals.models.map((item) => <tr key={JSON.stringify([item.provider, item.model, item.category, item.operation])}>
              <th scope="row">{item.model}<small>{item.provider}</small></th>
              <td>{operationLabels[item.operation] ?? item.operation}<small>{item.category === "embedding" ? "向量化" : "解析"}</small></td>
              <td>{formatTokens(item.callCount)}</td><td>{formatTokens(item.inputTokens)}</td><td>{formatTokens(item.outputTokens)}</td><td>{formatTokens(item.cachedInputTokens)}</td><td>{formatTokens(item.reasoningTokens)}</td><td>{formatTokens(item.totalTokens)}</td><td>{formatCostCny(item.estimatedCostCny)}{item.missingUsageCount || item.unpricedCallCount ? <small className={styles.warning}>记录不完整</small> : null}</td>
            </tr>)}</tbody>
          </table>
          {totals.models.length === 0 ? <p className={styles.noRows}>暂无已记录的模型调用，可切换“按任务”查看记录状态。</p> : null}
        </div>
        <p className={styles.tableNote}>缓存输入属于输入 Token，推理 Token 属于输出 Token，不重复计入总量。</p>
      </section> : null}

      {ready && visible.length > 0 && view === "tasks" ? <section className={styles.tablePanel} aria-label="按任务统计用量">
        <div className={styles.tableScroll} role="region" aria-label="任务用量明细" tabIndex={0}>
          <table className={styles.table}>
            <thead><tr><th>视频 / 系列</th><th>记录状态</th><th>已记录调用</th><th>总 Token</th><th>预估费用</th><th>执行尝试</th></tr></thead>
            <tbody>{visible.map(({ job, usage, error }) => <tr key={job.jobId}>
              <th scope="row">{job.originalFilename}<small>{job.seriesName}</small></th>
              <td className={error || usage?.coverage !== "recorded" || usage.missingUsageCount || usage.unpricedCallCount ? styles.warning : undefined}>{error ? "读取失败" : usage?.coverage === "unknown" ? "历史未记录" : usage?.coverage === "not_started" ? "尚未开始" : usage?.coverage === "partial" || usage?.missingUsageCount || usage?.unpricedCallCount ? "记录不完整" : "已记录"}{error ? <small>{error}</small> : null}</td>
              <td>{usage ? formatTokens(usage.callCount) : "—"}</td><td>{formatTokens(usage?.totalTokens ?? null)}</td><td>{formatCostCny(usage?.estimatedCostCny ?? null)}</td><td>{job.attemptCount} 次</td>
            </tr>)}</tbody>
          </table>
        </div>
      </section> : null}
    </main>
  );
}
