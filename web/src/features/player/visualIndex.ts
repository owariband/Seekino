import type { VideoVisualCue } from "../../api/contracts";
import { videoTextKey } from "./videoTextSearch";

export interface VisualMatch {
  startMs: number;
  endMs: number;
  text: string;
  source: "visual" | "related";
}

const sceneKinds = new Set(["SCENE", "ACTION", "EMOTION"]);
const particles = new Set(["的", "了", "着", "在", "和", "与", "跟", "and", "the", "a", "an", "with"]);
const segmenter = new Intl.Segmenter("zh", { granularity: "word" });

// Preserve word boundaries in Latin phrases while sharing the literal finder's
// Traditional/Simplified and inter-character whitespace normalization.
function phraseKey(text: string): string {
  return videoTextKey(text.replace(/([\p{Script=Latin}\d])\s+(?=[\p{Script=Latin}\d])/gu, "$1\u0001"))
    .replaceAll("\u0001", " ");
}

function nameForms(name: string): string[] {
  if (/^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]{3,5}$/u.test(name)) {
    return [name, name.slice(0, 2), name.slice(-2)];
  }
  return [name];
}

export function mergeVisualMatches(matches: VisualMatch[]): VisualMatch[] {
  const ordered = matches.toSorted((a, b) => a.startMs - b.startMs
    || Number(a.source === "related") - Number(b.source === "related") || b.endMs - a.endMs);
  const merged: VisualMatch[] = [];
  for (const match of ordered) {
    const previous = merged.at(-1);
    // Keep the timestamp and description tied to the same source event.
    if (previous && previous.startMs <= match.startMs && previous.endMs >= match.endMs) continue;
    merged.push(match);
  }
  return merged;
}

export function createVisualIndex(cues: VideoVisualCue[]) {
  const aliases = new Map<string, Set<string>>();
  const entries = cues.map(cue => {
    const visible = cue.visiblePersons.map(phraseKey).filter(Boolean);
    for (const name of visible) {
      for (const alias of nameForms(name)) {
        if (!aliases.has(alias)) aliases.set(alias, new Set());
        aliases.get(alias)!.add(name);
      }
    }
    return { cue, visible, description: sceneKinds.has(cue.kind) ? phraseKey(cue.description) : "" };
  });
  const names = [...aliases.keys()].sort((a, b) => b.length - a.length);

  return (query: string) => {
    let rest = phraseKey(query);
    const groups: Set<string>[] = [];
    for (const name of names) {
      if (!rest.includes(name)) continue;
      if (/^[\p{Script=Latin}\d ]+$/u.test(name)) {
        const word = new RegExp(`(?<![\\p{Script=Latin}\\d])${name}(?![\\p{Script=Latin}\\d])`, "gu");
        if (!word.test(rest)) continue;
        rest = rest.replace(word, " ");
      } else {
        rest = rest.replaceAll(name, " ");
      }
      groups.push(aliases.get(name)!);
    }
    const terms = [...segmenter.segment(rest)].filter(part => part.isWordLike && !particles.has(part.segment))
      .map(part => part.segment);
    const personOnly = groups.length > 0 && terms.length === 0;
    const hasPeople = (visible: string[]) => groups.every(group => visible.some(name => group.has(name)));
    const accepts = (cue: VideoVisualCue) => sceneKinds.has(cue.kind) && hasPeople(cue.visiblePersons.map(phraseKey));
    const matches: VisualMatch[] = [];
    for (const { cue, visible, description } of entries) {
      if (!hasPeople(visible)) continue;
      if (!personOnly && (!terms.length || !description || !terms.every(term => description.includes(term)))) continue;
      matches.push({ startMs: cue.startMs, endMs: cue.endMs, source: "visual",
        text: description ? cue.description : cue.visiblePersons.join("、") });
    }
    return { matches: mergeVisualMatches(matches), personOnly, accepts };
  };
}
