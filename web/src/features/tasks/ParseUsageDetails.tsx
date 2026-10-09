import { useQuery } from "@tanstack/react-query";
import { ChevronDown, CircleAlert, Rows3 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { getUsageOverview, shouldRetryApiRequest, TENANT_ID } from "../../api/client";
import type { ParseJobStatus } from "../../api/contracts";
import { formatTokens } from "../../shared/format";
import styles from "./tasks.module.css";

const displayTokens = (value: number | null) => value === null ? "—" : formatTokens(value);

const operationLabels: Record<string, string> = {
  candidate_transcript: "候选对白识别",
  video_timeline: "视频时间线解析",
  neutral_visual_evidence: "视觉证据核验",
  fallback_video_timeline: "备用时间线整理",
  text_embedding: "文本向量化",
  video_embedding: "视频向量化",
};

export function ParseUsageDetails({ jobId, status }: { jobId: string; status: ParseJobStatus }) {
  const [expanded, setExpanded] = useState(false);
  const previousStatus = useRef(status);
  const terminal = status === "SUCCEEDED" || status === "FAILED";
  const usageQuery = useQuery({
    queryKey: ["usage-overview", TENANT_ID, "run", jobId],
    queryFn: ({ signal }) => getUsageOverview({ runId: jobId }, signal),
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

  return (
    <details className={styles.usageDetails} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary><Rows3 size={14} />模型消耗<span>已记录 Token</span><ChevronDown size={14} className={styles.usageChevron} /></summary>
      {expanded ? <div className={styles.usageBody}>
        {usageQuery.isPending ? <p role="status">正在读取模型消耗…</p> : null}
        {usageQuery.error instanceof Error ? <div className={styles.jobError} role="alert"><CircleAlert size={15} /><div><strong>模型消耗暂时无法读取</strong><p>{usageQuery.error.message}</p><button className="button-secondary" type="button" disabled={usageQuery.isFetching} onClick={() => void usageQuery.refetch()}>重新读取消耗</button></div></div> : null}
        {overview && !totals ? <p className={styles.usageNotice} role="status">{overview.message ?? "模型消耗暂时无法读取，请稍后重试。"}</p> : null}
        {overview && totals ? <>
          <p className={styles.usageSource}>{jobId} · 最近 30 天已记录调用</p>
          <dl className={styles.usageTotals} aria-label="本次任务模型消耗合计">
            <div><dt>{totals.totalTokens === null || overview.truncated ? "已知 Token 小计" : "已记录 Token"}</dt><dd>{displayTokens(totals.totalTokens ?? totals.knownTokens)}</dd></div>
            <div><dt>调用记录</dt><dd>{displayTokens(totals.callCount)}</dd></div>
          </dl>
          {overview.models.length ? <div className={styles.usageTableScroll}><table className={styles.usageTable}>
            <caption>本次任务已记录的模型调用</caption>
            <thead><tr><th>模型 / 服务商</th><th>职责</th><th>调用记录</th><th>输入 Token</th><th>输出 Token</th><th>缓存输入</th><th>缓存写入</th><th>推理 Token</th><th>总 Token</th></tr></thead>
            <tbody>{overview.models.map((item) => <tr key={JSON.stringify([item.provider, item.model, item.category, item.operation])}>
              <th scope="row">{item.model ?? "未知模型"}<small>{item.provider ?? "未知服务商"}</small></th>
              <td>{item.category ? `${item.category === "embedding" ? "向量化" : item.category === "parse" ? "解析" : item.category} · ` : ""}{item.operation ? operationLabels[item.operation] ?? item.operation : "未知用途"}</td>
              <td>{displayTokens(item.callCount)}</td><td>{displayTokens(item.inputTokens)}</td><td>{displayTokens(item.outputTokens)}</td><td>{displayTokens(item.cachedInputTokens)}</td><td>{displayTokens(item.cacheWriteInputTokens)}</td><td>{displayTokens(item.reasoningTokens)}</td><td>{displayTokens(item.totalTokens)}{item.totalTokens === null ? <small>已知小计 {displayTokens(item.knownTokens)}</small> : null}</td>
            </tr>)}</tbody>
          </table></div> : <p className={styles.usageNotice}>当前窗口暂无已记录的模型调用。</p>}
          {totals.missingUsageCount ? <p className={styles.usageNotice}>{totals.missingUsageCount} 条调用记录缺少完整 Token 用量，显示已知小计。</p> : null}
          {overview.truncated ? <p className={styles.usageNotice}>当前读取结果被截断，不能代表完整消耗。</p> : null}
          <p className={styles.usageNotice}>仅统计已记录调用；未知细项显示为 —，未记录的历史用量无法还原。</p>
        </> : null}
      </div> : null}
    </details>
  );
}
