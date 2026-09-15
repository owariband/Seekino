import type { ParseJobView, ParseUsageRowVO, ParseUsageVO } from "../../api/contracts";

export interface UsageEntry {
  job: ParseJobView;
  usage: ParseUsageVO | null;
  error: string | null;
}

function sumTokens(values: (number | null)[]): number | null {
  return values.some((value) => value === null) ? null : values.reduce<number>((sum, value) => sum + value!, 0);
}

function sumCosts(values: (string | null)[]): string | null {
  if (values.some((value) => value === null)) return null;
  const parts = values.map((value) => value!.split("."));
  const scale = Math.max(0, ...parts.map(([, fraction = ""]) => fraction.length));
  const total = parts.reduce((sum, [integer, fraction = ""]) => sum + BigInt(`${integer}${fraction.padEnd(scale, "0")}`), 0n);
  if (scale === 0) return total.toString();
  const digits = total.toString().padStart(scale + 1, "0");
  return `${digits.slice(0, -scale)}.${digits.slice(-scale)}`;
}

export function summarizeUsage(entries: UsageEntry[]) {
  const groups = new Map<string, ParseUsageRowVO>();
  for (const entry of entries) {
    for (const row of entry.usage?.items ?? []) {
      const key = JSON.stringify([row.provider, row.model, row.category, row.operation]);
      const previous = groups.get(key);
      groups.set(key, previous ? {
        ...row,
        callCount: previous.callCount + row.callCount,
        inputTokens: sumTokens([previous.inputTokens, row.inputTokens]),
        outputTokens: sumTokens([previous.outputTokens, row.outputTokens]),
        cachedInputTokens: sumTokens([previous.cachedInputTokens, row.cachedInputTokens]),
        reasoningTokens: sumTokens([previous.reasoningTokens, row.reasoningTokens]),
        totalTokens: sumTokens([previous.totalTokens, row.totalTokens]),
        estimatedCostCny: sumCosts([previous.estimatedCostCny, row.estimatedCostCny]),
        missingUsageCount: previous.missingUsageCount + row.missingUsageCount,
        unpricedCallCount: previous.unpricedCallCount + row.unpricedCallCount,
      } : { ...row });
    }
  }
  const knownTokens = entries.flatMap(({ usage }) => usage?.totalTokens !== null && usage?.totalTokens !== undefined ? [usage.totalTokens] : []);
  const knownCosts = entries.flatMap(({ usage }) => usage?.estimatedCostCny !== null && usage?.estimatedCostCny !== undefined ? [usage.estimatedCostCny] : []);
  return {
    totalTokens: sumTokens(entries.map(({ usage }) => usage?.totalTokens ?? null)),
    estimatedCostCny: sumCosts(entries.map(({ usage }) => usage?.estimatedCostCny ?? null)),
    knownTokens: knownTokens.length ? sumTokens(knownTokens) : null,
    knownCostCny: knownCosts.length ? sumCosts(knownCosts) : null,
    callCount: entries.reduce((sum, { usage }) => sum + (usage?.callCount ?? 0), 0),
    models: [...groups.values()].sort((left, right) => left.model.localeCompare(right.model) || left.provider.localeCompare(right.provider) || left.operation.localeCompare(right.operation)),
  };
}
