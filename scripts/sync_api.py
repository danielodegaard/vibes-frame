#!/usr/bin/env python3
"""Refresh pins.json from the Pinterest API. Prints NO_TOKEN and exits 0 if secrets are absent."""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PINS_PATH = ROOT / "pins.json"
TOKEN_PATH = ROOT / ".refresh-token.new"
BOARD_ID = "478155754127188549"
BOARD_URL = "https://www.pinterest.com/danielodegaard/vibes/"
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_PINS = 800
TIMEOUT = 30


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def request(url: str, method: str, headers: dict, body: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(180).decode("utf-8", "replace").replace("\n", " ")
        print(f"HTTP {exc.code} from Pinterest", file=sys.stderr)
        if detail and "token" not in detail.lower() and "secret" not in detail.lower():
            print(detail[:180], file=sys.stderr)
        return exc.code, b""


def refresh(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str | None]:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
    status, payload = request(
        "https://api.pinterest.com/v5/oauth/token",
        "POST",
        {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body,
    )
    if status != 200 or not payload:
        raise SystemExit(1)
    data = json.loads(payload.decode())
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        print("Pinterest did not return an access token", file=sys.stderr)
        raise SystemExit(1)
    rotated = data.get("refresh_token")
    new_refresh = rotated if isinstance(rotated, str) and rotated and rotated != refresh_token else None
    return access, new_refresh


def best_image(images: object) -> tuple[int, int, str, str | None] | None:
    if not isinstance(images, dict):
        return None
    best = None
    url_1200 = None
    for key, info in images.items():
        if not isinstance(info, dict):
            continue
        url = info.get("url")
        width = info.get("width")
        height = info.get("height")
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        if key == "1200x":
            url_1200 = url
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            rank = (width, height)
            if best is None or rank > best[0]:
                best = (rank, width, height, url)
    if best is None:
        return None
    return best[1], best[2], best[3], url_1200


def normalize(pin: object) -> dict | None:
    if not isinstance(pin, dict):
        return None
    media = pin.get("media")
    images = media.get("images") if isinstance(media, dict) else None
    picked = best_image(images)
    if picked is None:
        return None
    width, height, largest, url_1200 = picked
    pin_id = pin.get("id")
    if pin_id is None:
        return None
    text_id = str(pin_id).strip()
    if not text_id:
        return None
    color = pin.get("dominant_color")
    if not isinstance(color, str) or not color.startswith("#"):
        color = "#1a1a1a"
    return {
        "id": text_id,
        "width": width,
        "height": height,
        "color": color,
        "src": url_1200 or largest,
        "created_at": pin.get("created_at") if isinstance(pin.get("created_at"), str) else "",
    }


def list_pins(access: str) -> list[dict]:
    pins = []
    seen = set()
    bookmark = None
    for _ in range(MAX_PAGES):
        query = {"page_size": str(PAGE_SIZE)}
        if bookmark:
            query["bookmark"] = bookmark
        url = f"https://api.pinterest.com/v5/boards/{BOARD_ID}/pins?{urllib.parse.urlencode(query)}"
        status, payload = request(url, "GET", {"Authorization": f"Bearer {access}"})
        if status != 200 or not payload:
            raise SystemExit(1)
        data = json.loads(payload.decode())
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            break
        for item in items:
            pin = normalize(item)
            if pin is None or pin["id"] in seen:
                continue
            seen.add(pin["id"])
            pins.append(pin)
            if len(pins) >= MAX_PINS:
                return pins
        bookmark = data.get("bookmark") if isinstance(data, dict) else None
        if not isinstance(bookmark, str) or not bookmark.strip():
            break
        bookmark = bookmark.strip()
    return pins


def write_pins(pins: list[dict]) -> None:
    ordered = sorted(pins, key=lambda pin: pin.get("created_at") or "", reverse=True)
    payload = {
        "board": "Vibes",
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
            for pin in ordered
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = PINS_PATH.with_name(".pins.json.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, PINS_PATH)


def existing_nonempty() -> bool:
    if not PINS_PATH.is_file():
        return False
    try:
        data = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    pins = data.get("pins") if isinstance(data, dict) else None
    return isinstance(pins, list) and len(pins) > 0


def main() -> int:
    client_id = env("PINTEREST_CLIENT_ID")
    client_secret = env("PINTEREST_CLIENT_SECRET")
    refresh_token = env("PINTEREST_REFRESH_TOKEN")
    if not client_id or not client_secret or not refresh_token:
        print("NO_TOKEN")
        return 0
    access, new_refresh = refresh(client_id, client_secret, refresh_token)
    if new_refresh:
        TOKEN_PATH.write_text(new_refresh, encoding="utf-8")
        os.chmod(TOKEN_PATH, 0o600)
        print("NEW_REFRESH_TOKEN=1")
    pins = list_pins(access)
    if not pins:
        print("Pinterest returned no pins", file=sys.stderr)
        if existing_nonempty():
            print("Leaving existing pins.json untouched.", file=sys.stderr)
        return 1
    write_pins(pins)
    print(f"count={len(pins)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as exc:
        print(f"Pinterest request failed: {exc.reason}", file=sys.stderr)
        sys.exit(1)
