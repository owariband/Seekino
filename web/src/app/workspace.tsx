import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  type PropsWithChildren,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  checkHealth,
  identityStorageKey,
  createSeries as createSeriesRequest,
  getSeries,
  listSeries,
  updateSeries as updateSeriesRequest,
  updateVideoEpisode as updateVideoEpisodeRequest,
  shouldRetryApiRequest,
  TENANT_ID,
} from "../api/client";
import type {
  RuntimeMode,
  SeriesCreateRequest,
  SeriesUpdateRequest,
  SeriesView,
  SeriesSummary,
  VideoEpisodeUpdateRequest,
  VideoSummary,
} from "../api/contracts";

interface WorkspaceContextValue {
  mode: RuntimeMode;
  series: SeriesSummary[];
  selectedSeries: SeriesView | null;
  selectedSeriesId: string | null;
  isLoading: boolean;
  error: Error | null;
  selectSeries: (seriesId: string) => void;
  createSeries: (input: Omit<SeriesCreateRequest, "tenantId">) => Promise<SeriesView>;
  updateSeries: (input: Omit<SeriesUpdateRequest, "tenantId">) => Promise<SeriesView>;
  updateVideoEpisode: (input: Omit<VideoEpisodeUpdateRequest, "tenantId">) => Promise<VideoSummary>;
  refreshSeries: () => Promise<void>;
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const seriesStorageKey = identityStorageKey("anivag-selected-series", "anivag-selected-series");
  const [selectedSeriesId, setSelectedSeriesId] = useState<string | null>(
    () => window.localStorage.getItem(seriesStorageKey),
  );

  const health = useQuery({
    queryKey: ["health"],
    queryFn: checkHealth,
    retry: shouldRetryApiRequest,
    refetchInterval: 15_000,
  });

  const seriesQuery = useQuery({
    queryKey: ["series"],
    queryFn: async () => {
      const items: SeriesSummary[] = [];
      let cursor: string | undefined;
      do {
        const page = await listSeries(cursor);
        items.push(...page.items);
        if (page.hasMore && !page.nextCursor) throw new Error("系列列表缺少下一页游标");
        cursor = page.hasMore ? page.nextCursor! : undefined;
      } while (cursor);
      return items;
    },
    retry: shouldRetryApiRequest,
  });

  const mode: RuntimeMode =
    health.data === true || seriesQuery.isSuccess
      ? "live"
      : health.isPending || seriesQuery.isPending
        ? "checking"
        : "offline";
  const series = seriesQuery.data ?? [];

  useEffect(() => {
    if (series.length === 0) {
      setSelectedSeriesId(null);
      return;
    }
    if (!selectedSeriesId || !series.some((item) => item.seriesId === selectedSeriesId)) {
      setSelectedSeriesId(series[0]!.seriesId);
    }
  }, [selectedSeriesId, series]);

  useEffect(() => {
    if (selectedSeriesId) {
      window.localStorage.setItem(seriesStorageKey, selectedSeriesId);
    } else {
      window.localStorage.removeItem(seriesStorageKey);
    }
  }, [selectedSeriesId, seriesStorageKey]);

  const selectedSummary =
    series.find((item) => item.seriesId === selectedSeriesId) ?? null;
  const selectedSeriesQuery = useQuery({
    queryKey: ["series-detail", selectedSummary?.seriesId],
    queryFn: () => getSeries(selectedSummary!.seriesId),
    enabled: Boolean(selectedSummary),
    retry: shouldRetryApiRequest,
  });

  const selectedSeries = selectedSeriesQuery.data ?? null;
  const error =
    seriesQuery.error instanceof Error
      ? seriesQuery.error
      : selectedSeriesQuery.error instanceof Error
        ? selectedSeriesQuery.error
        : null;

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      mode,
      series,
      selectedSeries,
      selectedSeriesId,
      isLoading:
        seriesQuery.isPending ||
        (Boolean(selectedSummary) && selectedSeriesQuery.isPending),
      error,
      selectSeries: setSelectedSeriesId,
      createSeries: async (input) => {
        const created = await createSeriesRequest({ ...input, tenantId: TENANT_ID });
        await queryClient.invalidateQueries({ queryKey: ["series"] });
        setSelectedSeriesId(created.seriesId);
        return created;
      },
      updateSeries: async (input) => {
        const updated = await updateSeriesRequest({ ...input, tenantId: TENANT_ID });
        queryClient.setQueryData(["series-detail", updated.seriesId], updated);
        await queryClient.invalidateQueries({ queryKey: ["series"] });
        return updated;
      },
      updateVideoEpisode: async (input) => {
        const updated = await updateVideoEpisodeRequest({ ...input, tenantId: TENANT_ID });
        queryClient.setQueryData<SeriesView>(["series-detail", updated.seriesId], (current) => current ? {
          ...current,
          videos: current.videos
            .map((video) => video.videoId === updated.videoId ? updated : video)
            .sort((left, right) => left.episodeOrder - right.episodeOrder),
        } : current);
        await queryClient.invalidateQueries({ queryKey: ["series"] });
        return updated;
      },
      refreshSeries: async () => {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["series"] }),
          queryClient.invalidateQueries({ queryKey: ["series-detail"] }),
        ]);
      },
    }),
    [
      error,
      mode,
      queryClient,
      selectedSeries,
      selectedSeriesId,
      series,
      seriesQuery.isPending,
      selectedSeriesQuery.isPending,
    ],
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return value;
}
