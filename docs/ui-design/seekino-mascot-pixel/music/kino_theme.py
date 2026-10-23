"""Kino's Theme: an original chiptune sketch for the Seekino promo intro.

Mood: playful and a little silly, but bright, warm and laid-back. A punchy solo bass hook opens
cold (it has to land in the first second on a muted-autoplay feed), music-box bells bring the
warmth, and a jazzy I-vi-ii-V loop carries a bouncy piano melody over lazy Charleston chords.
Every sound is built from clean sine partials (no square waves). Everything is written for this
track; no melody or riff is taken from any reference.

104 BPM, 16th-note swing, F major, 26 bars plus a final hit (about 62 s). Bars 1-12 are the intro cut
on their own (the 30 s version ends on the bar 13 hit):
  bars 1-2   solo bass hook                    Kino asleep, Zs bounce on the hits
  bars 3-4   + kick, shaker, bells, soft piano Kino wakes, "boing" on the hop (bar 4 beat 3)
  bars 5-8   + piano melody A, snaps           Kino walks into town
  bars 9-12  melody A', a rising glockenspiel  one feature sign pops per bar
             "ding" on every downbeat,
             a snap fill + slide whistle,
             then a bass-only break
  bar 13     soft "play" hit                   footage starts
  bars 13-20 B section: laid-back IV-iii-ii-V,  a quiet bed under screen footage; a "boing" at
             sparse piano, music-box replies,   bar 16 beat 4, the band thickens from bar 17
             walking bass, light drums
  bars 21-24 the main melody returns, a ding    recap beats
             on each downbeat
  bars 25-26 the turn home and the bass break
  bar 27     "ta-da" hit (60.0 s)               logo / end card

Pure Python standard library, deterministic output. Run:
  python3 kino_theme.py [out.wav]
"""

import math
import random
import struct
import sys
import wave

SR = 44100
BPM = 104
BEAT = 60 / BPM
SWING = 0.58  # a 16th offbeat sits at 58% of its 8th: a light, bouncy shuffle
BARS = 26

NOTE_INDEX = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def midi(name: str) -> int:
    letter, rest = name[0], name[1:]
    shift = 0
    while rest and rest[0] in "#b":
        shift += 1 if rest[0] == "#" else -1
        rest = rest[1:]
    return 12 * (int(rest) + 1) + NOTE_INDEX[letter] + shift


def hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def at(bar: int, beat: float) -> float:
    """Seconds for a position in the score (bars from 1), with 16th-note swing applied."""
    total = (bar - 1) * 4 + beat
    quarter = math.floor(total + 1e-9)
    frac = total - quarter
    eighth = math.floor(frac * 2 + 1e-9) / 2
    g = (frac - eighth) * 2  # position inside the 8th, 0..1
    g = g * 2 * SWING if g < 0.5 else SWING + (g - 0.5) * 2 * (1 - SWING)
    return (quarter + eighth + g / 2) * BEAT


TAIL = 2.2
LENGTH = at(BARS + 1, 0) + TAIL
N = int(LENGTH * SR) + 1
mix = [0.0] * N
send = [0.0] * N  # echo send for lead and bells
rng = random.Random(7)


def add(buf, start: float, samples):
    i0 = int(start * SR)
    for k, v in enumerate(samples):
        i = i0 + k
        if 0 <= i < N:
            buf[i] += v


# ---------------------------------------------------------------- instruments
# Clean, warm timbres built from sine partials: no square waves, so nothing buzzes.

