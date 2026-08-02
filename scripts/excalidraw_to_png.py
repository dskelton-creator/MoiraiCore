#!/usr/bin/env python3
"""
excalidraw_to_png.py — Render a .excalidraw file to PNG locally (no browser, no CDN).
Faithful static render of the elements we generate: rectangles (with bound text),
text labels, arrows (straight/elbow with optional bound points), ellipses, diamonds.

Usage:
  python3 excalidraw_to_png.py <input.excalidraw> <output.png> [--scale 2]
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BG = (27, 27, 27)              # #1b1b1b
DEFAULT_FILL = (230, 230, 230)
DEFAULT_STROKE = (31, 31, 31)
GRID = (43, 43, 43)
ARROW = (200, 200, 200)
TEXT = (20, 20, 20)
TEXT_ON_DARK = (235, 235, 235)

# STRIDE palette (fills) -> rgb
PALETTE = {
    "#ffc9c9": (255, 201, 201), "#ffd8a8": (255, 216, 168), "#fff3bf": (255, 243, 191),
    "#b2f2bb": (178, 242, 187), "#a5d8ff": (165, 216, 255), "#d0bfff": (208, 191, 255),
    "#ffa8a8": (255, 168, 168), "#bac8ff": (186, 200, 255), "#ffec99": (255, 236, 153),
    "#eebefa": (238, 190, 250), "#99e9f2": (153, 233, 242), "#ffd6e7": (255, 214, 231),
    "#ffffff": (255, 255, 255), "#1b1b1b": (27, 27, 27), "#1e1e1e": (30, 30, 30),
}

_FONT_CACHE = {}

def font(size, bold=False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    paths = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ]
    f = None
    for p in paths:
        if Path(p).exists():
            try:
                f = ImageFont.truetype(p, size)
                break
            except Exception:
                continue
    if f is None:
        f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def hex2rgb(h):
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return DEFAULT_STROKE


def text_color_for(fill_rgb):
    # pick readable text colour
    r, g, b = fill_rgb
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return TEXT if lum > 140 else TEXT_ON_DARK


def wrap_text(draw, text, fnt, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=fnt) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def add_el(el, index, elements, by_id):
    """Return an element's screen-space bounding box in scene coords."""
    x, y = el.get("x", 0), el.get("y", 0)
    w, h = el.get("width", 0), el.get("height", 0)
    return (x, y, x + w, y + h)


