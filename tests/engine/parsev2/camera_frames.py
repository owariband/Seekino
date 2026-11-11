"""Deterministic camera frames shared by parsing and worker tests."""

import math

from PIL import Image, ImageDraw


def panorama():
    image = Image.new("RGB", (792, 280), "#b4d5d9")
    d = ImageDraw.Draw(image)
    d.ellipse((381, 36, 423, 78), fill="#f7be7b")
    d.polygon(
        [
            (0, 133),
            (60, 107),
            (98, 119),
            (171, 90),
            (244, 127),
            (321, 93),
            (373, 110),
            (438, 87),
            (496, 121),
            (554, 103),
            (625, 121),
            (703, 89),
            (756, 110),
            (792, 98),
            (792, 182),
            (0, 182),
        ],
        fill="#739797",
    )
    d.rectangle((0, 156, 792, 280), fill="#447c8a")
    for y in range(165, 279, 13):
        for x in range((7 * y) % 53 - 20, 795, 66):
            d.line((x, y, x + 19, y - 1), fill="#84adb0", width=2)
    d.polygon(
        [(0, 155), (75, 144), (128, 150), (173, 164), (188, 184), (135, 180), (88, 194), (0, 199)],
        fill="#586362",
    )
    d.polygon([(73, 156), (82, 64), (103, 64), (116, 156)], fill="#fff0d1", outline="#243e44", width=2)
    d.polygon([(78, 112), (106, 106), (110, 125), (77, 133)], fill="#c74b39")
    d.rectangle((80, 52, 105, 66), fill="#edbc6b", outline="#2a4449", width=2)
    d.polygon([(75, 52), (92, 37), (110, 52)], fill="#344954")
    for x in (84, 91, 99):
        d.line((x, 53, x, 65), fill="#2a4449", width=2)
    for i, x in enumerate((210, 314, 448, 548, 645, 731)):
        y = 145 - (i % 3) * 4
        d.rectangle((x, y - 19, x + 29, y + 3), fill="#dfc79d", outline="#625d50")
        d.polygon([(x - 3, y - 18), (x + 10, y - 32), (x + 31, y - 20)], fill="#aa6047")
        d.rectangle((x + 8, y - 12, x + 13, y - 4), fill="#3e5d65")
    d.rectangle((525, 155, 790, 160), fill="#6a6758")
    for x in range(533, 792, 23):
        d.line((x, 159, x, 175), fill="#515b54", width=3)
    return image


def sample(background, case, i):
    u = i / 11
    progress = (1 - math.cos(math.pi * u)) / 2
    left = {
        "right": round(396 * progress),
        "left": round(396 * (1 - progress)),
        "subject": 0,
        "cut": 396 if i >= 6 else 0,
    }[case]
    image = background.copy()
    x = round(75 + 245 * u) if case == "subject" else 629
    d = ImageDraw.Draw(image)
    d.polygon([(x - 37, 216), (x + 35, 216), (x + 20, 230), (x - 23, 230)], fill="#b94f3f", outline="#313e44")
    d.rectangle((x - 11, 201, x + 19, 215), fill="#f7e2b7", outline="#405860")
    d.rectangle((x - 7, 204, x + 3, 210), fill="#496c73")
    d.line((x - 15, 215, x - 15, 177), fill="#384d52", width=2)
    d.polygon([(x - 14, 177), (x + 3, 180), (x - 14, 184)], fill="#c24d39")
    return image.crop((left, 0, left + 396, 280))
