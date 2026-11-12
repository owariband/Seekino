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

// Drop contained annotations, then select up to five distinct suggestions by relevance.
export function mergeVisualMatches(exact: VisualMatch[], related: VisualMatch[] = []): VisualMatch[] {
  const confirmed = exact.toSorted((a, b) => a.startMs - b.startMs || b.endMs - a.endMs);
  const matches: VisualMatch[] = [];
  for (const match of confirmed) {
    if (matches.length && matches.at(-1)!.endMs >= match.endMs) continue;
    matches.push(match);
  }
  const confirmedCount = matches.length;
  for (const match of related) {
    if (matches.some(other => other.startMs < match.endMs && match.startMs < other.endMs)) continue;
    matches.push(match);
    if (matches.length - confirmedCount === 5) break;
  }
  return matches;
}

export function createVisualIndex(cues: VideoVisualCue[], knownPersons: string[] = []) {
  const aliases = new Map<string, Set<string>>();
  const entries = cues.map(cue => {
    const visible = cue.visiblePersons.map(phraseKey).filter(Boolean);
    return { cue, visible, description: sceneKinds.has(cue.kind) ? phraseKey(cue.description) : "" };
  });
  // Known people include offscreen speakers; keep this lexicon separate from visual evidence.
  const roster = new Set([...knownPersons.map(phraseKey), ...entries.flatMap(entry => entry.visible)]);
  for (const name of roster) {
    if (!name) continue;
    for (const alias of nameForms(name)) {
      if (!aliases.has(alias)) aliases.set(alias, new Set());
      aliases.get(alias)!.add(name);
    }
  }
  const names = [...aliases.keys()].sort((a, b) => b.length - a.length);

  return (query: string, requirePeople?: (visible: string[]) => boolean) => {
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
    const hasPeople = (visible: string[]) => (!requirePeople || requirePeople(visible))
      && groups.every(group => visible.some(name => group.has(name)));
    const accepts = (cue: VideoVisualCue) => sceneKinds.has(cue.kind) && hasPeople(cue.visiblePersons.map(phraseKey));
    const matches: VisualMatch[] = [];
    let allowSemantic = false;
    for (const { cue, visible, description } of entries) {
      if (!hasPeople(visible)) continue;
      if (!personOnly) {
        if (!terms.length || !description) continue;
        allowSemantic = true;
        if (!terms.every(term => description.includes(term))) continue;
      }
      matches.push({ startMs: cue.startMs, endMs: cue.endMs, source: "visual",
        text: description ? cue.description : cue.visiblePersons.join("、") });
    }
    return { matches: mergeVisualMatches(matches), personOnly, allowSemantic, accepts, hasPeople };
  };
}
