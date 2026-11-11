import { CustomConverter } from "opencc-js/core";
import traditionalCharacters from "opencc-js/dict/TSCharacters";

import type { VideoTextTimeline } from "../../api/contracts";

// Character mappings keep a substring searchable even when a neighbour changes.
const toSimplified = CustomConverter(traditionalCharacters);

function videoTextKey(text: string): string {
  return toSimplified(text.replace(/\s+/gu, "").toLowerCase());
}

export function createVideoTextSearch(cues: VideoTextTimeline["cues"]) {
  const entries = cues.map(cue => ({ cue, keys: cue.texts.map(videoTextKey) }));
  return (query: string) => {
    const needle = videoTextKey(query);
    if (!needle) return [];
    return entries.flatMap(({ cue, keys }) => {
      const variant = keys.findIndex(key => key.includes(needle));
      return variant === -1 ? [] : [{ cue, text: cue.texts[variant]! }];
    });
  };
}
