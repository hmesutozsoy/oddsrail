#!/usr/bin/env python3
"""Draw fixed educational strategy examples, never market data or forecasts.

Usage:
    python tools/generate_strategy_gifs.py

Requires Pillow. Outputs real looping GIFs and complete static PNG posters.
No random inputs, network calls, or changes to trading configuration are used.
The page must label these as illustrative examples independent of its settings.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, SCALE = 720, 260, 2
TEXT_SCALE, MIN_TEXT_SIZE = 0.54, 15
FRAMES = 60
DURATIONS = [70, 70, 60] * 20  # GIF timing uses 10 ms units: exactly four seconds.
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "site" / "assets" / "strategy-gifs"
THEMES = {
    "dark": {"bg": "#18191b", "text": "#f4f2ed", "muted": "#98999e",
             "line": "#45464b", "accent": "#bda7ff"},
    "light": {"bg": "#f0efea", "text": "#212225", "muted": "#69696f",
              "line": "#c6c4ce", "accent": "#7652ce"},
}
FONT_CANDIDATES = [
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
]


def rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def blend(a, b, amount):
    return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


def ease(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def progress(phase):
    """Read an existing diagram from left to right, then gently clear its accent."""
    return ease((phase - 0.08) / 0.62)


def accent_strength(phase):
    return 1 - ease((phase - 0.83) / 0.17)


class Canvas:
    def __init__(self, theme, regular_font, bold_font):
        self.colors = {name: rgb(value) for name, value in theme.items()}
        self.image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), self.colors["bg"])
        self.draw = ImageDraw.Draw(self.image)
        self.regular_font, self.bold_font = regular_font, bold_font
        self.fonts = {}

    def color(self, name, strength=1):
        return blend(self.colors["bg"], self.colors[name], strength)

    def text(self, xy, value, size=28, color="text", anchor="la", bold=False, strength=1):
        # Smaller diagram typography, with a floor for compact numeric labels.
        size = max(MIN_TEXT_SIZE, round(size * TEXT_SCALE))
        key = (size, bold)
        if key not in self.fonts:
            self.fonts[key] = ImageFont.truetype(
                str(self.bold_font if bold else self.regular_font), round(size * SCALE)
            )
        self.draw.text(tuple(round(v * SCALE) for v in xy), value, font=self.fonts[key],
                       fill=self.color(color, strength), anchor=anchor)

    def line(self, points, color="line", width=2, strength=1):
        self.draw.line([(round(x * SCALE), round(y * SCALE)) for x, y in points],
                       fill=self.color(color, strength), width=max(1, round(width * SCALE)),
                       joint="curve")

    def circle(self, xy, radius, color="accent", strength=1, outline=False, width=2):
        x, y = xy
        box = tuple(round(v * SCALE) for v in (x - radius, y - radius, x + radius, y + radius))
        self.draw.ellipse(box, fill=None if outline else self.color(color, strength),
                          outline=self.color(color, strength) if outline else None,
                          width=round(width * SCALE))

    def rounded(self, box, radius=8, color="accent", strength=1):
        self.draw.rounded_rectangle(tuple(round(v * SCALE) for v in box),
                                    radius=round(radius * SCALE), fill=self.color(color, strength))

    def dot(self, xy, phase=0, strength=1, radius=5):
        # Small local halo only. Background and labels never flash or move.
        self.circle(xy, radius + 6 + math.sin(phase * math.tau) * 1.5,
                    strength=0.10 * strength)
        self.circle(xy, radius + 2, strength=0.20 * strength)
        self.circle(xy, radius, strength=strength)

    def finish(self):
        return self.image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def polyline_prefix(points, fraction):
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    target, result = sum(lengths) * fraction, [points[0]]
    for a, b, length in zip(points, points[1:], lengths):
        if target >= length:
            result.append(b)
            target -= length
        else:
            t = target / length if length else 0
            result.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            break
    return result


def trace(canvas, points, phase, poster):
    canvas.line(points, "line", 3)
    amount = 1 if poster else progress(phase)
    strength = 1 if poster else accent_strength(phase)
    prefix = polyline_prefix(points, amount)
    if len(prefix) > 1:
        canvas.line(prefix, "accent", 4, strength)
    canvas.dot(prefix[-1], phase, strength)


def quote(canvas, phase, poster):
    amount = 1 if poster else progress(phase)
    strength = 1 if poster else accent_strength(phase)
    for name, y in (("YES", 78), ("NO", 187)):
        canvas.text((34, y - 23), name, 29, bold=True)
        canvas.text((34, y + 14), "20 shares", 25, "muted")
        canvas.line([(198, y), (647, y)], width=2)
        canvas.line([(570, y - 11), (570, y + 11)], "muted", 2)
        canvas.text((570, y - 50), "50¢ mid", 28, "muted", anchor="ma")
        canvas.circle((312, y), 6, "accent", 0.65, outline=True)
        canvas.text((312, y - 50), "48¢ bid", 30, "accent", anchor="ma", bold=True)
        canvas.text((444, y + 16), "2¢ below mid", 26, "muted", anchor="ma")
        moving = (570 - 258 * amount, y)
        canvas.line([moving, (570, y)], "accent", 3, strength * 0.8)
        canvas.dot(moving, phase, strength, radius=5)


def fade(canvas, phase, poster):
    canvas.text((42, 23), "6-hour lookback", 29)
    canvas.text((674, 20), "+8¢ jump", 33, "accent", anchor="ra", bold=True)
    points = [(60, 174), (95, 176), (132, 170), (173, 176), (211, 172),
              (250, 174), (290, 171), (330, 174), (370, 173), (401, 175),
              (433, 174), (448, 162), (463, 138), (478, 116), (493, 91),
              (520, 83), (550, 83), (585, 86), (620, 85)]
    canvas.line([(60, 203), (650, 203)], width=1)
    canvas.text((60, 216), "6h ago", 26, "muted")
    canvas.text((650, 216), "Observed now", 26, "muted", anchor="ra")
    canvas.line([(657, 175), (657, 85)], "muted", 2)
    canvas.line([(649, 175), (665, 175)], "muted", 2)
    canvas.line([(649, 85), (665, 85)], "muted", 2)
    trace(canvas, points, phase, poster)


def settle(canvas, phase, poster):
    canvas.text((42, 23), "Entry price range", 29)
    canvas.text((674, 17), "90–96¢", 40, "accent", anchor="ra", bold=True)
    left, right, y = 59, 659, 168
    lo, hi = left + (right - left) * 0.90, left + (right - left) * 0.96
    canvas.rounded((left, y - 6, right, y + 6), radius=6, color="line")
    canvas.rounded((lo - 1, y - 21, hi + 1, y + 21), radius=8, strength=0.14)
    canvas.rounded((lo, y - 6, hi, y + 6), radius=5)
    canvas.line([(lo, y - 31), (lo, y + 31)], "accent", 2)
    canvas.line([(hi, y - 31), (hi, y + 31)], "accent", 2)
    # The full axis is linear. Only the narrow 90–96 band is eligible.
    canvas.line([(lo, y - 43), (lo, 106), (505, 106)], "accent", 2, 0.55)
    canvas.text((496, 88), "Entry band", 27, "accent", anchor="ra")
    canvas.text((left, 214), "0¢", 27, "muted")
    canvas.text((right, 214), "100¢", 27, "muted", anchor="ra")
    amount = 0.5 if poster else (1 - math.cos(phase * math.tau)) / 2
    canvas.dot((lo + (hi - lo) * (0.15 + 0.70 * amount), y), phase, radius=5)


def value(canvas, phase, poster):
    canvas.text((42, 23), "Price vs your forecast", 29)
    canvas.text((674, 20), "5¢ gap", 33, "accent", anchor="ra", bold=True)
    left, right, y = 216, 520, 141
    canvas.line([(66, y), (654, y)], width=2)
    canvas.rounded((left, y - 8, right, y + 8), radius=8, strength=0.14)
    canvas.line([(left, y - 15), (left, y + 15)], "muted", 2)
    canvas.line([(right, y - 15), (right, y + 15)], "accent", 2)
    canvas.circle((left, y), 6, "muted")
    canvas.circle((right, y), 7, "accent", outline=True)
    canvas.text((left, 188), "Market 60¢", 29, "muted", anchor="ma")
    canvas.text((right, 188), "Your view 65¢", 29, "accent", anchor="ma", bold=True)
    strength = 1 if poster else accent_strength(phase)
    amount = 1 if poster else progress(phase)
    end = left + (right - left) * amount
    canvas.line([(left, y), (end, y)], "accent", 3, strength)
    canvas.dot((end, y), phase, strength)


def momentum(canvas, phase, poster):
    canvas.text((42, 23), "6-hour move", 29)
    canvas.text((674, 20), "+10¢", 34, "accent", anchor="ra", bold=True)
    points = [(60, 184), (95, 180), (130, 173), (165, 165), (200, 169),
              (235, 158), (270, 147), (305, 151), (340, 137), (375, 129),
              (410, 120), (445, 124), (480, 110), (515, 103), (550, 97),
              (585, 85), (620, 80)]
    canvas.line([(60, 205), (650, 205)], width=1)
    canvas.text((60, 216), "40¢ · 6h ago", 26, "muted")
    canvas.text((650, 216), "50¢ · now", 26, "muted", anchor="ra")
    trace(canvas, points, phase, poster)


SCENES = {"mm": quote, "fade": fade, "settle": settle, "value": value, "momentum": momentum}


def render(scene, theme, regular_font, bold_font, frame=0, poster=False):
    canvas = Canvas(theme, regular_font, bold_font)
    scene(canvas, frame / FRAMES, poster)
    return canvas.finish()


def encode(scene, theme_name, regular_font, bold_font, output):
    theme = THEMES[theme_name]
    poster = render(SCENES[scene], theme, regular_font, bold_font, poster=True)
    target = output / f"{scene}-{theme_name}"
    poster.save(target.with_suffix(".png"), optimize=True)
    frames = [render(SCENES[scene], theme, regular_font, bold_font, frame)
              for frame in range(FRAMES)]
    # One palette for the whole animation prevents color flicker between frames.
    swatch = Image.new("RGB", (WIDTH, HEIGHT * 5))
    for index, image in enumerate([poster, frames[0], frames[18], frames[37], frames[54]]):
        swatch.paste(image, (0, HEIGHT * index))
    palette = swatch.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    indexed = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    indexed[0].save(target.with_suffix(".gif"), save_all=True, append_images=indexed[1:],
                    duration=DURATIONS, loop=0, optimize=True, disposal=1)
    gif = target.with_suffix(".gif")
    with Image.open(gif) as result:
        duration = 0
        for frame in range(result.n_frames):
            result.seek(frame)
            duration += result.info.get("duration", 0)
        assert result.format == "GIF" and result.n_frames > 1
        assert result.size == (WIDTH, HEIGHT) and duration == 4000
        assert result.info.get("loop") == 0
        assert gif.stat().st_size <= 500_000, f"{gif.name} exceeds the 500 KB budget"
        print(f"{gif.name}: {result.n_frames} frames, {duration} ms, "
              f"{gif.stat().st_size / 1000:.1f} KB; poster "
              f"{target.with_suffix('.png').stat().st_size / 1000:.1f} KB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--font", type=Path)
    parser.add_argument("--bold-font", type=Path)
    args = parser.parse_args()
    fonts = next(((Path(a), Path(b)) for a, b in FONT_CANDIDATES
                  if Path(a).is_file() and Path(b).is_file()), None)
    regular = args.font or (fonts[0] if fonts else None)
    bold = args.bold_font or (fonts[1] if fonts else regular)
    if not regular or not bold:
        parser.error("Pass --font and --bold-font with installed TrueType font paths.")
    args.output.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        for scene in SCENES:
            encode(scene, theme, regular, bold, args.output)


if __name__ == "__main__":
    main()
