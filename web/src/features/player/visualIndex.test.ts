import { describe, expect, it } from "vitest";
import type { VideoVisualCue } from "../../api/contracts";
import { createVisualIndex } from "./visualIndex";

const scene = (startMs: number, endMs: number, description: string, visiblePersons: string[], kind = "SCENE"): VideoVisualCue =>
  ({ startMs, endMs, kind, description, visiblePersons });

const phone = scene(1100, 2500, "粉发少女低头看着手机。", ["千早愛音"]);
const nested = scene(1150, 1500, "嗯。", ["千早愛音"], "DIALOGUE");
const mention = scene(3100, 4500, "灯想起了爱音。", ["高松燈"]);
const both = scene(7000, 8000, "两人收拾吉他包。", ["千早愛音", "高松燈"], "ACTION");
const bothAgain = scene(8000, 9000, "两人走出教室。", ["千早愛音", "高松燈"], "ACTION");
const dialogue = scene(9500, 9700, "想买手机。", ["千早愛音"], "DIALOGUE");
const lookup = createVisualIndex([mention, nested, phone, bothAgain, both, dialogue]);

describe("single-video visual find", () => {
  it.each(["爱音", "愛　 音", "千早爱音"])("finds annotated appearances and preserves separate shots for %s", query => {
    const hits = lookup(query);
    expect(hits.personOnly).toBe(true);
    expect(hits.matches.map(hit => hit.startMs)).toEqual([1100, 7000, 8000, 9500]);
    expect(hits.matches.at(-1)?.text).toBe("千早愛音");
  });

  it("requires every named person and searches only visual descriptions for actions", () => {
    expect(lookup("爱音和高松燈").matches.map(hit => hit.startMs)).toEqual([7000, 8000]);
    const search = lookup("爱音看手机");
    expect(search.personOnly).toBe(false);
    expect(search.matches.map(hit => hit.startMs)).toEqual([1100]);
    expect(search.accepts(phone)).toBe(true);
    expect(search.accepts(mention)).toBe(false);
    expect(search.accepts(dialogue)).toBe(false);
    expect(lookup("辣椒").matches).toEqual([]);
    expect(lookup(" ").matches).toEqual([]);
  });

  it("keeps Latin words searchable without assigning part of a longer word to a character", () => {
    const a = scene(100, 200, "A warm room with a guitar.", ["Ann"]);
    const b = scene(210, 300, "The annual concert.", []);
    const search = createVisualIndex([a, b]);
    expect(search("warm guitar").matches.map(hit => hit.startMs)).toEqual([100]);
    expect(search("annual").matches.map(hit => hit.startMs)).toEqual([210]);
    expect(search("ann").matches.map(hit => hit.startMs)).toEqual([100]);
  });
});
