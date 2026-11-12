import { useEffect, useMemo, useState } from "react";

import { interpretVideoFindQuery } from "../../api/client";
import type { VideoFindTerms } from "../../api/contracts";

interface Interpretation {
  key: string;
  terms: VideoFindTerms | null;
}

export function useWatchFindQuery(videoId: string, tenantId: string, query: string, enabled: boolean, attempt: number) {
  const raw = query.trim();
  const key = JSON.stringify([tenantId, videoId, raw, attempt]);
  const interpret = enabled && raw.length > 0 && raw.length <= 500;
  const [settled, setSettled] = useState<Interpretation | null>(null);
  const current = interpret && settled?.key === key ? settled : null;

  useEffect(() => {
    if (!interpret || current) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      interpretVideoFindQuery(raw, videoId, tenantId, controller.signal)
        .then(terms => { if (!controller.signal.aborted) setSettled({ key, terms }); })
        .catch(() => { if (!controller.signal.aborted) setSettled({ key, terms: null }); });
    }, 400);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [interpret, current, raw, key, tenantId, videoId]);

  const expansion = current?.terms;
  const terms = useMemo(() => expansion
    ? [...new Set([raw, expansion.query, ...expansion.synonyms])]
    : raw ? [raw] : [], [raw, expansion]);
  const loading = interpret && !current;
  return { terms, semanticQuery: loading ? "" : expansion?.query ?? raw, loading, failed: current?.terms === null };
}
