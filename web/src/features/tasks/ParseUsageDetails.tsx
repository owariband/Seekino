import { useQuery } from "@tanstack/react-query";
import { ChevronDown, CircleAlert, Rows3 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { getUsageOverview, shouldRetryApiRequest, TENANT_ID } from "../../api/client";
import type { UsageTotals, VideoParseTaskStatus } from "../../api/contracts";
import { formatMoneyCny, formatTokens } from "../../shared/format";
import styles from "./tasks.module.css";

const displayTokens = (value: number | null) => value === null ? "—" : formatTokens(value);
const displayApiCost = (cost: UsageTotals["cost"], truncated: boolean) => {
  if (!cost) return "未计价";
  if (cost.excludedCallCount > 0 && cost.pricedCallCount === 0 && cost.unpricedCallCount === 0) return "不适用（Codex订阅）";
  if (cost.pricedCallCount === 0 && cost.unpricedCallCount > 0) return "未计价";
  const subtotal = truncated || cost.unpricedCallCount > 0 || cost.estimatedAmount === null;
  const amount = subtotal ? cost.knownAmount : cost.estimatedAmount;
  return amount === null ? "未计价" : `${formatMoneyCny(amount)}${subtotal ? "（小计）" : ""}`;
};

const operationLabels: Record<string, string> = {
  candidate_transcript: "候选对白识别",
  video_timeline: "视频时间线解析",
  neutral_visual_evidence: "视觉证据核验",
  fallback_video_timeline: "备用时间线整理",
  text_embedding: "文本向量化",
  video_embedding: "视频向量化",
  understand: "理解",
  refine: "Refine",
  index: "索引发布",
  sdk_turn_run: "原生 Agent 执行",
};

export function ParseUsageDetails({ runId, status }: { runId: string; status: VideoParseTaskStatus }) {
  const [expanded, setExpanded] = useState(false);
  const previousStatus = useRef(status);
  const terminal = status === "COMPLETE" || status === "PARSING_FAILED" || status === "INDEXING_FAILED";
  const usageQuery = useQuery({
    queryKey: ["usage-overview", TENANT_ID, "run", runId],
    queryFn: ({ signal }) => getUsageOverview({ runId }, signal),
    enabled: expanded,
    retry: shouldRetryApiRequest,
    staleTime: 30_000,
    refetchInterval: expanded ? 60_000 : false,
  });
  useEffect(() => {
    const previous = previousStatus.current;
    previousStatus.current = status;
    if (expanded && terminal && previous !== status) void usageQuery.refetch();
  }, [expanded, terminal, status, usageQuery.refetch]);
  const overview = usageQuery.data;
  const totals = overview?.status === "available" ? overview.totals : undefined;
  const knownCostSubtotal = Boolean(overview?.truncated || (totals?.cost && (
    totals.cost.unpricedCallCount > 0 || totals.cost.pricedCallCount > 0 && totals.cost.estimatedAmount === null
  )));

  return (
    <details className={styles.usageDetails} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary><Rows3 size={14} />模型消耗<span>Token 与 API 费用</span><ChevronDown size={14} className={styles.usageChevron} /></summary>
      {expanded ? <div className={styles.usageBody}>
        {usageQuery.isPending ? <p role="status">正在读取模型消耗…</p> : null}
        {usageQuery.error instanceof Error ? <div className={styles.jobError} role="alert"><CircleAlert size={15} /><div><strong>模型消耗暂时无法读取</strong><p>{usageQuery.error.message}</p><button className="button-secondary" type="button" disabled={usageQuery.isFetching} onClick={() => void usageQuery.refetch()}>重新读取消耗</button></div></div> : null}
        {overview && !totals ? <p className={styles.usageNotice} role="status">{overview.message ?? "模型消耗暂时无法读取，请稍后重试。"}</p> : null}
        {overview && totals ? <>
          <p className={styles.usageSource}>{runId} · 最近 30 天已记录调用</p>
          <dl className={styles.usageTotals} aria-label="本次任务模型消耗合计">
            <div><dt>{totals.totalTokens === null || overview.truncated ? "已知 Token 小计" : "已记录 Token"}</dt><dd>{displayTokens(totals.totalTokens ?? totals.knownTokens)}</dd></div>
            <div><dt>调用记录</dt><dd>{displayTokens(totals.callCount)}</dd></div>
            <div><dt>{knownCostSubtotal ? "已计价API小计" : "API预估费用"}</dt><dd>{displayApiCost(totals.cost, overview.truncated)}</dd></div>
          </dl>
          {overview.models.length ? <div className={styles.usageTableScroll}><table className={styles.usageTable}>
            <caption>本次任务已记录的模型调用</caption>
            <thead><tr><th>模型 / 服务商</th><th>来源（src）</th><th>职责</th><th>调用记录</th><th>输入 Token</th><th>输出 Token</th><th>缓存输入</th><th>缓存写入</th><th>推理 Token</th><th>总 Token</th><th>API预估费用</th></tr></thead>
            <tbody>{overview.models.map((item) => <tr key={JSON.stringify([item.provider, item.src, item.model, item.category, item.operation])}>
              <th scope="row">{item.model ?? "未知模型"}<small>{item.provider ?? "未知服务商"}</small></th>
              <td>{item.src?.trim() || "未记录"}</td>
              <td>{item.category ? `${item.category === "embedding" ? "向量化" : item.category === "parse" ? "解析" : item.category} · ` : ""}{item.operation ? operationLabels[item.operation] ?? item.operation : "未知用途"}</td>
              <td>{displayTokens(item.callCount)}</td><td>{displayTokens(item.inputTokens)}</td><td>{displayTokens(item.outputTokens)}</td><td>{displayTokens(item.cachedInputTokens)}</td><td>{displayTokens(item.cacheWriteInputTokens)}</td><td>{displayTokens(item.reasoningTokens)}</td><td>{displayTokens(item.totalTokens)}{item.totalTokens === null ? <small>已知小计 {displayTokens(item.knownTokens)}</small> : null}</td>
              <td>{displayApiCost(item.cost, overview.truncated)}{item.cost?.unpricedCallCount ? <small>{item.cost.unpricedCallCount} 条未计价</small> : null}</td>
            </tr>)}</tbody>
          </table></div> : <p className={styles.usageNotice}>当前窗口暂无已记录的模型调用。</p>}
          {totals.missingUsageCount ? <p className={styles.usageNotice}>{totals.missingUsageCount} 条调用记录缺少完整 Token 用量，显示已知小计。</p> : null}
          {totals.cost?.unpricedCallCount ? <p className={styles.usageNotice}>{totals.cost.unpricedCallCount} 条 API 调用未计价，费用仅显示已计价部分。</p> : null}
          {overview.truncated ? <p className={styles.usageNotice}>当前读取结果被截断，不能代表完整消耗。</p> : null}
          <p className={styles.usageNotice}>API 费用按配置单价估算，以人民币显示，不含 Codex 订阅费用。Codex 仅统计 Token；Token 不能直接换算为每周额度百分比。</p>
          <p className={styles.usageNotice}>缓存读写与推理 Token 是用量子集，不重复计入总量。</p>
          <p className={styles.usageNotice}>仅统计已记录调用；未知细项显示为 —，未记录的历史用量无法还原。</p>
        </> : null}
      </div> : null}
    </details>
  );
}