def piano(start, dur, note, amp=0.24, vel=0.8):
    """Additive piano: stretched partials, a two-stage decay that dims the highs first, a felt hammer
    tick, a detuned string pair for warmth, and a damper when the key lifts."""
    f = hz(midi(note))
    tau = min(2.4, 1.3 * (262 / f) ** 0.5)
    partials = []
    for n in range(1, 9):
        fn = n * f * math.sqrt(1 + 0.0004 * n * n)
        if fn > 8000:
            break
        a = vel ** (0.4 * (n - 1)) / n ** 1.25  # softer notes are darker
        t_n = tau / (1 + 0.8 * (n - 1))
        partials.append([2 * math.pi * fn / SR, a, math.exp(-1 / (SR * t_n * 0.25)), math.exp(-1 / (SR * t_n)), 0.55, 0.45])
    norm = sum(p[1] for p in partials)
    pair = 2 * math.pi * f * 2 ** (1.5 / 1200) / SR
    damper = math.exp(-1 / (SR * 0.09))
    n_samples = int((dur + 0.5) * SR)
    out, lp, held = [], 0.0, 1.0
    for k in range(n_samples):
        t = k / SR
        x = 0.0
        for p in partials:
            x += p[1] * (p[4] + p[5]) * math.sin(p[0] * k)
            p[4] *= p[2]
            p[5] *= p[3]
        x += 0.45 * partials[0][1] * (partials[0][4] + partials[0][5]) * math.sin(pair * k)
        if t < 0.008:  # felt hammer
            lp += 0.15 * (rng.uniform(-1, 1) - lp)
            x += lp * 0.25 * vel * (1 - t / 0.008)
        if t > dur:
            held *= damper
        out.append(x / norm * amp * vel * held * min(1.0, t / 0.0015))
    add(mix, start, out)
    add(send, start, [v * 0.35 for v in out])


def bass(start, dur, note, amp=0.42, ghost=False, slide_to=None):
    """Round finger bass: mostly fundamental, a short bright pop on the pluck so the hook still punches."""
    f0, f1 = hz(midi(note)), hz(midi(slide_to)) if slide_to else None
    if ghost:
        amp, dur = amp * 0.3, min(dur, 0.05)
    pop = 1.6 if midi(note) >= midi("C3") else 1.0
    n = int((dur + 0.04) * SR)
    out, phase = [], 0.0
    for k in range(n):
        t = k / SR
        f = f0 if f1 is None else f0 * (f1 / f0) ** min(1.0, t / dur)
        phase += f * (1 + 0.025 * math.exp(-t / 0.01)) / SR
        w = 2 * math.pi * phase
        x = math.sin(w) + 0.45 * math.sin(2 * w) + 0.14 * math.sin(3 * w)
        x += pop * (0.22 * math.sin(4 * w) + 0.12 * math.sin(5 * w) + 0.06 * math.sin(6 * w)) * math.exp(-t / 0.035)
        x = math.tanh(0.9 * x)
        env = min(1.0, t / 0.004) * math.exp(-t / 0.8)
        if t > dur:
            env *= max(0.0, 1 - (t - dur) / 0.04)
        out.append(x * env * amp)
    add(mix, start, out)


def bell(start, note, amp=0.12, decay=0.55):
    """Music-box tine: a sine with a soft octave partial and a quick decay."""
    f = hz(midi(note))
    n = int(decay * 5 * SR)
    out = []
    for k in range(n):
        t = k / SR
        x = math.sin(2 * math.pi * f * t) + 0.25 * math.sin(2 * math.pi * 2 * f * t) * math.exp(-t / 0.12)
        out.append(x * amp * min(1.0, t / 0.002) * math.exp(-t / decay))
    add(mix, start, out)
    add(send, start, [v * 0.8 for v in out])


def kick(start, amp=0.45):
    out, phase = [], 0.0
    for k in range(int(0.28 * SR)):
        t = k / SR
        phase += (55 + 90 * math.exp(-t / 0.025)) / SR
        out.append(math.sin(2 * math.pi * phase) * amp * math.exp(-t / 0.075))
    add(mix, start, out)


def shaker(start, amp=0.035):
    """Soft shaker in place of a hi-hat: band-limited noise with a gentle attack."""
    out, lo, hi = [], 0.0, 0.0
    for k in range(int(0.09 * SR)):
        t = k / SR
        x = rng.uniform(-1, 1)
        lo += 0.35 * (x - lo)
        hi += 0.04 * (lo - hi)
        out.append((lo - hi) * amp * min(1.0, t / 0.006) * math.exp(-t / 0.028))
    add(mix, start, out)


def snap(start, amp=0.11):
    """Finger snap: a tiny click and a short burst of mid-band noise."""
    out, lo, hi = [], 0.0, 0.0
    for k in range(int(0.12 * SR)):
        t = k / SR
        lo += 0.3 * (rng.uniform(-1, 1) - lo)
        hi += 0.08 * (lo - hi)
        click = math.sin(2 * math.pi * 1800 * t) * math.exp(-t / 0.003)
        out.append(((lo - hi) * math.exp(-t / 0.022) + 0.5 * click) * amp)
    add(mix, start, out)


