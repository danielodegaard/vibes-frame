#!/usr/bin/env python3
"""Upload collages/*.jpg to Samsung The Frame Art Mode.

Safe to import: nothing connects until this file is run as a script.
pair() blocks until someone presses Allow on the TV. Do not run this
until that pairing step is intended.

The 2024 Frame (QN43LS03DAFXZA) takes slideshow duration in minutes.
3 is the on-screen minimum. category 2 is My Photos, sent by this
library as category_id MY-C0002. type False is an ordered slideshow.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from samsungframetv import SamsungTVWS
from samsungframetv import exceptions as frame_exceptions

HOST = "10.140.1.241"
PORT = 8002
NAME = "Vibes"
TOKEN_FILE = "/Users/danielodegaard/Desktop/Pinterest TV/.frame-token"
DURATION_MINUTES = 3
MY_PHOTOS_CATEGORY = 2  # library formats this as category_id MY-C0002
MY_PHOTOS_CATEGORY_ID = "MY-C0002"
UPLOAD_PAUSE_SECONDS = 2

ROOT = Path(__file__).resolve().parent.parent
COLLAGES = ROOT / "collages"
UPLOADED = COLLAGES / "uploaded.json"


def token_present() -> bool:
    path = Path(TOKEN_FILE)
    if not path.is_file():
        return False
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_collages() -> list[Path]:
    if not COLLAGES.is_dir():
        return []
    return sorted(path for path in COLLAGES.glob("*.jpg") if path.is_file())


def parse_record(value: object) -> dict | None:
    if isinstance(value, str) and value.strip():
        return {"content_id": value.strip(), "sha256": "", "matte": ""}
    if isinstance(value, dict):
        content_id = value.get("content_id")
        if isinstance(content_id, str) and content_id.strip():
            sha = value.get("sha256")
            matte = value.get("matte")
            return {
                "content_id": content_id.strip(),
                "sha256": sha if isinstance(sha, str) else "",
                "matte": matte if isinstance(matte, str) else "",
            }
    return None


def load_uploaded() -> dict[str, dict]:
    if not UPLOADED.is_file():
        return {}
    try:
        payload = json.loads(UPLOADED.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    records = {}
    for name, value in payload.items():
        if not isinstance(name, str) or not name.endswith(".jpg") or name.startswith("."):
            continue
        record = parse_record(value)
        if record:
            records[name] = record
    return records


def save_uploaded(records: dict[str, dict]) -> None:
    COLLAGES.mkdir(parents=True, exist_ok=True)
    ordered = {name: records[name] for name in sorted(records)}
    text = json.dumps(ordered, indent=2) + "\n"
    temporary = UPLOADED.with_suffix(".json.tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.replace(temporary, UPLOADED)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def names_from_matte_list(items: object, keys: tuple[str, ...]) -> list[str]:
    names = []
    if not isinstance(items, list):
        return names
    for item in items:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            for key in keys:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
                    break
    return names


def matte_score(name: str) -> tuple[int, int]:
    low = name.lower()
    thin = any(word in low for word in ("thin", "slim", "minimal", "narrow", "line"))
    black = "black" in low
    modern = any(word in low for word in ("modern", "simple", "flexible"))
    if thin and black:
        rank = 0
    elif black and modern:
        rank = 1
    elif black:
        rank = 2
    elif thin or modern:
        rank = 3
    else:
        rank = 4
    return (rank, len(low))


def choose_matte(art) -> str:
    """A thin black matte from the TV list, used when matte none is rejected."""
    listed = art.get_matte_list(include_colour=True)
    types: object = listed
    colors: object = []
    if isinstance(listed, tuple) and len(listed) == 2:
        types, colors = listed
    type_names = names_from_matte_list(types, ("matte_type", "matte_id", "id", "type"))
    color_names = names_from_matte_list(colors, ("color", "matte_color", "id"))
    black = next((color for color in color_names if "black" in color.lower()), None)
    candidates = []
    for name in type_names:
        low = name.lower()
        if low in {"none", "no_matte", "nomate", "off"}:
            continue
        if black and "_" not in name:
            candidates.append(f"{name}_{black}")
        else:
            candidates.append(name)
    if not candidates:
        raise RuntimeError("TV returned no usable matte")
    candidates.sort(key=matte_score)
    return candidates[0]


def upload_jpeg(art, path: Path, matte_state: dict) -> str:
    payload = path.read_bytes()
    matte = matte_state["id"]
    try:
        content_id = art.upload(
            payload,
            file_type="JPEG",
            matte=matte,
            portrait_matte=matte,
        )
    except frame_exceptions.ResponseError as exc:
        if matte_state["confirmed"]:
            raise
        fallback = choose_matte(art)
        print(f"TV rejected matte {matte} ({exc}); using {fallback}")
        matte_state["id"] = fallback
        matte_state["confirmed"] = True
        content_id = art.upload(
            payload,
            file_type="JPEG",
            matte=fallback,
            portrait_matte=fallback,
        )
    else:
        matte_state["confirmed"] = True
    if not content_id:
        raise RuntimeError(f"TV did not return a content id for {path.name}")
    print(f"matte={matte_state['id']}")
    return str(content_id)


def rotation_started(status: object) -> bool:
    value = status
    if isinstance(status, dict):
        value = status.get("value", status.get("data"))
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    value = parsed.get("value", stripped)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "off", "0", "false", "none"}
    return value not in (None, 0, False)


def start_slideshow(art) -> object:
    """Ordered My Photos rotation. Duration is minutes on this 2024 Frame."""
    try:
        art.set_slideshow_status(
            duration=DURATION_MINUTES,
            type=False,
            category=MY_PHOTOS_CATEGORY,
        )
    except Exception as exc:
        print(f"set_slideshow_status did not start rotation ({exc})", file=sys.stderr)
    try:
        status = art.get_slideshow_status()
    except Exception as exc:
        print(f"get_slideshow_status failed ({exc})", file=sys.stderr)
        status = None
    if not rotation_started(status):
        art.set_auto_rotation_status(
            duration=DURATION_MINUTES,
            type=False,
            category=MY_PHOTOS_CATEGORY,
        )
        status = art.get_slideshow_status()
    return status


def connect_art():
    pairing = not token_present()
    if pairing:
        print(
            "No token file. pair() waits until Allow is pressed on the TV.",
            flush=True,
        )
    tv = SamsungTVWS(
        HOST,
        port=PORT,
        token_file=TOKEN_FILE,
        name=NAME,
        timeout=60 if pairing else 20,
    )
    if pairing:
        tv.pair()
    if not token_present() and not tv.token:
        raise SystemExit("Pairing did not produce a token. Nothing was uploaded.")
    return tv, tv.art(timeout=30)


def publish(art, files: list[Path], previous: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    current: dict[str, dict] = {}
    digests = {path.name: sha256_file(path) for path in files}
    for path in files:
        record = previous.get(path.name)
        if record and record["sha256"] and record["sha256"] == digests[path.name]:
            current[path.name] = record

    for name, record in previous.items():
        kept = current.get(name)
        if kept and kept["content_id"] == record["content_id"]:
            continue
        content_id = record["content_id"]
        if not content_id:
            continue
        print(f"delete {content_id} ({name})")
        art.delete(content_id)

    matte_state = {"id": "none", "confirmed": False}
    pending = [path for path in files if path.name not in current]
    content_ids = []
    for index, path in enumerate(pending):
        content_id = upload_jpeg(art, path, matte_state)
        current[path.name] = {
            "content_id": content_id,
            "sha256": digests[path.name],
            "matte": matte_state["id"],
        }
        save_uploaded(current)
        content_ids.append(content_id)
        print(f"uploaded {path.name} {content_id}")
        if index != len(pending) - 1:
            time.sleep(UPLOAD_PAUSE_SECONDS)
    save_uploaded(current)
    ordered_ids = [current[path.name]["content_id"] for path in files if path.name in current]
    return current, ordered_ids


def main() -> int:
    files = list_collages()
    previous = load_uploaded()
    if not files and not previous:
        print("No collages to upload.")
        return 0

    tv, art = connect_art()
    try:
        _records, content_ids = publish(art, files, previous)
        for content_id in content_ids:
            print(f"content_id {content_id}")
        try:
            status = start_slideshow(art)
        except Exception as exc:
            print(f"slideshow_status error: {exc}", file=sys.stderr)
            return 1
        art.set_artmode(True)
        if content_ids:
            art.select_image(content_ids[0], category=MY_PHOTOS_CATEGORY_ID, show=True)
        print(f"slideshow_status {json.dumps(status, default=str)}")
        return 0
    finally:
        try:
            art.close()
        except Exception:
            pass
        try:
            tv.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
