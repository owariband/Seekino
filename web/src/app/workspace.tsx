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
  createSeries as createSeriesRequest,
  getSeries,
  listSeries,
  updateSeries as updateSeriesRequest,
  shouldRetryApiRequest,
  WORKSPACE_ID,
} from "../api/client";
import type {
  RuntimeMode,
  SeriesCreateRequest,
  SeriesUpdateRequest,
  SeriesView,
  SeriesSummary,
} from "../api/contracts";
const SERIES_STORAGE_KEY = "anivag-selected-series";

interface WorkspaceContextValue {
  mode: RuntimeMode;
  series: SeriesSummary[];
  selectedSeries: SeriesView | null;
  selectedSeriesId: string | null;
  isLoading: boolean;
  error: Error | null;
  selectSeries: (seriesId: string) => void;
  createSeries: (input: Omit<SeriesCreateRequest, "workspaceId">) => Promise<SeriesView>;
  updateSeries: (input: Omit<SeriesUpdateRequest, "workspaceId">) => Promise<SeriesView>;
  refreshSeries: () => Promise<void>;
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [selectedSeriesId, setSelectedSeriesId] = useState<string | null>(
    () => window.localStorage.getItem(SERIES_STORAGE_KEY),
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
      window.localStorage.setItem(SERIES_STORAGE_KEY, selectedSeriesId);
    } else {
      window.localStorage.removeItem(SERIES_STORAGE_KEY);
    }
  }, [selectedSeriesId]);

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
        const created = await createSeriesRequest({ ...input, workspaceId: WORKSPACE_ID });
        await queryClient.invalidateQueries({ queryKey: ["series"] });
        setSelectedSeriesId(created.seriesId);
        return created;
      },
      updateSeries: async (input) => {
        const updated = await updateSeriesRequest({ ...input, workspaceId: WORKSPACE_ID });
        queryClient.setQueryData(["series-detail", updated.seriesId], updated);
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
