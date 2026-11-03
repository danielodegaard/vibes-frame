#!/usr/bin/env python3
"""Save the Vibes board to pins.json.

Without a Pinterest session this merges the public widget and RSS feeds.
That is the same partial snapshot as scripts/sync_pins.py (about 50 pins).
With a logged-in cookie it pages BoardFeedResource and can collect the
full board, up to 800 pins.

Run with the project virtualenv. This script does not connect to the TV.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
PINS_PATH = ROOT / "pins.json"
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import sync_pins  # noqa: E402

BOARD_NAME = "Vibes"
BOARD_URL = "https://www.pinterest.com/danielodegaard/vibes/"
BOARD_ID = "478155754127188549"
SOURCE_URL = "/danielodegaard/vibes/"
FEED_URL = "https://www.pinterest.com/resource/BoardFeedResource/get/"
PAGE_SIZE = 50
MAX_PAGES = 40
MAX_PINS = 800

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 30
PINIMG_RE = re.compile(r"^https?://i\.pinimg\.com/([^/]+)/(.+)$", re.IGNORECASE)
FALLBACK_COLOR = "#1a1a1a"


class FetchError(Exception):
    """A board request failed or returned something we cannot use."""


def fetch_bytes(url: str, headers: dict | None = None) -> bytes:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for {url.split('?', 1)[0]}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"request failed for BoardFeedResource: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FetchError(f"timed out after {TIMEOUT_SECONDS}s") from exc


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


def image_signature(url: object) -> str | None:
    """Path after the pinimg size segment: aa/bb/cc/hash.ext."""
    if not isinstance(url, str):
        return None
    clean = url.strip().split("?", 1)[0].split("#", 1)[0]
    if clean.startswith("//"):
        clean = "https:" + clean
    match = PINIMG_RE.match(clean)
    if not match:
        return None
    signature = match.group(2).strip("/")
    if not signature or "/" not in signature:
        return None
    return signature


def best_rendition(images: object) -> tuple[int, int, str] | None:
    """Largest width among renditions that include width, height, and a URL."""
    if not isinstance(images, dict):
        return None
    best: tuple[tuple[int, int], int, int, str] | None = None
    for info in images.values():
        if not isinstance(info, dict):
            continue
        width = positive_int(info.get("width"))
        height = positive_int(info.get("height"))
        url = info.get("url")
        if width is None or height is None or not isinstance(url, str) or not url.strip():
            continue
        rank = (width, height)
        if best is None or rank > best[0]:
            best = (rank, width, height, url.strip())
    if best is None:
        return None
    return best[1], best[2], best[3]


def normalize_feed_pin(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    images = raw.get("images")
    rendition = best_rendition(images)
    if rendition is None:
        return None
    width, height, url = rendition
    signature = image_signature(url)
    if signature is None and isinstance(images, dict):
        for info in images.values():
            if isinstance(info, dict):
                signature = image_signature(info.get("url"))
                if signature:
                    break
    if signature is None:
        return None
    pin_id = raw.get("id")
    if pin_id is None:
        return None
    text_id = str(pin_id).strip()
    if not text_id:
        return None
    return {
        "id": text_id,
        "width": width,
        "height": height,
        "color": normalize_color(raw.get("dominant_color")),
        "src": f"https://i.pinimg.com/1200x/{signature}",
        "signature": signature,
    }


def cookie_value(cookie_header: str, name: str) -> str | None:
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == name:
            token = value.strip()
            return token or None
    return None


def load_netscape_cookies(path: Path) -> str:
    if not path.is_file():
        raise FetchError(f"Cookie file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FetchError(f"Could not read cookie file: {exc}") from exc
    pairs: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5]:
            pairs.append(f"{parts[5]}={parts[6]}")
    if pairs:
        return "; ".join(pairs)
    compact = " ".join(text.split())
    if "csrftoken=" in compact:
        return compact
    raise FetchError(f"No Netscape cookies found in {path}")


def resolve_cookie_header(cookies_path: str | None) -> str | None:
    if cookies_path:
        return load_netscape_cookies(Path(cookies_path).expanduser())
    env = os.environ.get("PINTEREST_COOKIE", "").strip()
    return env or None


def feed_pins(raw_data: object) -> list:
    if isinstance(raw_data, list):
        return raw_data
    if isinstance(raw_data, dict):
        for key in ("pins", "results"):
            value = raw_data.get(key)
            if isinstance(value, list):
                return value
    raise FetchError("BoardFeedResource data was not a list of pins")


def fetch_feed_page(cookie_header: str, csrf: str, bookmark: str | None) -> dict:
    options: dict = {
        "board_id": BOARD_ID,
        "field_set_key": "react_grid_pin",
        "filter_section_pins": False,
        "is_react": True,
        "prepend": False,
        "page_size": PAGE_SIZE,
    }
    if bookmark:
        options["bookmarks"] = [bookmark]
    payload = {"options": options, "context": {}}
    query = urlencode(
        {
            "source_url": SOURCE_URL,
            "data": json.dumps(payload, separators=(",", ":")),
            "_": str(int(time.time() * 1000)),
        }
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-AppState": "active",
        "X-Pinterest-Source-Url": SOURCE_URL,
        "X-Pinterest-PWS-Handler": "www/[username]/[slug].js",
        "Origin": "https://www.pinterest.com",
        "X-CSRFToken": csrf,
        "Cookie": cookie_header,
        "Referer": BOARD_URL,
    }
    raw = fetch_bytes(f"{FEED_URL}?{query}", headers)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FetchError("BoardFeedResource did not return JSON") from exc
    if not isinstance(parsed, dict):
        raise FetchError("BoardFeedResource response was not an object")
    return parsed


def fetch_logged_in_pins(cookie_header: str) -> list[dict]:
    csrf = cookie_value(cookie_header, "csrftoken")
    if not csrf:
        raise FetchError("csrftoken is missing from the Pinterest cookies")
    pins: list[dict] = []
    seen_signatures: set[str] = set()
    seen_bookmarks: set[str] = set()
    bookmark: str | None = None
    for _page in range(MAX_PAGES):
        if len(pins) >= MAX_PINS:
            break
        payload = fetch_feed_page(cookie_header, csrf, bookmark)
        resource = payload.get("resource_response")
        if not isinstance(resource, dict):
            raise FetchError("BoardFeedResource response missing resource_response")
        batch = feed_pins(resource.get("data"))
        if not batch:
            break
        for raw in batch:
            if len(pins) >= MAX_PINS:
                break
            pin = normalize_feed_pin(raw)
            if pin is None:
                continue
            signature = pin["signature"]
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            pins.append(pin)
        new_bookmark = resource.get("bookmark")
        if not isinstance(new_bookmark, str) or not new_bookmark.strip():
            break
        new_bookmark = new_bookmark.strip()
        if new_bookmark == "-end-" or new_bookmark in seen_bookmarks or new_bookmark == bookmark:
            break
        seen_bookmarks.add(new_bookmark)
        bookmark = new_bookmark
    return pins


def public_pins() -> list[dict]:
    """Widget plus RSS, newest first when an RSS pubDate matches. Not the full board."""
    merged = sync_pins.load_public_sources()
    pins = []
    for pin in merged:
        pins.append(
            {
                "id": pin["id"],
                "width": pin["width"],
                "height": pin["height"],
                "color": pin["color"],
                "src": pin["src"],
            }
        )
    return pins


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
    payload = {
        "board": BOARD_NAME,
        "boardUrl": BOARD_URL,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pins": [
            {
                "id": pin["id"],
                "width": pin["width"],
                "height": pin["height"],
                "color": pin["color"],
                "src": pin["src"],
            }
            for pin in pins
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = PINS_PATH.with_name(".pins.json.tmp")
    temporary.write_bytes(text.encode("utf-8"))
    try:
        os.replace(temporary, PINS_PATH)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def refuse_empty() -> int:
    print("Refusing to write an empty pin list.", file=sys.stderr)
    if existing_has_pins():
        print("Leaving existing pins.json untouched.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Write pins.json for the Vibes board.")
    parser.add_argument(
        "--cookies",
        help="Netscape cookies.txt. Otherwise PINTEREST_COOKIE is used when set.",
    )
    args = parser.parse_args()
    try:
        cookie_header = resolve_cookie_header(args.cookies)
    except FetchError as exc:
        print(exc, file=sys.stderr)
        return 1

    full_board = cookie_header is not None
    try:
        if cookie_header:
            pins = fetch_logged_in_pins(cookie_header)
        else:
            pins = public_pins()
    except (FetchError, json.JSONDecodeError, UnicodeError, ValueError, OSError) as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        if existing_has_pins():
            print("Leaving existing pins.json untouched.", file=sys.stderr)
        return 1

    if not pins:
        return refuse_empty()
    try:
        write_snapshot(pins)
    except OSError as exc:
        print(f"Could not write pins.json: {exc}", file=sys.stderr)
        return 1
    print(f"FULL_BOARD={'yes' if full_board else 'no'} count={len(pins)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
