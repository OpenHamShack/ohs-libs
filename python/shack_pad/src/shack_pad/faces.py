"""What a screen key shows, and drawing it.

A face is two short lines, a tone and optionally an icon. Apps send faces
already translated (CONTROLLER-PAD.md §6.1: `l1`, `l2`, `tone`); the
library maps the tone to the brand colours, so every app's ON AIR is the
same red and nobody picks a colour per app.

Drawn with Pillow rather than ffmpeg (the box's way): ffmpeg is not on a
Windows or macOS desktop, and one renderer everywhere means the pad looks
the same on the controller and on a laptop. Pillow is optional (the
`shack-pad[hid]` extra); without it render_jpeg returns None, the driver
draws nothing and every key still works.

Text is set in the platform's system font: the brand rule is system faces
only, nothing vendored (~/openhamshack/brand/README.md). Icons are either
the brand kit's app icons (icons/app-*.png, copied from brand/icons/png at
64 px, for the app-switching keys) or small glyphs drawn here from
primitives. The brand kit has no action glyphs yet; drawing them in code
keeps the package free of assets nobody designed, and a PNG called
icons/<name>.png, if the brand build ever makes one, is used in preference.
"""

from __future__ import annotations

import io
import math
import sys
from dataclasses import dataclass
from pathlib import Path

ICONS = Path(__file__).resolve().parent / "icons"

TONES = ("normal", "active", "info", "warn", "onair", "disabled", "big", "blank")

# (background, ink) per tone, from brand/tokens/tokens.json's dark theme:
# the keys are small lit screens in a dark case, so the dark palette is the
# one that reads. onair is tx-fill / tx-on-fill: the red every app shares.
TONE_COLOURS = {
    "normal": ("#1a232e", "#e8eef5"),     # raised / ink
    "big": ("#1a232e", "#e8eef5"),
    "active": ("#4fd1e8", "#04141a"),     # accent-fill / accent-on-fill
    "info": ("#7cb8ff", "#04101f"),       # info-fill / info-on-fill
    "warn": ("#ffc24d", "#1d1300"),       # warn-fill / warn-on-fill
    "onair": ("#d42019", "#ffffff"),      # tx-fill / tx-on-fill
    "disabled": ("#121922", "#6f7d8e"),   # surface / ink-faint
    "blank": ("#000000", "#000000"),
}


@dataclass(frozen=True)
class Face:
    l1: str = ""
    l2: str = ""
    tone: str = "normal"
    icon: str | None = None
    big: bool = False

    @classmethod
    def from_app(cls, d: dict, icon: str | None = None) -> "Face":
        """A face as an app sends it ({"l1", "l2", "tone"}); a tone this
        version does not know is drawn as normal rather than refused."""
        tone = d.get("tone") if d.get("tone") in TONES else "normal"
        return cls(str(d.get("l1") or "")[:12], str(d.get("l2") or "")[:12], tone,
                   d.get("icon") or icon, bool(d.get("big")) or tone == "big")


BLANK = Face(tone="blank")

# --- Fonts -------------------------------------------------------------------------
# The bold system face per platform. Linux paths are measured (Debian,
# Fedora, Arch); the Windows and macOS ones are [guess] until a pad is
# drawn on those systems. Pillow's built-in font is the last resort, so a
# missing font never stops the pad.
FONT_PATHS = {
    "linux": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"],
    "win32": [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf"],
    "darwin": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
               "/System/Library/Fonts/Helvetica.ttc"],
}
_fonts: dict[int, object] = {}


def font_path() -> str | None:
    plat = "linux" if sys.platform.startswith("linux") else sys.platform
    return next((p for p in FONT_PATHS.get(plat, []) if Path(p).is_file()), None)


def _font(size: int):
    from PIL import ImageFont  # noqa: PLC0415
    if size not in _fonts:
        path = font_path()
        try:
            _fonts[size] = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
        except OSError:
            _fonts[size] = ImageFont.load_default(size)
    return _fonts[size]


