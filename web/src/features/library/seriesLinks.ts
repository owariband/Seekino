import type { SeriesSummary } from "../../api/contracts";

type SeriesSource = Pick<SeriesSummary, "seriesId" | "tenantId">;

export function seriesPath(series: SeriesSource): string {
  return `/series/${encodeURIComponent(series.seriesId)}?${new URLSearchParams({ tenantId: series.tenantId })}`;
}

export function watchPath(series: SeriesSource, videoId: string): string {
  return `/watch/${encodeURIComponent(series.seriesId)}/${encodeURIComponent(videoId)}?${new URLSearchParams({ tenantId: series.tenantId })}`;
}
