#!/usr/bin/env python3
"""Add new public Vibes pins in front of the saved list.

The public widget and RSS only expose the newest pins. This keeps every pin
already in pins.json and prepends ones the feed has not shown before. A failed
or empty fetch leaves the file untouched.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_pins

PINS_PATH = sync_pins.PINS_PATH


def load_existing() -> dict | None:
    if not PINS_PATH.is_file():
        return None
    try:
        payload = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Could not read pins.json: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        return None
    pins = payload.get("pins")
    if not isinstance(pins, list):
        return None
    return payload


def signature_of(pin: dict) -> str | None:
    return sync_pins.image_signature(pin.get("src"))


def main() -> int:
    existing = load_existing()
    if existing is None or not existing.get("pins"):
        print("pins.json has no pins to extend.", file=sys.stderr)
        return 1
    try:
        fresh = sync_pins.load_public_sources()
    except sync_pins.FetchError as exc:
        print(f"Public feed unavailable: {exc}. Leaving pins.json untouched.")
        return 0
    if not fresh:
        print("Public feed returned no pins. Leaving pins.json untouched.")
        return 0

    pins = [pin for pin in existing["pins"] if isinstance(pin, dict)]
    known_ids = {str(pin.get("id")) for pin in pins if pin.get("id")}
    known_signatures = {sig for sig in (signature_of(pin) for pin in pins) if sig}

    added = []
    for pin in fresh:
        pin_id = str(pin.get("id") or "")
        signature = pin.get("signature")
        if pin_id in known_ids or signature in known_signatures:
            continue
        public = sync_pins.public_pin(pin)
        added.append(public)
        known_ids.add(pin_id)
        if signature:
            known_signatures.add(signature)

    if not added:
        print(f"No new pins. Keeping {len(pins)}.")
        return 0

    existing["pins"] = added + pins
    existing["board"] = sync_pins.BOARD_NAME
    existing["boardUrl"] = sync_pins.BOARD_URL
    existing["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
    temporary = PINS_PATH.with_name(".pins.json.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(PINS_PATH)
    print(f"Added {len(added)} pins. Total {len(existing['pins'])}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