# --- Glyphs ------------------------------------------------------------------------
def _glyph(draw, name: str, box: tuple[int, int, int], ink: str) -> bool:
    """Draw glyph `name` in the square (x, y, side). False when unknown."""
    x, y, s = box
    w = max(2, s // 10)

    def P(fx: float, fy: float) -> tuple[float, float]:
        return x + fx * s, y + fy * s

    def line(*pts, width=w):
        draw.line([P(*p) for p in pts], fill=ink, width=width, joint="curve")

    def speaker():
        draw.polygon([P(.1, .38), P(.28, .38), P(.5, .18), P(.5, .82), P(.28, .62), P(.1, .62)], fill=ink)

    if name in ("ptt", "mic"):
        draw.rounded_rectangle([P(.36, .08), P(.64, .58)], radius=s * .14, fill=ink)
        draw.arc([P(.22, .28), P(.78, .72)], 0, 180, fill=ink, width=w)
        line((.5, .72), (.5, .88))
        line((.32, .9), (.68, .9))
    elif name == "transmit":
        line((.5, .35), (.32, .92))
        line((.5, .35), (.68, .92))
        line((.39, .7), (.61, .7))
        for r in (.16, .3):
            draw.arc([P(.5 - r, .35 - r), P(.5 + r, .35 + r)], 200, 340, fill=ink, width=w)
    elif name == "volume":
        speaker()
        for r in (.14, .26):
            draw.arc([P(.5 - r, .5 - r), P(.5 + r, .5 + r)], -50, 50, fill=ink, width=w)
    elif name == "mute":
        speaker()
        line((.62, .36), (.9, .64))
        line((.9, .36), (.62, .64))
    elif name == "record":
        draw.ellipse([P(.22, .22), P(.78, .78)], fill=ink)
    elif name == "tune":
        draw.ellipse([P(.15, .15), P(.85, .85)], outline=ink, width=w)
        line((.5, .5), (.5 + .28 * math.cos(math.radians(-60)), .5 + .28 * math.sin(math.radians(-60))))
    elif name == "step":
        for dx in (0, .28):
            line((.22 + dx, .25), (.46 + dx, .5), (.22 + dx, .75))
    elif name == "band":
        for i, fw in enumerate((.35, .55, .75)):
            draw.rectangle([P(.15, .2 + i * .22), P(.15 + fw, .32 + i * .22)], fill=ink)
    elif name == "mode":
        pts = [(.1 + .8 * i / 24, .5 - .25 * math.sin(i / 24 * 4 * math.pi)) for i in range(25)]
        line(*pts)
    elif name == "scope":
        draw.rectangle([P(.1, .18), P(.9, .82)], outline=ink, width=max(1, w // 2))
        line((.15, .72), (.3, .68), (.4, .4), (.48, .7), (.62, .62), (.7, .3), (.78, .68), (.86, .7), width=max(1, w // 2))
    elif name == "power":
        draw.arc([P(.2, .22), P(.8, .82)], -60, 240, fill=ink, width=w)
        line((.5, .12), (.5, .5))
    elif name == "page":
        draw.rectangle([P(.3, .15), P(.85, .7)], outline=ink, width=w)
        draw.rectangle([P(.15, .3), P(.7, .85)], fill=ink)
    elif name == "apps":
        for gx in (.18, .55):
            for gy in (.18, .55):
                draw.rounded_rectangle([P(gx, gy), P(gx + .27, gy + .27)], radius=s * .05, fill=ink)
    elif name == "channel":
        line((.38, .15), (.3, .85))
        line((.7, .15), (.62, .85))
        line((.15, .38), (.88, .38))
        line((.12, .62), (.85, .62))
    elif name == "receivers":
        for gx, gy in ((.12, .2), (.52, .2), (.12, .55), (.52, .55)):
            draw.rectangle([P(gx, gy), P(gx + .36, gy + .25)], outline=ink, width=max(1, w // 2))
    elif name == "preset":
        draw.polygon([P(.25, .12), P(.75, .12), P(.75, .88), P(.5, .68), P(.25, .88)], fill=ink)
    else:
        return False
    return True


GLYPHS = ("ptt", "mic", "transmit", "volume", "mute", "record", "tune", "step", "band", "mode", "scope",
          "power", "page", "apps", "channel", "receivers", "preset")


def app_icon_path(kind: str) -> Path | None:
    """The brand app icon for an app kind ('qo100nb', 'qo100-nb',
    'shack-qo100nb' all name the QO-100 NB icon)."""
    want = kind.removeprefix("shack-").replace("-", "")
    for p in sorted(ICONS.glob("app-*.png")):
        if p.stem[4:].replace("-", "") == want:
            return p
    return None


def _paste_icon(img, name: str, box: tuple[int, int, int], ink: str, draw) -> bool:
    from PIL import Image  # noqa: PLC0415
    path = app_icon_path(name[4:]) if name.startswith("app:") else ICONS / f"{name}.png"
    if path and path.is_file():
        icon = Image.open(path).convert("RGBA").resize((box[2], box[2]), Image.LANCZOS)
        img.paste(icon, (box[0], box[1]), icon)
        return True
    return _glyph(draw, name, box, ink)


# --- Drawing -----------------------------------------------------------------------
_jpeg_cache: dict[tuple, bytes] = {}
# Bounded, as on the box: the fixed faces are a few dozen images, but a
# frequency or a meter on a key draws a new one every change and would
# otherwise grow it for as long as the process runs. Oldest first out.
JPEG_CACHE_MAX = 400


def have_pillow() -> bool:
    try:
        import PIL  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def render(face: Face, size: int = 64):
    """The face drawn upright as a Pillow RGB image. Font sizes were judged
    on the AKP03E's 64 px keys (the box's numbers) and scale with size, so a
    bigger screen gets the same layout, not smaller text."""
    from PIL import Image, ImageDraw  # noqa: PLC0415
    bg, ink = TONE_COLOURS.get(face.tone, TONE_COLOURS["normal"])
    img = Image.new("RGB", (size, size), bg)
    if face.tone == "blank":
        return img
    draw = ImageDraw.Draw(img)

    def px(n: float) -> int:
        return max(1, round(n * size / 64))

    def text(t: str, fs: int, cy: float) -> None:
        if not t:
            return
        fs = px(fs)
        # Shrink until it fits: a translated word may be longer than the
        # English the sizes were judged on.
        while fs > px(8) and draw.textlength(t, font=_font(fs)) > size - px(4):
            fs -= 1
        draw.text((size / 2, px(cy)), t, fill=ink, font=_font(fs), anchor="mm")

    icon = face.icon
    if icon and not face.big and not face.l2 and _paste_icon(img, icon, (px(16) if face.l1 else px(12), px(5) if face.l1 else px(12),
                                                        px(32) if face.l1 else px(40)), ink, draw):
        text(face.l1, 12, 50)
        return img
    if face.big:
        fs = 26 if len(face.l1) <= 3 else 22 if len(face.l1) <= 4 else 17
        text(face.l1, fs, 22)
        text(face.l2, 12, 51)
    elif face.l2:
        text(face.l1, 15, 22)
        text(face.l2, 13, 44)
    else:
        fs = 20 if len(face.l1) <= 4 else 15 if len(face.l1) <= 6 else 12
        text(face.l1, fs, 32)
    return img


def render_jpeg(face: Face, size: int = 64, turn: str = "") -> bytes | None:
    """A size x size JPEG for a key screen, turned for how the model mounts
    its screens ('cw': a quarter clockwise; '' none). None without Pillow."""
    k = (face, size, turn)
    if k in _jpeg_cache:
        return _jpeg_cache[k]
    if not have_pillow():
        return None
    from PIL import Image  # noqa: PLC0415
    img = render(face, size)
    if turn == "cw":
        img = img.transpose(Image.Transpose.ROTATE_270)   # PIL turns anticlockwise: 270 = a quarter clockwise
    elif turn == "ccw":
        img = img.transpose(Image.Transpose.ROTATE_90)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    jpeg = buf.getvalue()
    if len(_jpeg_cache) >= JPEG_CACHE_MAX:
        del _jpeg_cache[next(iter(_jpeg_cache))]
    _jpeg_cache[k] = jpeg
    return jpeg
