#!/usr/bin/env python3
"""Refresh the Vibes board snapshot used by the TV screensaver.

Uses the public widget feed and RSS. When PINTEREST_ACCESS_TOKEN is set,
the official v5 board pins API is preferred. Stdlib only.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

ROOT = Path(__file__).resolve().parent.parent
PINS_PATH = ROOT / "pins.json"

BOARD_NAME = "Vibes"
BOARD_URL = "https://www.pinterest.com/danielodegaard/vibes/"
BOARD_ID = "478155754127188549"
WIDGET_URL = (
    "https://widgets.pinterest.com/v3/pidgets/boards/danielodegaard/vibes/pins/"
)
RSS_URL = "https://www.pinterest.com/danielodegaard/vibes.rss"
API_PINS_URL = f"https://api.pinterest.com/v5/boards/{BOARD_ID}/pins"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 30
MAX_PINS = 80
MAX_API_PAGES = 8
FALLBACK_COLOR = "#1a1a1a"

PINIMG_RE = re.compile(r"^https://i\.pinimg\.com/([^/]+)/(.+)$", re.IGNORECASE)
IMG_URL_RE = re.compile(r"https://i\.pinimg\.com/[^\s\"'<>]+", re.IGNORECASE)
SIZE_PREFIX_RE = re.compile(r"(\d+)")


class FetchError(Exception):
    """A board request failed or returned something we cannot use."""


def fetch_bytes(url: str, headers: dict | None = None) -> bytes:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"request failed for {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FetchError(f"timed out after {TIMEOUT_SECONDS}s for {url}") from exc


def image_signature(url: object) -> str | None:
    """Return the pinimg path after the size segment, if this is a pin image."""
    if not isinstance(url, str):
        return None
    clean = url.strip().split("?", 1)[0].split("#", 1)[0].split("&", 1)[0]
    match = PINIMG_RE.match(clean)
    if not match:
        return None
    signature = match.group(2).strip("/")
    if not signature or "/" not in signature:
        return None
    return signature


def positive_int(value: object) -> int | None:
    if isinstance(value, bool) or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value > 0 and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def normalize_color(value: object) -> str:
    if isinstance(value, str):
        color = value.strip()
        if color.startswith("#") and len(color) >= 4:
            return color
    return FALLBACK_COLOR


def best_rendition(images: object) -> tuple[int, int, str] | None:
    """Largest rendition that includes width, height, and a URL.

    Pixel area wins, so 564x is preferred over 236x. The numeric size prefix
    breaks ties.
    """
    if not isinstance(images, dict):
        return None
    best: tuple[tuple[int, int], int, int, str] | None = None
    for key, info in images.items():
        if not isinstance(info, dict):
            continue
        width = positive_int(info.get("width"))
        height = positive_int(info.get("height"))
        url = info.get("url")
        if width is None or height is None or not isinstance(url, str) or not url:
            continue
        prefix_match = SIZE_PREFIX_RE.match(str(key))
        prefix = int(prefix_match.group(1)) if prefix_match else 0
        rank = (width * height, prefix)
        if best is None or rank > best[0]:
            best = (rank, width, height, url)
    if best is None:
        return None
    return best[1], best[2], best[3]


def signature_from_images(images: object, preferred_url: str | None) -> str | None:
    signature = image_signature(preferred_url)
    if signature:
        return signature
    if not isinstance(images, dict):
        return None
    for info in images.values():
        if isinstance(info, dict):
            signature = image_signature(info.get("url"))
            if signature:
                return signature
    return None


def make_pin(
    pin_id: object,
    width: int | None,
    height: int | None,
    color: object,
    signature: str | None,
) -> dict | None:
    if pin_id is None or signature is None or width is None or height is None:
        return None
    text_id = str(pin_id).strip()
    if not text_id or width <= 0 or height <= 0:
        return None
    return {
        "id": text_id,
        "width": width,
        "height": height,
        "color": normalize_color(color),
        "src": f"https://i.pinimg.com/1200x/{signature}",
        "signature": signature,
        "published": None,
    }


def normalize_image_pin(raw: dict, images: object) -> dict | None:
    rendition = best_rendition(images)
    if rendition is None:
        return None
    width, height, url = rendition
    return make_pin(
        raw.get("id"),
        width,
        height,
        raw.get("dominant_color"),
        signature_from_images(images, url),
    )


def normalize_widget_pin(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return normalize_image_pin(raw, raw.get("images"))


def normalize_api_pin(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    media = raw.get("media")
    images = media.get("images") if isinstance(media, dict) else None
    return normalize_image_pin(raw, images)


def dedupe(pins: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for pin in pins:
        signature = pin["signature"]
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(pin)
    return unique


def fetch_widget_pins() -> list[dict]:
    # One request. This endpoint ignores page_size and does not paginate.
    raw = fetch_bytes(WIDGET_URL, {"Accept": "application/json"})
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise FetchError("widget status was not success")
    data = payload.get("data")
    raw_pins = data.get("pins") if isinstance(data, dict) else None
    if not isinstance(raw_pins, list):
        raise FetchError("widget response missing pins")
    pins = [pin for pin in (normalize_widget_pin(item) for item in raw_pins) if pin]
    return dedupe(pins)


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def child_text(element: ET.Element, name: str) -> str:
    for child in list(element):
        if local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def parse_pubdate(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pin_slug(link: str) -> str | None:
    if not link:
        return None
    parts = [part for part in urlparse(link).path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "pin" and parts[1]:
        return parts[1]
    return None


def fetch_rss_items() -> list[dict]:
    raw = fetch_bytes(
        RSS_URL,
        {"Accept": "application/rss+xml, application/xml, text/xml"},
    )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FetchError(f"RSS was not valid XML: {exc}") from exc

    items = []
    for element in root.iter():
        if local_name(element.tag) != "item":
            continue
        description = child_text(element, "description")
        match = IMG_URL_RE.search(description)
        signature = image_signature(match.group(0)) if match else None
        if not signature:
            continue
        slug = pin_slug(child_text(element, "link")) or pin_slug(
            child_text(element, "guid")
        )
        items.append(
            {
                "signature": signature,
                "published": parse_pubdate(child_text(element, "pubDate")),
                "id": slug,
            }
        )
    return items


def merge_widget_and_rss(widget_pins: list[dict], rss_items: list[dict]) -> list[dict]:
    """Dedupe by image signature and sort dated pins newest-first.

    RSS pubDate enriches a widget pin that shares an image signature. Undated
    pins keep widget order after dated ones. The feed has no width or height,
    so a pin that exists only in RSS is not emitted (its id would be the
    /pin/<slug>/ path segment, stored on the RSS item).
    """
    dates: dict[str, datetime] = {}
    for item in rss_items:
        published = item["published"]
        if published is None:
            continue
        signature = item["signature"]
        previous = dates.get(signature)
        if previous is None or published > previous:
            dates[signature] = published

    for pin in widget_pins:
        published = dates.get(pin["signature"])
        if published is not None:
            pin["published"] = published

    dated = [pin for pin in widget_pins if pin["published"] is not None]
    undated = [pin for pin in widget_pins if pin["published"] is None]
    dated.sort(key=lambda pin: pin["published"], reverse=True)
    return (dated + undated)[:MAX_PINS]


def fetch_api_pins(token: str) -> list[dict]:
    pins: list[dict] = []
    bookmark: str | None = None
    for _ in range(MAX_API_PAGES):
        params: dict[str, object] = {"page_size": 100}
        if bookmark:
            params["bookmark"] = bookmark
        url = f"{API_PINS_URL}?{urlencode(params)}"
        raw = fetch_bytes(
            url,
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise FetchError("API response was not an object")
        items = payload.get("items")
        if not isinstance(items, list):
            raise FetchError("API response missing items")
        for item in items:
            pin = normalize_api_pin(item)
            if pin:
                pins.append(pin)
        next_bookmark = payload.get("bookmark")
        if not items or not isinstance(next_bookmark, str) or not next_bookmark.strip():
            break
        bookmark = next_bookmark
    return dedupe(pins)


def public_pin(pin: dict) -> dict:
    return {
        "id": pin["id"],
        "width": pin["width"],
        "height": pin["height"],
        "color": pin["color"],
        "src": pin["src"],
    }


def existing_has_pins() -> bool:
    if not PINS_PATH.is_file():
        return False
    try:
        payload = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    pins = payload.get("pins") if isinstance(payload, dict) else None
    return isinstance(pins, list) and len(pins) > 0


def write_snapshot(pins: list[dict]) -> None:
    usable = pins[:MAX_PINS]
    if not usable:
        raise FetchError("refusing to write an empty pin list")
    payload = {
        "board": BOARD_NAME,
        "boardUrl": BOARD_URL,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pins": [public_pin(pin) for pin in usable],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = PINS_PATH.with_name(".pins.json.tmp")
    temporary.write_bytes(text.encode("utf-8"))
    try:
        os.replace(temporary, PINS_PATH)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def keep_existing(reason: str) -> int:
    if existing_has_pins():
        print(f"{reason} Leaving existing pins.json untouched.")
        return 0
    print(f"{reason} pins.json does not already exist.", file=sys.stderr)
    return 1


def load_public_sources() -> list[dict]:
    widget_pins: list[dict] = []
    rss_items: list[dict] = []
    widget_error: Exception | None = None
    rss_error: Exception | None = None

    try:
        widget_pins = fetch_widget_pins()
    except (FetchError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
        widget_error = exc
        print(f"Widget request failed: {exc}", file=sys.stderr)

    try:
        rss_items = fetch_rss_items()
    except (FetchError, UnicodeError, ValueError) as exc:
        rss_error = exc
        print(f"RSS request failed: {exc}", file=sys.stderr)

    if widget_error and rss_error:
        return []
    return merge_widget_and_rss(widget_pins, rss_items)


def main() -> int:
    token = os.environ.get("PINTEREST_ACCESS_TOKEN", "").strip()
    if token:
        try:
            api_pins = fetch_api_pins(token)
        except (FetchError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
            print(f"Pinterest API unavailable: {exc}", file=sys.stderr)
            api_pins = []
        if api_pins:
            write_snapshot(api_pins)
            count = min(len(api_pins), MAX_PINS)
            print(f"Updated pins.json with {count} pins from the Pinterest API.")
            return 0

    pins = load_public_sources()
    if not pins:
        return keep_existing("No usable pins from the widget or RSS.")
    write_snapshot(pins)
    dated = sum(1 for pin in pins if pin.get("published") is not None)
    print(f"Updated pins.json with {len(pins)} pins ({dated} dated from RSS).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