def cymbal(start, amp=0.03):
    out, lo, hi = [], 0.0, 0.0
    for k in range(int(1.6 * SR)):
        t = k / SR
        lo += 0.4 * (rng.uniform(-1, 1) - lo)
        hi += 0.1 * (lo - hi)
        out.append((lo - hi) * amp * min(1.0, t / 0.01) * math.exp(-t / 0.5))
    add(mix, start, out)


def glock(start, note, amp=0.11):
    """Glockenspiel "ding" for each feature sign."""
    f = hz(midi(note))
    out = []
    for k in range(int(0.9 * SR)):
        t = k / SR
        x = math.sin(2 * math.pi * f * t) + 0.25 * math.sin(2 * math.pi * 2.76 * f * t) * math.exp(-t / 0.07)
        out.append(x * amp * min(1.0, t / 0.001) * math.exp(-t / 0.3))
    add(mix, start, out)
    add(send, start, [v * 0.5 for v in out])


def boing(start, amp=0.16):
    """Cartoon spring for Kino's hop: a rising, wobbling sine."""
    out, phase = [], 0.0
    for k in range(int(0.42 * SR)):
        t = k / SR
        f = 180 * 2 ** (t * 3.2) * (1 + 0.12 * math.sin(2 * math.pi * 16 * t) * math.exp(-t / 0.2))
        phase += f / SR
        out.append(math.sin(2 * math.pi * phase) * amp * min(1.0, t / 0.004) * math.exp(-t / 0.16))
    add(mix, start, out)


def slide_whistle(start, dur, lo, hi, amp=0.08):
    f0, f1 = hz(midi(lo)), hz(midi(hi))
    out, phase = [], 0.0
    n = int(dur * SR)
    for k in range(n):
        t = k / SR
        f = f0 * (f1 / f0) ** (t / dur) * (1 + 0.012 * math.sin(2 * math.pi * 6 * t))
        phase += f / SR
        env = min(1.0, t / 0.03) * min(1.0, (dur - t) / 0.04)
        out.append(math.sin(2 * math.pi * phase) * amp * env)
    add(mix, start, out)


# ---------------------------------------------------------------- score

def play_bass_bar(bar, hits):
    """`hits`: (sixteenth, length in 16ths, note[, "g" ghost | "s:NOTE" slide])."""
    for hit in hits:
        s16, len16, note = hit[:3]
        kind = hit[3] if len(hit) > 3 else ""
        start = at(bar, s16 / 4)
        dur = (at(bar, (s16 + len16) / 4) - start) * 0.8
        bass(start, dur, note, ghost=kind == "g", slide_to=kind[2:] if kind.startswith("s:") else None)


HOOK_F = [(0, 2, "F2"), (3, 1, "F2", "g"), (4, 1, "F3"), (6, 1, "Eb3"), (8, 2, "C3"),
          (10, 1, "F2"), (11, 1, "F2", "g"), (12, 2, "Ab2", "s:A2"), (14, 1, "C3")]
HOOK_BB = [(0, 2, "Bb1"), (3, 1, "Bb1", "g"), (4, 1, "Bb2"), (6, 1, "Ab2"), (8, 1, "F2"), (9, 1, "F2", "g"),
           (10, 1, "D2"), (12, 1, "C2"), (13, 1, "B1")]

play_bass_bar(1, HOOK_F)
play_bass_bar(2, HOOK_BB + [(14, 2, "C3", "s:C2")])  # goofy fall
play_bass_bar(3, HOOK_F)
play_bass_bar(4, HOOK_BB + [(14, 2, "C2", "s:C3")])  # springs back up into the tune

