import type { ParseJobView, ParseUsageRowVO, ParseUsageVO } from "../../api/contracts";

export interface UsageEntry {
  job: ParseJobView;
  usage: ParseUsageVO | null;
  error: string | null;
}

function sumKnownTokens(values: (number | null)[]): number | null {
  const known = values.filter((value): value is number => value !== null);
  return known.length ? known.reduce((sum, value) => sum + value, 0) : null;
}

function sumKnownCosts(values: (string | null)[]): string | null {
  const known = values.filter((value): value is string => value !== null);
  if (!known.length) return null;
  const parts = known.map((value) => value.split("."));
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
        inputTokens: sumKnownTokens([previous.inputTokens, row.inputTokens]),
        outputTokens: sumKnownTokens([previous.outputTokens, row.outputTokens]),
        cachedInputTokens: sumKnownTokens([previous.cachedInputTokens, row.cachedInputTokens]),
        reasoningTokens: sumKnownTokens([previous.reasoningTokens, row.reasoningTokens]),
        totalTokens: sumKnownTokens([previous.totalTokens, row.totalTokens]),
        estimatedCostCny: sumKnownCosts([previous.estimatedCostCny, row.estimatedCostCny]),
        missingUsageCount: previous.missingUsageCount + row.missingUsageCount,
        unpricedCallCount: previous.unpricedCallCount + row.unpricedCallCount,
      } : { ...row });
    }
  }
  const knownTokens = entries.flatMap(({ usage }) => usage?.totalTokens !== null && usage?.totalTokens !== undefined ? [usage.totalTokens] : []);
  const knownCosts = entries.flatMap(({ usage }) => usage?.estimatedCostCny !== null && usage?.estimatedCostCny !== undefined ? [usage.estimatedCostCny] : []);
  const completeTokens = entries.every(({ usage, error }) =>
    !error && usage?.coverage === "recorded" && usage.missingUsageCount === 0 && usage.totalTokens !== null,
  );
  const completeCosts = entries.every(({ usage, error }) =>
    !error && usage?.coverage === "recorded" && usage.missingUsageCount === 0 && usage.unpricedCallCount === 0
      && usage.estimatedCostCny !== null,
  );
  return {
    totalTokens: entries.length === 0 ? 0 : completeTokens ? sumKnownTokens(knownTokens) : null,
    estimatedCostCny: entries.length === 0 ? "0" : completeCosts ? sumKnownCosts(knownCosts) : null,
    knownTokens: sumKnownTokens(knownTokens),
    knownCostCny: sumKnownCosts(knownCosts),
    callCount: entries.reduce((sum, { usage }) => sum + (usage?.callCount ?? 0), 0),
    models: [...groups.values()].sort((left, right) => left.model.localeCompare(right.model) || left.provider.localeCompare(right.provider) || left.operation.localeCompare(right.operation)),
  };
}
