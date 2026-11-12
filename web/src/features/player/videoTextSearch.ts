import { CustomConverter } from "opencc-js/core";
import traditionalCharacters from "opencc-js/dict/TSCharacters";

import type { VideoTextTimeline } from "../../api/contracts";

// Character mappings keep a substring searchable even when a neighbour changes.
const toSimplified = CustomConverter(traditionalCharacters);

export function videoTextKey(text: string): string {
  return toSimplified(text.replace(/\s+/gu, "").toLowerCase());
}

export function createVideoTextSearch(cues: VideoTextTimeline["cues"]) {
  const entries = cues.map(cue => ({ cue, keys: cue.texts.map(videoTextKey) }));
  return (query: string | readonly string[]) => {
    const needles = [...new Set((typeof query === "string" ? [query] : query).map(videoTextKey).filter(Boolean))];
    if (!needles.length) return [];
    return entries.flatMap(({ cue, keys }) => {
      for (const needle of needles) {
        const variant = keys.findIndex(key => key.includes(needle));
        if (variant >= 0) return [{ cue, text: cue.texts[variant]! }];
      }
      return [];
    });
  };
}
