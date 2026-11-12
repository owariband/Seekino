import { useEffect, useMemo, useState } from "react";

import { getVideoVisualTimeline, searchVideoVisuals } from "../../api/client";
import type { VideoVisualTimeline } from "../../api/contracts";
import type { createVisualIndex, mergeVisualMatches, VisualMatch } from "./visualIndex";

type LocalIndex = { lookup: ReturnType<typeof createVisualIndex>; merge: typeof mergeVisualMatches };
type SemanticResult = { index: LocalIndex; query: string; result: VideoVisualTimeline };
const empty: VisualMatch[] = [];

// VideoTextFind is keyed by tenant and video. Its index lives only for that open panel.
export function useEpisodeVisualFind(videoId: string, tenantId: string, query: string, enabled: boolean, attempt: number) {
  const [index, setIndex] = useState<LocalIndex | null>(null);
  const [timelineFailed, setTimelineFailed] = useState(false);
  const [semantic, setSemantic] = useState<SemanticResult | null>(null);
  const needle = query.trim();

  useEffect(() => {
    if (!enabled || index) return;
    const controller = new AbortController();
    setTimelineFailed(false);
    Promise.all([getVideoVisualTimeline(videoId, tenantId, controller.signal), import("./visualIndex")])
      .then(([timeline, { createVisualIndex, mergeVisualMatches }]) => {
        if (controller.signal.aborted) return;
        if (timeline.ready) setIndex({ lookup: createVisualIndex(timeline.cues, timeline.knownPersons), merge: mergeVisualMatches });
        else setTimelineFailed(true);
      })
      .catch(() => { if (!controller.signal.aborted) setTimelineFailed(true); });
    return () => controller.abort();
  }, [videoId, tenantId, enabled, index, attempt]);

  const local = useMemo(() => enabled && needle && index ? index.lookup(needle) : null, [enabled, needle, index]);
  const needsSemantic = Boolean(local?.allowSemantic && needle.length >= 2 && needle.length <= 500);
  useEffect(() => {
    setSemantic(null);
    if (!index || !needsSemantic) return;
    const controller = new AbortController();
    const publish = (result: VideoVisualTimeline) => {
      if (!controller.signal.aborted) setSemantic({ index, query: needle, result });
    };
    const timer = window.setTimeout(() => {
      searchVideoVisuals(needle, videoId, tenantId, controller.signal)
        .then(publish).catch(() => publish({ ready: false, cues: [] }));
    }, 300);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [videoId, tenantId, needsSemantic, needle, index, attempt]);

  const remote = needsSemantic && semantic?.query === needle && semantic.index === index ? semantic.result : null;
  const matches = useMemo(() => {
    if (!local || !index) return empty;
    if (!remote?.cues.length) return local.matches;
    const candidates: VisualMatch[] = remote.cues.filter(local.accepts).map(cue => ({
      startMs: cue.startMs, endMs: cue.endMs, source: "related", text: cue.description,
    }));
    return index.merge(local.matches, candidates);
  }, [local, index, remote]);
  return {
    matches,
    loading: enabled && Boolean(needle) && ((!index && !timelineFailed) || (needsSemantic && !remote)),
    timelineUnavailable: enabled && timelineFailed,
    semanticUnavailable: remote?.ready === false && !remote.cues.length,
    semanticPartial: remote?.ready === false && remote.cues.length > 0,
  };
}