def el_bounds(el):
    x, y = el.get("x", 0), el.get("y", 0)
    w, h = el.get("width", 0), el.get("height", 0)
    return (x, y, x + w, y + h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--scale", type=float, default=2.0)
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text())
    elements = [e for e in data.get("elements", []) if not e.get("isDeleted")]
    by_id = {e["id"]: e for e in elements}

    # Bounding box of all elements (incl. text labels)
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for e in elements:
        b = el_bounds(e)
        minx = min(minx, b[0]); miny = min(miny, b[1])
        maxx = max(maxx, b[2]); maxy = max(maxy, b[3])
    if not elements or minx == float("inf"):
        minx = miny = 0; maxx = maxy = 100
    pad = 40
    minx -= pad; miny -= pad; maxx += pad; maxy += pad
    W = maxx - minx; H = maxy - miny

    S = args.scale
    img = Image.new("RGB", (int(W * S), int(H * S)), BG)
    d = ImageDraw.Draw(img)
    tf = lambda x, y: ((x - minx) * S, (y - miny) * S)

    # grid (subtle)
    grid_step = 20 * S
    for gx in range(0, img.width, int(grid_step)):
        d.line([(gx, 0), (gx, img.height)], fill=GRID, width=1)
    for gy in range(0, img.height, int(grid_step)):
        d.line([(0, gy), (img.width, gy)], fill=GRID, width=1)

    # Draw order: arrows first (behind), then shapes, then text on top
    arrows = [e for e in elements if e.get("type") == "arrow"]
    shapes = [e for e in elements if e.get("type") in ("rectangle", "ellipse", "diamond")]
    texts = [e for e in elements if e.get("type") == "text"]

    # map containerId -> container element (for bound text)
    container_of = {e["id"]: e for e in elements if e.get("containerId")}

    def draw_arrow(a):
        pts = a.get("points", [[0, 0]])
        x0, y0 = a["x"], a["y"]
        abs_pts = [tf(x0 + p[0], y0 + p[1]) for p in pts]
        color = hex2rgb(a.get("strokeColor", "#c8c8c8"))
        # bound endpoints if available
        start = a.get("startBinding") or {}
        end = a.get("endBinding") or {}
        width = max(1.5, (a.get("strokeWidth", 1) or 1) * S * 0.6)
        d.line(abs_pts, fill=color, width=int(width))
        # arrowhead at last point
        if len(abs_pts) >= 2:
            ax, ay = abs_pts[-1]; px, py = abs_pts[-2]
            import math
            ang = math.atan2(ay - py, ax - px)
            L = 10 * S
            for da in (math.radians(150), math.radians(210)):
                ex = ax - L * math.cos(ang + da)
                ey = ay - L * math.sin(ang + da)
                d.line([(ax, ay), (ex, ey)], fill=color, width=int(width))

    def draw_shape(s):
        b = el_bounds(s)
        x1, y1 = tf(b[0], b[1]); x2, y2 = tf(b[2], b[3])
        fill = PALETTE.get(s.get("backgroundColor", ""), DEFAULT_FILL)
        if s.get("backgroundColor") in ("transparent", "", None):
            fill = None
        stroke = hex2rgb(s.get("strokeColor", "#1f1f1f"))
        w = max(1, int((s.get("strokeWidth", 1) or 1) * S * 0.6))
        if s["type"] == "rectangle":
            if fill:
                d.rectangle([x1, y1, x2, y2], fill=fill, outline=stroke, width=w)
            else:
                d.rectangle([x1, y1, x2, y2], outline=stroke, width=w)
        elif s["type"] == "ellipse":
            if fill:
                d.ellipse([x1, y1, x2, y2], fill=fill, outline=stroke, width=w)
            else:
                d.ellipse([x1, y1, x2, y2], outline=stroke, width=w)
        elif s["type"] == "diamond":
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            poly = [(cx, y1), (x2, cy), (cx, y2), (x1, cy)]
            if fill:
                d.polygon(poly, fill=fill, outline=stroke)
            else:
                d.polygon(poly, outline=stroke)

    def draw_text_label(t, container=None):
        # position: if bound, center in container; else use x,y
        b = el_bounds(t)
        if container is not None:
            cb = el_bounds(container)
            cx1, cy1 = tf(cb[0], cb[1]); cx2, cy2 = tf(cb[2], cb[3])
            box_w = cx2 - cx1; box_h = cy2 - cy1
        else:
            cx1, cy1 = tf(b[0], b[1]); box_w = (b[2] - b[0]) * S; box_h = (b[3] - b[1]) * S
        txt = t.get("text", "")
        fs = max(10, int((t.get("fontSize", 16) or 16) * S * 0.62))
        fnt = font(fs, bold=(t.get("fontFamily", 1) in (2, 3)))
        # Decide text colour: prefer an explicit light strokeColor (e.g. diagram
        # headings on the dark canvas); otherwise derive from the box background.
        stroke = t.get("strokeColor", "")
        explicit = hex2rgb(stroke) if stroke and stroke not in ("transparent",) else None
        # measure wrapped
        avail = box_w - 8 * S
        lines = []
        for raw in txt.split("\n"):
            lines.extend(wrap_text(d, raw, fnt, max(20, avail)))
        lh = fs * 1.15
        total_h = lh * len(lines)
        # vertical center within box; horizontal center
        ty = cy1 + (box_h - total_h) / 2
        for ln in lines:
            tw = d.textlength(ln, font=fnt)
            tx = cx1 + (box_w - tw) / 2
            if explicit is not None:
                tc = explicit          # heading text uses its own (light) stroke colour
            else:
                fill_rgb = PALETTE.get(t.get("backgroundColor", ""), None) or (
                    PALETTE.get(container.get("backgroundColor", ""), None) if container else None)
                tc = TEXT_ON_DARK if (fill_rgb and (0.299*fill_rgb[0]+0.587*fill_rgb[1]+0.114*fill_rgb[2]) <= 140) else TEXT
            d.text((tx, ty), ln, font=fnt, fill=tc)
            ty += lh

    for a in arrows:
        draw_arrow(a)
    for s in shapes:
        draw_shape(s)
    for t in texts:
        container = container_of.get(t["id"])
        draw_text_label(t, container)

    img.save(args.output)
    print(f"wrote {args.output} ({img.width}x{img.height})")


if __name__ == "__main__":
    raise SystemExit(main())
