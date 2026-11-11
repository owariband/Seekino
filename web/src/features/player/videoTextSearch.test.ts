import { describe, expect, it } from "vitest";

import { createVideoTextSearch } from "./videoTextSearch";

function cue(...texts: string[]) {
  return { startMs: 1200, endMs: 1500, source: "dialogue" as const, texts };
}

describe("video text matching", () => {
  it.each([
    ["演員表　高 松 燈", "高松灯"],
    ["我是高松灯", "高　松 燈"],
    ["臺 湾的繁體會議", "台灣的繁体会 议"],
    ["字幕　Ａ：My\tGO", "my go"],
  ])("finds the literal text in %s for %s", (source, query) => {
    const entry = cue(source);
    expect(createVideoTextSearch([entry])(query)).toEqual([{ cue: entry, text: source }]);
  });

  it("keeps separate characters and readings distinct", () => {
    const search = createVideoTextSearch([cue("高松 燈")]);
    for (const query of ["松高灯", "ともり", "　 \t"]) expect(search(query)).toEqual([]);
  });

  it("returns the first matching original variant once per cue in timeline order", () => {
    const first = cue("なまえ", "臺灣 鐘錶");
    const second = { ...cue("钟表行", "鐘錶店"), startMs: 3400, endMs: 4000 };
    expect(createVideoTextSearch([first, second])("钟　錶")).toEqual([
      { cue: first, text: "臺灣 鐘錶" }, { cue: second, text: "钟表行" },
    ]);
  });

  it("keeps word boundaries between variants and between cues", () => {
    const search = createVideoTextSearch([cue("圖書", "館藏"), cue("影片")]);
    expect(search("書館")).toEqual([]);
    expect(search("藏影")).toEqual([]);
  });
});