GROOVE = {
    5: [(0, 2, "F2"), (3, 1, "F2", "g"), (4, 1, "F3"), (6, 1, "C3"), (8, 2, "F2"), (10, 1, "A2"), (12, 1, "C3"), (14, 1, "C#2")],
    6: [(0, 2, "D2"), (3, 1, "D2", "g"), (4, 1, "D3"), (6, 1, "C3"), (8, 2, "A2"), (10, 1, "F2"), (12, 1, "A2"), (14, 1, "F#2")],
    7: [(0, 2, "G2"), (3, 1, "G2", "g"), (4, 1, "G3"), (6, 1, "F3"), (8, 2, "D3"), (10, 1, "Bb2"), (12, 1, "D3"), (14, 1, "Db3")],
    8: [(0, 2, "C3"), (3, 1, "C3", "g"), (4, 1, "C2"), (6, 1, "Bb2"), (8, 2, "G2"), (10, 1, "E2"), (12, 2, "G2", "s:E2"), (14, 1, "E2")],
    9: [(0, 2, "F2"), (3, 1, "F2", "g"), (4, 1, "F3"), (6, 1, "C3"), (8, 2, "F2"), (10, 1, "A2"), (12, 1, "C3"), (14, 1, "E2")],
    10: [(0, 2, "F2"), (3, 1, "F2", "g"), (4, 1, "F3"), (6, 1, "Eb3"), (8, 2, "C3"), (10, 1, "A2"), (12, 1, "C3"), (14, 1, "A2")],
    11: [(0, 2, "Bb1"), (3, 1, "Bb1", "g"), (4, 1, "Bb2"), (6, 1, "F2"), (8, 2, "Bb1"), (10, 1, "Db2"), (12, 1, "F2"), (14, 1, "B1")],
    # Beats 1-3 keep the groove; then everything drops out and the bass quotes the hook before the hit.
    12: [(0, 2, "C2"), (3, 1, "C2", "g"), (4, 1, "C3"), (6, 1, "A2"), (8, 2, "C2"), (10, 1, "E2"),
         (12, 1, "F2"), (13, 1, "F2", "g"), (14, 1, "F3"), (15, 1, "Eb3")],
}
for bar, hits in GROOVE.items():
    play_bass_bar(bar, hits)

# Music-box bells: warmth arrives as Kino wakes.
for bar, line in {
    3: [(0, "F4"), (0.5, "A4"), (1, "C5"), (1.5, "E5"), (2, "A5"), (3.5, "G5")],
    4: [(0, "Bb5"), (0.5, "G5"), (1, "E5"), (1.5, "C5")],
}.items():
    for beat, note in line:
        bell(at(bar, beat), note)
boing(at(4, 2))  # Kino's hop

# Melody: (bar, beat, beats, note[, bend]).
MELODY = [
    (4, 3, 0.5, "A4"), (4, 3.5, 0.5, "Bb4"),
    (5, 0, 0.5, "C5"), (5, 0.5, 0.5, "A4"), (5, 1, 0.5, "C5"), (5, 1.5, 1.25, "F5"), (5, 3, 0.5, "E5"), (5, 3.5, 0.5, "F5"),
    (6, 0, 0.75, "A5"), (6, 1, 0.5, "G5"), (6, 1.5, 0.5, "F5"), (6, 2, 1.25, "D5"), (6, 3.5, 0.5, "C5"),
    (7, 0, 0.5, "Bb4"), (7, 0.5, 0.5, "D5"), (7, 1, 0.5, "G5"), (7, 1.5, 0.5, "F5"), (7, 2, 1.0, "D5"), (7, 3, 0.5, "Bb4"), (7, 3.5, 0.5, "C5"),
    (8, 0, 0.5, "E5"), (8, 0.5, 0.5, "G5"), (8, 1, 0.25, "B5"), (8, 1.25, 1.25, "Bb5"), (8, 3, 0.5, "G5"), (8, 3.5, 0.5, "E5"),
    (9, 0, 0.5, "F5"), (9, 0.5, 0.5, "A5"), (9, 1, 0.5, "C6"), (9, 1.5, 0.5, "A5"), (9, 2, 1.0, "F5"), (9, 3, 0.5, "G5"), (9, 3.5, 0.5, "A5"),
    (10, 0, 0.5, "Bb5"), (10, 0.5, 0.5, "A5"), (10, 1, 0.5, "G5"), (10, 1.5, 0.5, "F5"), (10, 2, 1.0, "Eb5"), (10, 3, 0.5, "C5"), (10, 3.5, 0.5, "D5"),
    (11, 0, 1.0, "D5"), (11, 1, 0.5, "F5"), (11, 1.5, 0.5, "D5"), (11, 2, 1.0, "Db5"), (11, 3, 0.5, "C5"), (11, 3.5, 0.5, "Bb4"),
    (12, 0, 1.0, "A4"), (12, 1, 0.5, "C5"), (12, 1.5, 0.5, "F5"), (12, 2, 0.5, "E5"), (12, 2.5, 0.5, "G5"),
]
for bar, beat, beats, note in MELODY:
    start = at(bar, beat)
    # Notes on the beat lean in a little; the swung offbeats stay light and bouncy.
    vel = 0.9 if beat % 1 == 0 else 0.72
    piano(start, (at(bar, beat + beats) - start) * 0.9, note, vel=vel)
    if bar >= 9:  # the reprise gets a music-box double an octave up
        bell(start, note[:-1] + str(int(note[-1]) + 1), amp=0.04, decay=0.35)

