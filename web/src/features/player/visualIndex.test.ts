import { describe, expect, it } from "vitest";
import type { VideoVisualCue } from "../../api/contracts";
import { createVisualIndex, mergeVisualMatches, type VisualMatch } from "./visualIndex";

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

  it("retains confirmed jumps and preserves retrieval rank for distinct related shots", () => {
    const exact: VisualMatch = { startMs: 500, endMs: 600, source: "visual", text: "原片" };
    const similar = (startMs: number, endMs: number): VisualMatch => ({ startMs, endMs, source: "related", text: "相关" });
    const suggestions = [similar(400, 650), similar(9500, 9700), similar(1100, 1200),
      similar(7200, 7500), similar(5000, 5100), similar(2000, 2500), similar(100, 150)];
    expect(mergeVisualMatches([exact], suggestions).map(hit => hit.startMs)).toEqual([500, 9500, 1100, 7200, 5000, 2000]);
  });

  it("keeps a known offscreen speaker out of both appearance and semantic suggestions", () => {
    const find = createVisualIndex([phone], ["千早愛音", "高松燈"]);
    expect(find("高 松 灯")).toMatchObject({ matches: [], personOnly: true, allowSemantic: false });
    expect(find("高松灯看手机")).toMatchObject({ matches: [], allowSemantic: false });
    expect(find("手机").allowSemantic).toBe(true);
    expect(find("！？").allowSemantic).toBe(false);
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
