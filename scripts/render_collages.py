#!/usr/bin/env python3
"""Pack pins.json into full-screen masonry JPEGs for The Frame.

Canvas is 3840x2160. Each pin is used once, newest first. A frame closes
once every column is at least the canvas height; the masonry is then scaled
to cover the screen. Run with the project virtualenv (Pillow is installed).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parent.parent
CANVAS_W = 3840
CANVAS_H = 2160
COLUMNS = 5
GAP = 18
RADIUS = 14
BACKGROUND = (0x0C, 0x0C, 0x0C)
JPEG_QUALITY = 90
TIMEOUT_SECONDS = 30
FALLBACK_SIZES = ("1200x", "originals", "736x", "564x")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
PINIMG_RE = re.compile(r"^(https://i\.pinimg\.com/)[^/]+/(.+)$", re.IGNORECASE)


def column_geometry() -> tuple[list[int], list[int]]:
    """Column widths that, with the gaps between them, add up to the canvas width."""
    total_gap = GAP * (COLUMNS - 1)
    base = (CANVAS_W - total_gap) // COLUMNS
    extra = (CANVAS_W - total_gap) % COLUMNS
    widths = [base + (1 if index < extra else 0) for index in range(COLUMNS)]
    positions: list[int] = []
    x = 0
    for width in widths:
        positions.append(x)
        x += width + GAP
    return widths, positions


def load_pins(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read {path}: {exc}") from exc
    raw_pins = payload.get("pins") if isinstance(payload, dict) else None
    if not isinstance(raw_pins, list) or not raw_pins:
        raise SystemExit(f"{path} has no pins")
    pins = []
    for raw in raw_pins:
        if not isinstance(raw, dict):
            continue
        src = raw.get("src")
        pin_id = raw.get("id")
        try:
            width = int(raw["width"])
            height = int(raw["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width <= 0 or height <= 0 or not isinstance(src, str) or not src or pin_id is None:
            continue
        pins.append(
            {
                "id": str(pin_id),
                "width": width,
                "height": height,
                "color": raw.get("color") or "#1a1a1a",
                "src": src,
            }
        )
    if not pins:
        raise SystemExit(f"{path} has no usable pins")
    return pins


def candidate_urls(src: str) -> list[str]:
    clean = src.strip().split("?", 1)[0].split("#", 1)[0]
    match = PINIMG_RE.match(clean)
    if not match:
        return [clean]
    prefix, signature = match.group(1), match.group(2)
    urls = []
    for size in FALLBACK_SIZES:
        url = f"{prefix}{size}/{signature}"
        if url not in urls:
            urls.append(url)
    return urls


def fetch_image(url: str) -> Image.Image:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://www.pinterest.com/",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise OSError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from exc
    try:
        image = Image.open(io.BytesIO(payload))
        image = ImageOps.exif_transpose(image)
        image.load()
    except (OSError, ValueError) as exc:
        raise OSError("response was not an image") from exc
    return image.convert("RGB")


def download_pin(pin: dict, cache: dict[str, Image.Image | None]) -> Image.Image | None:
    src = pin["src"]
    if src in cache:
        cached = cache[src]
        return None if cached is None else cached
    last_error = "no url"
    for url in candidate_urls(src):
        try:
            image = fetch_image(url)
        except (OSError, TimeoutError) as exc:
            last_error = str(exc)
            continue
        cache[src] = image
        return image
    cache[src] = None
    print(f"skip {pin['id']} ({last_error})", file=sys.stderr)
    return None


def parse_color(value: object) -> tuple[int, int, int]:
    if isinstance(value, str):
        try:
            rgb = ImageColor.getrgb(value)
        except ValueError:
            rgb = (0x1A, 0x1A, 0x1A)
        return rgb[:3]
    return (0x1A, 0x1A, 0x1A)


def pack_frame(
    pins: list[dict],
    start: int,
    widths: list[int],
    positions: list[int],
    cache: dict[str, Image.Image | None],
) -> tuple[list[dict], list[dict], int]:
    """Place pins into the shortest column until every column covers the canvas."""
    columns = [{"h": 0, "n": 0} for _ in range(COLUMNS)]
    items: list[dict] = []
    index = start
    while index < len(pins):
        if items and all(column["h"] >= CANVAS_H for column in columns):
            break
        pin = pins[index]
        index += 1
        image = download_pin(pin, cache)
        if image is None:
            continue
        column_index = min(range(COLUMNS), key=lambda item: (columns[item]["h"], item))
        column = columns[column_index]
        width = widths[column_index]
        y = column["h"] + (GAP if column["n"] else 0)
        height = max(1, round(width * pin["height"] / pin["width"]))
        items.append(
            {
                "id": pin["id"],
                "image": image,
                "color": parse_color(pin["color"]),
                "x": positions[column_index],
                "y": y,
                "w": width,
                "h": height,
            }
        )
        column["h"] = y + height
        column["n"] += 1
    return items, columns, index


def cover_transform(columns: list[dict], items: list[dict]) -> tuple[float, float, float]:
    """Scale and center so the packed photos cover 3840x2160.

    Height is the shortest column, so centering that column cannot open a
    gap under a taller neighbour. A short final frame scales up instead.
    """
    filled = [column for column in columns if column["h"] > 0]
    if len(filled) == len(columns):
        left = 0.0
        width = float(CANVAS_W)
        height = float(min(column["h"] for column in columns))
    else:
        left = float(min(item["x"] for item in items))
        right = float(max(item["x"] + item["w"] for item in items))
        width = max(1.0, right - left)
        height = float(min(column["h"] for column in filled))
    scale = max(CANVAS_W / width, CANVAS_H / height)
    offset_x = (CANVAS_W - width * scale) / 2 - left * scale
    offset_y = (CANVAS_H - height * scale) / 2
    if width * scale < CANVAS_W - 0.5 or height * scale < CANVAS_H - 0.5:
        raise RuntimeError("masonry does not cover the canvas")
    return scale, offset_x, offset_y


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    width, height = size
    if width <= 0 or height <= 0:
        return Image.new("L", (max(1, width), max(1, height)), 0)
    factor = 4
    big = Image.new("L", (width * factor, height * factor), 0)
    draw = ImageDraw.Draw(big)
    clamped = min(radius * factor, (width * factor) // 2, (height * factor) // 2)
    draw.rounded_rectangle(
        (0, 0, width * factor - 1, height * factor - 1),
        radius=clamped,
        fill=255,
    )
    return big.resize((width, height), Image.Resampling.LANCZOS)


def cover_resize(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    resized_w = max(1, round(image.width * scale))
    resized_h = max(1, round(image.height * scale))
    resized = image.resize((resized_w, resized_h), Image.Resampling.LANCZOS)
    left = max(0, (resized_w - width) // 2)
    top = max(0, (resized_h - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def paste_clipped(canvas: Image.Image, image: Image.Image, mask: Image.Image, dx: int, dy: int) -> None:
    src_x0 = max(0, -dx)
    src_y0 = max(0, -dy)
    src_x1 = min(image.width, CANVAS_W - dx)
    src_y1 = min(image.height, CANVAS_H - dy)
    if src_x0 >= src_x1 or src_y0 >= src_y1:
        return
    canvas.paste(
        image.crop((src_x0, src_y0, src_x1, src_y1)),
        (dx + src_x0, dy + src_y0),
        mask.crop((src_x0, src_y0, src_x1, src_y1)),
    )


def render_frame(items: list[dict], columns: list[dict], path: Path) -> None:
    scale, offset_x, offset_y = cover_transform(columns, items)
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BACKGROUND)
    for item in items:
        x0 = round(item["x"] * scale + offset_x)
        y0 = round(item["y"] * scale + offset_y)
        x1 = round((item["x"] + item["w"]) * scale + offset_x)
        y1 = round((item["y"] + item["h"]) * scale + offset_y)
        width = x1 - x0
        height = y1 - y0
        if width < 1 or height < 1:
            continue
        if x0 >= CANVAS_W or y0 >= CANVAS_H or x0 + width <= 0 or y0 + height <= 0:
            continue
        cell = Image.new("RGB", (width, height), item["color"])
        cell.paste(cover_resize(item["image"], width, height), (0, 0))
        mask = rounded_mask((width, height), RADIUS)
        paste_clipped(canvas, cell, mask, x0, y0)
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(
        path,
        format="JPEG",
        quality=JPEG_QUALITY,
        optimize=True,
        progressive=True,
    )


def resolve_path(value: Path | None, default: Path) -> Path:
    path = default if value is None else value
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Vibes pins as Frame collages.")
    parser.add_argument("--pins", type=Path, default=None, help="pins.json (default: project pins.json)")
    parser.add_argument("--out", type=Path, default=None, help="output directory (default: collages)")
    parser.add_argument("--limit", type=int, default=None, help="render only the first N frames")
    parser.add_argument("--prefix", default="collage", help="filename prefix, collage-001.jpg")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 1

    pins_path = resolve_path(args.pins, ROOT / "pins.json")
    out_dir = resolve_path(args.out, ROOT / "collages")
    pins = load_pins(pins_path)
    widths, positions = column_geometry()
    cache: dict[str, Image.Image | None] = {}
    manifest = []
    cursor = 0
    while cursor < len(pins):
        if args.limit is not None and len(manifest) >= args.limit:
            break
        items, columns, cursor = pack_frame(pins, cursor, widths, positions, cache)
        if not items:
            break
        number = len(manifest) + 1
        filename = f"{args.prefix}-{number:03d}.jpg"
        path = out_dir / filename
        render_frame(items, columns, path)
        manifest.append({"file": filename, "pinIds": [item["id"] for item in items]})
        with Image.open(path) as saved:
            saved_size = saved.size
        print(f"{path} bytes={path.stat().st_size} size={saved_size[0]}x{saved_size[1]} pins={len(items)}")

    if not manifest:
        print("No collage could be rendered.", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
