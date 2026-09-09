#!/usr/bin/env python3
"""
Generates docs/cover.jpg — the artwork Apple Podcasts and Spotify show.

3000x3000, near-black with neon accents. Run once (or whenever the branding
changes); the result is committed to the repo, so this does not run in CI.

    python scripts/make_cover.py
"""

import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "cover.jpg")

SIZE = 3000
MARGIN = 200
INK = (10, 10, 10)
LIME = (204, 255, 0)
MAGENTA = (255, 45, 149)
WHITE = (245, 245, 245)
GREY = (128, 128, 128)

DISPLAY_FONTS = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
BODY_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def load_font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def ink_box(draw, text, font):
    """Bounding box of the actual marks, so layout is driven by pixels not font metrics."""
    return draw.textbbox((0, 0), text, font=font)


def draw_at(draw, left, top, text, font, fill):
    """Draw so the ink's top-left lands exactly on (left, top). Returns (width, height)."""
    box = ink_box(draw, text, font)
    draw.text((left - box[0], top - box[1]), text, font=font, fill=fill)
    return box[2] - box[0], box[3] - box[1]


def tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(c, font=font) for c in text) + tracking * max(len(text) - 1, 0)


def draw_tracked(draw, left, top, text, font, fill, tracking):
    """Letter-spaced text. Pillow has no tracking, so step glyph by glyph."""
    box = ink_box(draw, text, font)
    x, y = left, top - box[1]
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + tracking
    return box[3] - box[1]


def fit_tracked_font(draw, text, candidates, max_width, tracking, start=200):
    """Shrink until the letter-spaced string fits the available width."""
    size = start
    while size > 24:
        font = load_font(candidates, size)
        if tracked_width(draw, text, font, tracking) <= max_width:
            return font
        size -= 4
    return load_font(candidates, 24)


def speed_lines(region):
    """Diagonal neon streaks, painted on their own layer and masked into `region`."""
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    pen = ImageDraw.Draw(layer)
    for colour, top, thickness in [
        (MAGENTA + (255,), 620, 200),
        (LIME + (255,), 900, 90),
        (LIME + (70,), 1030, 26),
        (MAGENTA + (255,), 1320, 70),
        (LIME + (255,), 1480, 170),
        (LIME + (70,), 1720, 26),
    ]:
        pen.polygon(
            [(-400, top + 900), (SIZE + 400, top - 700),
             (SIZE + 400, top - 700 + thickness), (-400, top + 900 + thickness)],
            fill=colour,
        )
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rectangle(region, fill=255)
    layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", (SIZE, SIZE), 0), mask))
    return layer


def add_grain(img, strength=8):
    noise = Image.effect_noise((SIZE, SIZE), 44).convert("L").filter(ImageFilter.GaussianBlur(0.4))
    return Image.blend(img, Image.merge("RGB", (noise, noise, noise)), strength / 255)


def build():
    img = Image.new("RGB", (SIZE, SIZE), INK)
    draw = ImageDraw.Draw(img)

    title_font = load_font(DISPLAY_FONTS, 660)
    title_lines = [("THE", WHITE), ("FLAT", WHITE), ("SPOT", LIME)]
    title_top = 620
    cap_height = max(ink_box(draw, t, title_font)[3] - ink_box(draw, t, title_font)[1]
                     for t, _ in title_lines)
    line_step = cap_height + 64
    title_width = max(ink_box(draw, t, title_font)[2] - ink_box(draw, t, title_font)[0]
                      for t, _ in title_lines)
    title_bottom = title_top + line_step * 2 + cap_height

    # Streaks live to the right of the wordmark, so nothing crosses the type.
    gutter = MARGIN + title_width + 160
    streaks = speed_lines((gutter, 800, SIZE, title_bottom + 40))
    img.paste(streaks, (0, 0), streaks)
    draw = ImageDraw.Draw(img)

    # Magenta wedge, top-right corner.
    draw.polygon([(SIZE, 0), (SIZE, 560), (SIZE - 560, 0)], fill=MAGENTA)

    # Kicker.
    kicker_font = load_font(BODY_FONTS, 100)
    kicker_h = draw_tracked(draw, MARGIN, 360, "WEEKLY", kicker_font, LIME, 30)
    draw.rectangle([MARGIN, 360 + kicker_h + 46, MARGIN + 320, 360 + kicker_h + 74], fill=LIME)

    # Wordmark.
    y = title_top
    for text, colour in title_lines:
        draw_at(draw, MARGIN, y, text, title_font, colour)
        y += line_step

    # Full-bleed lime bar carrying the four sports.
    bar_top = title_bottom + 150
    bar_height = 240
    draw.rectangle([0, bar_top, SIZE, bar_top + bar_height], fill=LIME)
    strip = "SKATE  ·  MTB  ·  SNOW  ·  SURF"
    strip_font = fit_tracked_font(draw, strip, BODY_FONTS, SIZE - 2 * MARGIN, 18, start=150)
    strip_w = tracked_width(draw, strip, strip_font, 18)
    strip_h = ink_box(draw, strip, strip_font)[3] - ink_box(draw, strip, strip_font)[1]
    draw_tracked(draw, (SIZE - strip_w) / 2, bar_top + (bar_height - strip_h) / 2,
                 strip, strip_font, INK, 18)

    # Footer.
    foot = "THIRTY MINUTES.  FOUR SPORTS.  EVERY MONDAY."
    foot_font = fit_tracked_font(draw, foot, BODY_FONTS, SIZE - 2 * MARGIN, 10, start=90)
    foot_w = tracked_width(draw, foot, foot_font, 10)
    draw_tracked(draw, (SIZE - foot_w) / 2, bar_top + bar_height + 130, foot, foot_font, GREY, 10)

    img = add_grain(img)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    img.save(OUT_PATH, "JPEG", quality=92, optimize=True, progressive=True)
    print(f"Wrote {OUT_PATH} ({SIZE}x{SIZE}, {os.path.getsize(OUT_PATH) / 1024:.0f} KB)")


if __name__ == "__main__":
    build()