# Left-hand piano chords on a lazy Charleston rhythm (beat 1, then the "and" of 2).
COMP = {
    3: ["A3", "C4", "E4"], 4: ["G3", "Bb3", "E4"],
    5: ["A3", "C4", "E4"], 6: ["A3", "C4", "F4"], 7: ["Bb3", "D4", "F4"], 8: ["Bb3", "E4", "G4"],
    9: ["A3", "C4", "E4"], 10: ["A3", "Eb4", "G4"],
}
for bar, chord in COMP.items():
    soft = 0.55 if bar < 5 else 0.7
    for beat, beats in ((0, 0.9), (1.5, 0.45)):
        start = at(bar, beat)
        for note in chord:
            piano(start, (at(bar, beat + beats) - start), note, amp=0.12, vel=soft)
# Two chords a bar as the phrase turns home: Bb then Bbm, then F/C then C7 just before the break.
for bar, beat, beats, chord in [(11, 0, 1.4, ["Bb3", "D4", "F4"]), (11, 2, 1.4, ["Bb3", "Db4", "F4"]),
                                (12, 0, 1.4, ["A3", "C4", "F4"]), (12, 2, 0.45, ["Bb3", "E4", "G4"])]:
    start = at(bar, beat)
    for note in chord:
        piano(start, at(bar, beat + beats) - start, note, amp=0.12, vel=0.7)

# Drums.
for bar in range(3, 13):
    full = bar >= 5
    for beat in range(4):
        if bar == 12 and beat >= 2:
            break
        if beat in (0, 2) or (full and beat == 1 and bar % 2 == 0):
            kick(at(bar, beat + (0.75 if beat == 1 else 0)), amp=0.42 if full else 0.3)
        shaker(at(bar, beat + 0.5), amp=0.035 if full else 0.025)
        if full:
            shaker(at(bar, beat + 0.75), amp=0.018)
        if full and beat in (1, 3):
            snap(at(bar, beat), amp=0.1 if bar < 9 else 0.13)
# Bar 12: a rising snare fill, a slide whistle, then the bass-only break.
for i, beat in enumerate([2.0, 2.25, 2.5, 2.75]):
    snap(at(12, beat), amp=0.06 + 0.025 * i)
slide_whistle(at(12, 2), at(12, 3) - at(12, 2), "C5", "C6")

# One sign pops on each downbeat of bars 9-12.
for bar, note in zip(range(9, 13), ["C6", "D6", "E6", "F6"]):
    glock(at(bar, 0), note)

# ---------------------------------------------------------------- bars 13-20: B section, a bed for footage

PLAY = at(13, 0)
kick(PLAY, 0.42)
cymbal(PLAY, 0.022)
glock(PLAY, "F6", amp=0.08)
for note in ["D4", "F4", "A4", "C5"]:
    piano(PLAY, 1.2, note, amp=0.1, vel=0.8)

# Walking quarters; rootless piano voicings above. Bbmaj7 Am7 Gm7 C7 | Bbmaj7 Am7 Dm7-G7 Gm7-C7
B_BASS = {
    13: ["Bb1", "D2", "F2", "A2"], 14: ["A1", "C2", "E2", "G2"], 15: ["G1", "Bb1", "D2", "F2"], 16: ["C2", "E2", "G2", "Bb2"],
    17: ["Bb1", "D2", "F2", "A2"], 18: ["A1", "C2", "E2", "C#2"], 19: ["D2", "A1", "G1", "B1"], 20: ["G1", "D2", "C2", "C2"],
}
for bar, notes in B_BASS.items():
    hits = [(4 * i, 4, note) for i, note in enumerate(notes)]
    if bar == 20:  # springs back up into the theme, like bar 4
        hits[-1] = (12, 4, "C2", "s:C3")
    play_bass_bar(bar, hits)

