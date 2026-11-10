# 《Weekend Study》 promo script

The pilot deliverable of the [editing dual-recall TRD](editing-skill-style-reference-trd.md#5-试点验证与方案取舍). The owner approved this script on 2026-11-10. The film is to be made by one long prompt to Claude Code: the editor finds every shot through the Seekino MCP, cuts it out locally and renders with [motion-showreel](https://github.com/ConardLi/garden-skills/tree/main/skills/motion-showreel) (format H: cut-out characters on code-made scenes).

## Owner's original idea

BGM, fixed: `assets/太妍 - Weekend.mp3` (233.4 s).

The owner's 2026-11-07 brief, verbatim:

> 戴着眼镜的anon想要学习LLM的相关学习，看到什么深度神经网络、transformer、attention、各种类型的AI infra课程、gpt感到头大，戴着眼镜的anon咬牙切齿，然后摆烂地躺在沙发上准备玩的时候，这个时候seekino出现了，然后seekino展示自己的功能，让anon眼前一亮

2026-11-10 addition, as given:

> At the same time, I hope to maintain the advanced use of many transitions and camera movements, combined with the multi-character interactions from MyGO to form a script. It's about Anon wanting to learn LLM but always finding it boring and escaping until she meets Seekino. Then Seekino demonstrates its features. Finally, Anon becomes highly interested—not only finishing the videos but also learning how to read papers (?).

The first half was already cut twice in the Weekend trials (A/B/C on 11-07 and film A/B on 11-09, local evidence under `outputs/verification/anon-weekend-*`; [summary on #50](https://github.com/owariband/Seekino/pull/50#issuecomment-6708274214)); the owner blind-picked the version with the automatic style card and noted that the P5 style and Anon's shot choice were still weak.

## Why it was redesigned

Reviewed against *Made to Stick* (the owner's notes 宣传与粘性.md, SUCCESs checklist):

| Principle | Problem in the first draft | Decision |
| --- | --- | --- |
| Simple: one core | Kino showed three features; "ten arguments, the jury remembers none" | One core, the Sony pocket-radio way: **"41 hours of lectures. The 40 seconds you need."** |
| Unexpected: break the schema, open a gap | Gives up, mascot rescues her: every ad | Seekino does not make Anon study more, it gives her weekend back; the ending reveals the film was made the same way |
| Concrete | An abstract word cloud of DL terms | Real lecture thumbnails, a crawling progress bar, one recurring time-range chip |
| Credible: testable credentials, Sinatra test | Product screens alone are a claim | The viewer has just watched the proof: 1.3 s of gritted teeth found in 4.7 h of anime |
| Emotional: one person, identity | "Learn efficiently" is a basement-level benefit | Anon's motive is not losing the spotlight in front of the band |
| Story: challenge plot | No stakes, no deadline | She has bragged she will explain GPT to the band on Monday |
| — | "Reads papers" | Dropped: Seekino indexes video only, and the promo shows only real capabilities |

## Approved script (about 60 s: Weekend intro → verse → first chorus)

The **time-range chip** is the single recurring visual: countdown, lecture range and anime sources all use it.

| Act | Music | Picture | Material the editor must find (MCP) |
| --- | --- | --- | --- |
| 1 The gap | Intro | Friday. Anon in glasses, posing, boasts to the band; P5 lettering **"Monday, I'll explain GPT to all of you."** Quick cuts of Taki (doubtful), Soyo (smiles), Tomori (stares), Rāna (doesn't care). Countdown chip `MON 09:00 · 62:00:00` | Anon in glasses, confident; one reaction per band member |
| 2 The wall | Verse | 28 real lecture thumbnails tile the screen, counter `41.5 h`; first lecture plays, `0:03 / 1:56:20`; Attention, Backprop, KV cache fall on the beat; Anon grits her teeth on the P5R step-out ladder; countdown keeps falling | Course thumbnails and durations; EP04 gritted teeth; P5R ladder exemplar |
| 3 The escape | Pre-chorus | Glasses off, she drops onto the sofa, picks up her phone. **"It's the weekend."** Beat drops out | Sofa fall; glasses removed |
| 4 One demo | Chorus drop | Kino swims out of the phone. Lying down, Anon types "What is attention, actually?" One real Seekino screen; the chip slams in with the returned lecture range and only that range plays. **"41 hours of lectures. The 40 seconds you need."** She sits up, glasses back on; Kino and Anon dance on the chorus | Real course search result and its source range; a screen recording of the app at native speed |
| 5 Gap closes, reveal | Chorus end | Monday: Anon explains attention to the band in front of the lecture clip; Tomori writes in her notebook, Taki is caught out; Anon smiles. Freeze. Calling card: **"Every shot of Anon in this film was found the same way."** Fast rewind with each shot's real source chip; "4.7 hours of anime. 1.3 seconds of gritted teeth." Logo; Kino curls up asleep | Band listening; Anon's smile; every shot's source range from `used.json` |

Out of scope for this cut: watch-QA, learning path, paper reading. Person search appears only by being shown in the reveal.

## Facts and placeholders

| Item | Value | Status |
| --- | --- | --- |
| Karpathy *Zero to Hero* + Stanford CS336 | 14.6 h + 26.9 h = 41.5 h (8 + 20 files, ffprobe on `assets/`) | Measured 2026-11-10 |
| MyGO 01–13 local files | 12 files, 4.7 h | Measured; the reveal claim needs all 12 searchable (5 published now) |
| Gritted teeth | EP04 `[943166, 944500)` ms, 1.3 s; half-body 941732 ms, front 943401 ms; smile from 944166 ms | Measured in [E4](editing-skill-style-reference-trd.md#e4-character-material-whole-episode-prompt-pilot) |
| Sofa fall | EP02 | Used in the 11-07 trial; range to be re-found by the editor |
| P5R step-out ladder | 33900–35033 ms, cut at 34333 ms | Measured |
| Lecture range and "40 seconds" | From a real Seekino search for attention | **Placeholder**; the line uses the real length |
| Countdown, progress-bar duration, thumbnail count | — | Placeholder until taken from the course library |
| Band reactions: mute or original audio | — | Owner to decide |

Every number on screen must come from the library or the editor's `used.json`; a measured value replaces its placeholder before export.

## Acceptance

Same gate as the [TRD §5](editing-skill-style-reference-trd.md#5-试点验证与方案取舍): one prompt; the editor finds each shot in the table above through MCP without being told episodes or timestamps; it chooses whether to use the P5R exemplar; every on-screen source chip resolves to the shot it labels; the owner accepts the film. Logged: recalls Seekino could not serve, tokens, wall time and owner interventions.
