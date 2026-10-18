# Seekino promo: "Kino's Theme" and the 60 s video

Source for the Seekino promo. Everything is generated from code: the music is synthesized note by note, and every video frame is drawn from the same Kino engine the web app uses (`web/src/shared/kino/engine.ts`). Nothing is recorded or hand-animated, so a render is identical every time and any timing can be changed in code.

The video is 1080×1350 (4:5, for the X feed), 30 fps, 62.2 s. Kino walks a dashed ground line, collects "moment" tiles, headbutts a ▶ block, and a projector screen drops down behind it to show Seekino. The Undertale-style text box talks to the viewer, so the video still works with the sound off.

## Render

From the repository root:

```bash
./assets/promo/render.sh
```

Outputs go to `assets/promo/out/` (git-ignored):

| File | What |
| --- | --- |
| `kino-theme-60s.wav` / `.mp3` | Kino's Theme, 62.2 s |
| `kino-promo-60s.mp4` | the 60 s promo with the theme and typewriter blips |

`./assets/promo/render.sh --explorations` also renders the earlier directions into `out/explorations/`.

Requirements: Python 3 (standard library only), Node.js 24+ (it imports the TypeScript engine directly), ffmpeg, and the Playwright Chromium that `web/` already installs (`npm --prefix web exec -- playwright install chromium`). Chromium only typesets the text; the frames themselves are drawn in Node.

## Files

| Path | Role |
| --- | --- |
| `music/kino_theme.py` | Kino's Theme: an original piece, 104 BPM with a light 16th-note swing, in F major. Clean additive piano, a round finger bass, music-box bells, glockenspiel, soft shaker/snap drums, and a few cartoon effects (boing, slide whistle). |
| `video/promo-60s.mjs` | The 60 s promo renderer. Pixel art on a 135×169 grid scaled 8×; footage, text and the wordmark are composited at full resolution. Kino sits on its own foreground layer so it stands in front of the screen. |
| `video/fonts/` | Inter 750 subset for the "Seekino" wordmark (SIL OFL, license included). |
| `explorations/snowdin-town-intro.mjs` | Earlier direction: Kino walks through a snowy town past four shops (30 s). |
| `explorations/battle-box-style-frames.mjs` | Earlier direction: the Undertale battle box as the screen, with a stats row and SEARCH / ASK / WATCH / SAVE buttons (three stills). |
| `render.sh` | Renders the theme, then the video. |

## Timing

The music and the video share one clock: `at(bar, beat)` in both scripts applies the same swing, so story beats are written in bars, not seconds. One bar is about 2.31 s.

| Bars | Time | Music | Video |
| --- | --- | --- | --- |
| 1–2 | 0.0–4.6 s | solo bass hook | Kino asleep on the line; its back bumps on each bass hit. *"You remember a scene… but not where it was."* |
| 3–4 | 4.6–9.2 s | bells, soft drums; boing at 8.08 s | Kino wakes and hops on the boing. *"Kino will find it for you!"* |
| 5–8 | 9.2–18.5 s | main melody | Kino walks; the ground and the drifting moment tiles scroll. |
| 9–12 | 18.5–27.7 s | melody reprise; a ding on each downbeat | Kino hops to collect a tile on each ding, one tip per tile; the ▶ block appears; Kino stops and crouches on the bass-only break. |
| 13 | 27.7 s | soft "play" hit | Kino headbutts the block; the screen drops with a bounce. |
| 13–20 | 27.7–46.2 s | laid-back B section; boing at 36.35 s | Footage: watch + chat, then search results, resolving out of a mosaic with a slow push-in. |
| 21–24 | 46.2–55.4 s | main melody returns; four dings | Recap: one screen per ding; *"Watch. Ask. Find. Check."* |
| 25–26 | 55.4–60.0 s | turn home, bass break | The screen rolls up; *"Kino is getting sleepy…"* |
| 27 | 60.0 s | ta-da | The wordmark drops in; Kino jumps, then curls up and sleeps. |

The first 12 bars also work as a 30 s cut on their own.

## Before publishing

- **Footage:** the screenshots in `assets/screenshots/` show MyGO anime frames. Use videos you're allowed to show for a public post. Replace the files or point the `SHOTS` table in `video/promo-60s.mjs` at your own screenshots or recordings.
- **Music:** the theme is written for this project and does not use any existing melody, so it can be used freely in the promo.
- **Copy:** captions and the end-card tagline live in the `SCRIPT` array and the `tagline` line of `video/promo-60s.mjs`. Keep claims to what Seekino actually does.