B_CHORDS = {
    13: [["D4", "F4", "A4", "C5"]], 14: [["C4", "E4", "G4"]], 15: [["Bb3", "D4", "F4", "A4"]], 16: [["Bb3", "E4", "G4"]],
    17: [["D4", "F4", "A4", "C5"]], 18: [["C4", "E4", "G4"]], 19: [["C4", "F4", "A4"], ["B3", "F4", "G4"]],
    20: [["Bb3", "D4", "F4"], ["Bb3", "E4", "G4"]],
}
for bar, chords in B_CHORDS.items():
    # Lazier than the Charleston: a long chord on 1, a soft push on the "and" of 3 (or a second chord on 3).
    hits = [(0, 1.8, chords[0]), (2.5, 0.9, chords[0])] if len(chords) == 1 else [(0, 1.8, chords[0]), (2, 1.8, chords[1])]
    for beat, beats, chord in hits:
        if bar == 13 and beat == 0:
            continue  # the play hit already sounds this chord
        start = at(bar, beat)
        for note in chord:
            piano(start, at(bar, beat + beats) - start, note, amp=0.1, vel=0.6)

MELODY_B = [
    (13, 0.5, 0.5, "D5"), (13, 1, 0.5, "F5"), (13, 1.5, 1.5, "A5"),
    (14, 0, 0.75, "G5"), (14, 1, 0.5, "E5"), (14, 1.5, 1.5, "C5"),
    (15, 0.5, 0.5, "Bb4"), (15, 1, 0.5, "D5"), (15, 1.5, 0.5, "F5"), (15, 2, 1.0, "A5"), (15, 3, 0.5, "G5"), (15, 3.5, 0.5, "F5"),
    (16, 0, 1.0, "E5"), (16, 1, 0.5, "G5"), (16, 1.5, 0.5, "Bb5"), (16, 2, 0.5, "A5"), (16, 2.5, 0.5, "G5"),
    (17, 0.5, 0.5, "F5"), (17, 1, 0.5, "A5"), (17, 1.5, 1.5, "D6"),
    (18, 0, 0.75, "C6"), (18, 1, 0.5, "A5"), (18, 1.5, 1.5, "E5"),
    (19, 0, 0.5, "F5"), (19, 0.5, 0.5, "E5"), (19, 1, 1.0, "D5"), (19, 2, 0.5, "B4"), (19, 2.5, 0.5, "D5"), (19, 3, 1.0, "F5"),
    (20, 0, 0.5, "Bb4"), (20, 0.5, 0.5, "D5"), (20, 1, 0.5, "F5"), (20, 1.5, 0.5, "E5"), (20, 2, 1.0, "G5"), (20, 3, 0.5, "A4"), (20, 3.5, 0.5, "Bb4"),
]
for bar, beat, beats, note in MELODY_B:
    start = at(bar, beat)
    piano(start, (at(bar, beat + beats) - start) * 0.9, note, vel=0.78 if beat % 1 == 0 else 0.62)
# Music-box replies in the gaps the melody leaves.
for bar, beat, note in [(13, 3, "C6"), (13, 3.5, "A5"), (14, 3, "G5"), (14, 3.5, "E5"),
                        (17, 3, "F6"), (17, 3.5, "D6"), (18, 3, "C6"), (18, 3.5, "G5")]:
    bell(at(bar, beat), note, amp=0.08)
boing(at(16, 3))  # a cheeky moment for Kino mid-footage

for bar in range(13, 21):
    busy = bar >= 17
    for beat in range(4):
        if beat in (0, 2) and not (bar == 13 and beat == 0):
            kick(at(bar, beat), amp=0.3)
        shaker(at(bar, beat + 0.5), amp=0.022)
        if busy:
            shaker(at(bar, beat + 0.75), amp=0.012)
            if beat in (1, 3) and not (bar == 20 and beat == 3):
                snap(at(bar, beat), amp=0.07)
for i, beat in enumerate([3.0, 3.25, 3.5, 3.75]):  # a little fill back into the theme
    snap(at(20, beat), amp=0.05 + 0.02 * i)

# ---------------------------------------------------------------- bars 21-26: the theme returns, then home


