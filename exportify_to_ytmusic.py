# /// script
# requires-python = ">=3.11"
# dependencies = ["ytmusicapi>=1.10", "platformdirs>=4"]
# ///
"""Copy Spotify playlists, exported with Exportify, into YouTube Music."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

from platformdirs import user_config_path, user_state_path
from ytmusicapi import YTMusic, setup

APP = "exportify-to-ytmusic"
DEFAULT_AUTH = user_config_path(APP) / "browser.json"
STATE_DIR = user_state_path(APP)

BATCH_SIZE = 25
DURATION_TOLERANCE_S = 15

# Exportify writes headers in the language of your Spotify account.
COLUMNS = {
    "title": ["Track Name", "Nome da faixa"],
    "artist": ["Artist Name(s)", "Nome(s) do artista"],
    "ms": ["Track Duration (ms)", "Duration (ms)", "Duração da faixa (ms)"],
}

# Headers from the browser that break requests made outside the browser.
# Firefox sends a gzipped body (Content-Encoding: gzip); we don't, so Google answers 400.
DROP_HEADERS = ("content-encoding", "content-length", "accept-encoding", "te", "priority")


class LoginExpired(Exception):
    pass


def field(row: dict[str, str], key: str) -> str:
    for name in COLUMNS[key]:
        if name in row:
            return (row[name] or "").strip()
    return ""


def first_artist(artists: str) -> str:
    # Exportify separates artists with "," and escapes commas inside names as "\,"
    return re.split(r"(?<!\\),", artists)[0].replace("\\,", ",").strip()


def clean_title(title: str) -> str:
    # "Song - Remastered 2011" or "Song (feat. X)" search worse than "Song"
    title = re.sub(r"\s*[\(\[](feat\.|with )[^\)\]]*[\)\]]", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+-\s+.*(remaster|version|edit|mix|live|mono|stereo).*$", "", title, flags=re.IGNORECASE)
    return title.strip()


# ---------------------------------------------------------------- login


def login(auth: Path) -> None:
    print("Paste the request headers from music.youtube.com, then press Enter and Ctrl+D:")
    raw = sys.stdin.read()
    lines = []
    for line in raw.splitlines():
        name = line.split(":", 1)[0].strip().lower()
        if ":" not in line or line.startswith(("GET ", "POST ")) or name in DROP_HEADERS:
            continue
        lines.append(line)
    if not any(l.lower().startswith("cookie:") for l in lines):
        sys.exit("No Cookie header found. Copy the headers of a POST 'browse' request while logged in.")

    auth.parent.mkdir(parents=True, exist_ok=True)
    setup(filepath=str(auth), headers_raw="\n".join(lines))
    auth.chmod(0o600)

    if not YTMusic(str(auth)).get_library_playlists(limit=5):
        print("Warning: logged in, but your library looks empty. Check you copied the headers while logged in.")
    print(f"Login saved to {auth}")


# ---------------------------------------------------------------- matching


def find_video(yt: YTMusic, title: str, artist: str, ms: int) -> str | None:
    query = f"{clean_title(title)} {first_artist(artist)}".strip()
    target = ms / 1000

    for kind in ("songs", "videos"):
        results = [r for r in yt.search(query, filter=kind, limit=5) or [] if r.get("videoId")]
        if not results:
            continue
        if ms:
            close = [r for r in results if abs((r.get("duration_seconds") or 0) - target) <= DURATION_TOLERANCE_S]
            if close:
                return close[0]["videoId"]
        if kind == "songs":
            return results[0]["videoId"]
    return None


# After a few thousand quick searches Google answers with a "Sorry..." HTML page
# (HTTP 403/429), which ytmusicapi surfaces as a JSONDecodeError. It lifts on its own.
RATE_LIMIT_WAITS_MIN = (2, 5, 10, 20, 30)
CALM_AFTER = 100  # successful calls before the wait goes back to the shortest step


class RateLimited(Exception):
    pass


# Shared across calls: a block that comes back right after a wait means it never
# really lifted, so the next wait should be longer, not start over at 2 minutes.
_backoff = {"step": 0, "ok": 0}


def call(fn, *args, **kwargs):
    """Run a request: retry network errors, wait out rate limits, raise LoginExpired on 401."""
    errors = 0
    while True:
        try:
            result = fn(*args, **kwargs)
            _backoff["ok"] += 1
            if _backoff["ok"] >= CALM_AFTER:
                _backoff["step"] = 0
            return result
        except Exception as e:
            msg = str(e)
            if "401" in msg:
                raise LoginExpired from e
            if isinstance(e, json.JSONDecodeError) or "403" in msg or "429" in msg:
                if _backoff["step"] == len(RATE_LIMIT_WAITS_MIN):
                    raise RateLimited from e
                wait = RATE_LIMIT_WAITS_MIN[_backoff["step"]]
                _backoff["step"] += 1
                _backoff["ok"] = 0
                print(f"  YouTube is rate limiting us, waiting {wait} min...")
                time.sleep(wait * 60)
                continue
            errors += 1
            if errors == 3:
                raise
            print(f"  error: {msg[:80]} (retrying in {10 * errors}s)")
            time.sleep(10 * errors)


# ---------------------------------------------------------------- import


def import_csv(yt: YTMusic, csv_path: Path, name: str, privacy: str, dry_run: bool, delay: float) -> None:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    if not rows or not field(rows[0], "title"):
        sys.exit(f"{csv_path}: doesn't look like an Exportify CSV.")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"{csv_path.stem}.json"
    state = json.loads(state_file.read_text()) if state_file.exists() and not dry_run else {}
    state.setdefault("done", 0)
    state.setdefault("missing", [])

    def save() -> None:
        if not dry_run:
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    if not dry_run and "playlist_id" not in state:
        state["playlist_id"] = call(yt.create_playlist, name, "Imported from Spotify", privacy_status=privacy)
        save()
    if pid := state.get("playlist_id"):
        print(f"Playlist: https://music.youtube.com/playlist?list={pid}")
    print(f"{name}: {len(rows)} songs, starting at #{state['done'] + 1}")

    batch: list[str] = []

    def flush(upto: int) -> None:
        if batch and not dry_run:
            call(yt.add_playlist_items, state["playlist_id"], batch, duplicates=True)
        batch.clear()
        state["done"] = upto
        save()

    for i in range(state["done"], len(rows)):
        title, artist = field(rows[i], "title"), field(rows[i], "artist")
        label = f"{artist} - {title}" if artist else title
        vid = call(find_video, yt, title, artist, int(field(rows[i], "ms") or 0))
        if vid:
            batch.append(vid)
            print(f"[{i + 1}/{len(rows)}] ok    {label}")
        else:
            state["missing"].append(label)
            print(f"[{i + 1}/{len(rows)}] MISS  {label}")
        if len(batch) >= BATCH_SIZE:
            flush(i + 1)
        time.sleep(delay)
    flush(len(rows))

    missing_file = Path(f"{csv_path.stem}.missing.txt")
    missing_file.write_text("\n".join(state["missing"]) + "\n", encoding="utf-8")
    found = len(rows) - len(state["missing"])
    print(
        f"\nDone: {found} {'found' if dry_run else 'added'}, {len(state['missing'])} not found (see {missing_file})\n"
    )


# ---------------------------------------------------------------- cli


def main() -> None:
    p = argparse.ArgumentParser(prog=APP, description=__doc__)
    p.add_argument("--auth", type=Path, default=DEFAULT_AUTH, help=f"login file (default: {DEFAULT_AUTH})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="save YouTube Music login from browser headers (read from stdin)")

    imp = sub.add_parser("import", help="import one or more Exportify CSV files")
    imp.add_argument("csv", type=Path, nargs="+")
    imp.add_argument("--name", help="playlist name (only with a single CSV; default: file name)")
    imp.add_argument("--public", action="store_true", help="make the playlist public instead of private")
    imp.add_argument("--dry-run", action="store_true", help="only search, don't create or change playlists")
    imp.add_argument("--delay", type=float, default=1.0, help="seconds between songs (default: 1)")

    sub.add_parser("reset", help="forget saved progress (playlists on YouTube Music are kept)")

    args = p.parse_args()

    if args.cmd == "login":
        login(args.auth)
        return
    if args.cmd == "reset":
        for f in STATE_DIR.glob("*.json"):
            f.unlink()
        print("Progress cleared.")
        return

    if not args.auth.exists():
        sys.exit(f"Not logged in. Run `{APP} login` first.")
    if args.name and len(args.csv) > 1:
        sys.exit("--name only works with a single CSV.")

    yt = YTMusic(str(args.auth))
    try:
        for path in args.csv:
            path = path.expanduser()
            name = args.name or path.stem.replace("_", " ").strip()
            import_csv(yt, path, name, "PUBLIC" if args.public else "PRIVATE", args.dry_run, args.delay)
    except LoginExpired:
        sys.exit(f"\nLogin expired. Progress is saved: run `{APP} login` again, then the same command.")
    except RateLimited:
        sys.exit("\nYouTube keeps blocking searches. Progress is saved: try again in a few hours.")
    except KeyboardInterrupt:
        sys.exit("\nStopped. Progress is saved, run the same command to continue.")


if __name__ == "__main__":
    main()