def copy_bar(src, dst):
    """Replays one bar of the A section (bass, melody with a music-box double, chords, drums) at another bar."""
    if src in GROOVE:
        hits = GROOVE[src]
        if src == 8:  # lead into Bb (bar 25) by a half step instead of into F
            hits = hits[:-1] + [(14, 1, "B1")]
        play_bass_bar(dst, hits)
    for bar, beat, beats, note in MELODY:
        if bar == src:
            start = at(dst, beat)
            piano(start, (at(dst, beat + beats) - start) * 0.9, note, vel=0.9 if beat % 1 == 0 else 0.72)
            bell(start, note[:-1] + str(int(note[-1]) + 1), amp=0.04, decay=0.35)
    if src in COMP:
        for beat, beats in ((0, 0.9), (1.5, 0.45)):
            start = at(dst, beat)
            for note in COMP[src]:
                piano(start, at(dst, beat + beats) - start, note, amp=0.12, vel=0.7)
    for beat in range(4):
        if src == 12 and beat >= 2:
            break
        if beat in (0, 2) or (beat == 1 and dst % 2 == 0):
            kick(at(dst, beat + (0.75 if beat == 1 else 0)), amp=0.42)
        shaker(at(dst, beat + 0.5), amp=0.035)
        shaker(at(dst, beat + 0.75), amp=0.018)
        if beat in (1, 3):
            snap(at(dst, beat), amp=0.13)


for src, dst in [(5, 21), (6, 22), (7, 23), (8, 24), (11, 25), (12, 26)]:
    copy_bar(src, dst)
# Bars 25-26 carry the two-chords-a-bar turn home, like bars 11-12.
for bar, beat, beats, chord in [(25, 0, 1.4, ["Bb3", "D4", "F4"]), (25, 2, 1.4, ["Bb3", "Db4", "F4"]),
                                (26, 0, 1.4, ["A3", "C4", "F4"]), (26, 2, 0.45, ["Bb3", "E4", "G4"])]:
    start = at(bar, beat)
    for note in chord:
        piano(start, at(bar, beat + beats) - start, note, amp=0.12, vel=0.7)
for bar, note in zip(range(21, 25), ["C6", "D6", "E6", "F6"]):
    glock(at(bar, 0), note, amp=0.09)
for i, beat in enumerate([2.0, 2.25, 2.5, 2.75]):
    snap(at(26, beat), amp=0.06 + 0.025 * i)
slide_whistle(at(26, 2), at(26, 3) - at(26, 2), "C5", "C6")

# Bar 27: ta-da!
hit = at(27, 0)
kick(hit, 0.5)
snap(hit, 0.14)
cymbal(hit)
bass(hit, 0.9, "F2", amp=0.45)
for note in ["F3", "A3", "C4", "F4", "A4", "C5", "F5"]:
    piano(hit, 1.1, note, amp=0.11, vel=0.85)
bell(hit, "F6", amp=0.1, decay=0.8)

# ---------------------------------------------------------------- mix

delay = int(0.75 * BEAT * SR)  # dotted-8th echo
echo = [0.0] * N
for i in range(N):
    echo[i] = send[i] + (echo[i - delay] * 0.3 if i >= delay else 0.0)
master = [mix[i] + 0.22 * (echo[i - delay] if i >= delay else 0.0) for i in range(N)]
# Low-cut below ~45 Hz: phone speakers cannot play it and it only eats headroom.
for _ in range(2):
    a = math.exp(-2 * math.pi * 45 / SR)
    prev_x = prev_y = 0.0
    for i in range(N):
        y = a * (prev_y + master[i] - prev_x)
        prev_x, prev_y = master[i], y
        master[i] = y
# Gentle top-end smoothing (about 9 kHz) so nothing fizzes, then a soft clip.
a = 1 - math.exp(-2 * math.pi * 9000 / SR)
y = 0.0
for i in range(N):
    y += a * (master[i] - y)
    master[i] = y
master = [math.tanh(1.2 * x) / math.tanh(1.2) for x in master]
peak = max(abs(x) for x in master) or 1.0
gain = 0.89 / peak
fade = int(0.4 * SR)
for i in range(fade):
    master[N - 1 - i] *= i / fade

out_path = sys.argv[1] if len(sys.argv) > 1 else "kino-theme.wav"
with wave.open(out_path, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x * gain)) * 32767)) for x in master))

print(f"wrote {out_path}: {LENGTH:.2f}s at {BPM} BPM")
print("bar starts (s): " + "  ".join(f"{b}:{at(b, 0):.2f}" for b in range(1, BARS + 2)))
print(f"boing: {at(4, 2):.2f}s, {at(16, 3):.2f}s   play hit: {PLAY:.2f}s   ta-da: {hit:.2f}s")
