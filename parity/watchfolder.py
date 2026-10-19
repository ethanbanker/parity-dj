"""M3U playlists and a watch folder, for libraries that do not start in rekordbox.

Not every set starts in a rekordbox collection.  A DJ may be handed an M3U from
a download, or drop new files into a folder all night.  Both are the same
operation underneath: turn files into library rows, add those rows to the store,
and put them in a playlist the pool can be filtered by.

* **M3U/M3U8** is read as a playlist: ``#EXTINF`` carries the length and the
  ``artist - title``, relative paths resolve against the playlist's own folder,
  and streaming URLs are kept as streaming rows (they have no file to play, and
  the pool already says so).  Files that are already in the library are matched
  by absolute path, so an import never duplicates a track the library has.
* **The watch folder** is the same read, repeated on a timer: a new audio file
  appears, it is probed with ffprobe, added to the library and to a playlist
  named for the folder.  Nothing is moved or converted -- that is what Rename &
  convert is for -- and a file that disappears is left in the library rather
  than silently deleted.

Both write only into Parity's stored library.  rekordbox, AUTO/PILOT and the
files themselves are untouched until the user exports or writes a set.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from . import ingest, rekordbox

log = logging.getLogger("parity.watchfolder")

#: What the watch folder and an M3U are allowed to add.
AUDIO_EXTENSIONS = ingest.AUDIO_EXTENSIONS

#: How often the background watcher looks when it is switched on.
DEFAULT_INTERVAL = 5.0

#: The playlist an M3U import creates when it has no ``#PLAYLIST`` line.
DEFAULT_M3U_NAME = "Imported playlist"

_EXTINF = re.compile(r"^#EXTINF:\s*(-?[\d.]+)\s*,\s*(.*)$", re.IGNORECASE)
_PLAYLIST = re.compile(r"^#PLAYLIST:\s*(.+)$", re.IGNORECASE)


class PlaylistError(ValueError):
    """The playlist or folder cannot be read as asked."""


# ------------------------------------------------------------------- M3U parse


def parse_m3u(text: str, base_dir: str | Path = "", *, name: str = "") -> dict:
    """Read an M3U/M3U8 document into entries with resolved paths.

    Nothing is validated against the disk here: a streaming URL is a row with no
    local file, and a local path is kept even when it is not mounted yet -- the
    caller reports which ones it could not find.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", "replace")
    text = (text or "").lstrip("\ufeff")
    playlist_name = name or ""
    entries: list[dict] = []
    pending: dict | None = None
    base = Path(str(base_dir)).expanduser() if str(base_dir or "").strip() else None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.upper() == "#EXTM3U":
            continue
        playlist = _PLAYLIST.match(line)
        if playlist:
            playlist_name = playlist.group(1).strip() or playlist_name
            continue
        info = _EXTINF.match(line)
        if info:
            duration = 0.0
            try:
                duration = max(0.0, float(info.group(1)))
            except ValueError:
                duration = 0.0
            title = info.group(2).strip()
            artist = ""
            if " - " in title:
                artist, title = title.split(" - ", 1)
                artist, title = artist.strip(), title.strip()
            pending = {"duration": duration, "title": title, "artist": artist}
            continue
        if line.startswith("#"):
            continue

        location = line
        streaming = bool(re.match(r"^(?:https?|soundcloud|tidal|beatport|spotify)://", line, re.I))
        path = ""
        if streaming:
            path = line
        elif line.startswith("file://"):
            path = rekordbox.location_to_path(line)
        else:
            candidate = Path(line).expanduser()
            if not candidate.is_absolute() and base is not None:
                candidate = base / candidate
            path = str(candidate)
        entry = {
            "path": path,
            "streaming": streaming,
            "duration": (pending or {}).get("duration", 0.0),
            "title": (pending or {}).get("title", ""),
            "artist": (pending or {}).get("artist", ""),
        }
        entries.append(entry)
        pending = None

    if not playlist_name:
        playlist_name = DEFAULT_M3U_NAME
    return {"name": playlist_name, "entries": entries}


def read_m3u(path: str | Path) -> dict:
    """Read a playlist file, resolving relative paths against its own folder."""
    source = Path(str(path)).expanduser()
    if not source.is_file():
        raise PlaylistError(f"There is no playlist at {source}")
    parsed = parse_m3u(
        source.read_text(encoding="utf-8-sig", errors="replace"),
        source.parent,
        name=source.stem,
    )
    if not parsed["entries"]:
        raise PlaylistError(f"{source.name} lists no tracks.")
    return parsed


# ---------------------------------------------------------------- file -> row


def _name_from_stem(stem: str) -> tuple[str, str]:
    cleaned = re.sub(r"^\s*\d{1,3}[\s._-]+", "", stem).strip()
    if " - " in cleaned:
        artist, title = cleaned.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", cleaned or stem


def track_record(
    path: str | Path,
    track_id: str,
    *,
    fallback: dict | None = None,
    streaming: bool = False,
    source: str = "",
) -> dict:
    """Turn one file (or stream) into a stored library record.

    ffprobe supplies what the file says about itself; the M3U's own ``EXTINF``
    and the filename fill the gaps.  A file ffprobe cannot read still becomes a
    row -- unplayable files are reported by pre-flight, not silently dropped.
    """
    fallback = fallback or {}
    text = str(path or "")
    track = rekordbox.Track(track_id=str(track_id))
    track.location = text if streaming else "file://localhost" + quote(text)
    if streaming:
        track.kind = "streaming"
    probed: dict = {}
    if not streaming and text and Path(text).is_file():
        try:
            probed = ingest.probe(Path(text))
        except Exception as exc:  # noqa: BLE001 - a row without tags is still a row
            log.debug("could not probe %s: %s", text, exc)
    tags = {str(key).lower(): value for key, value in (probed.get("tags") or {}).items()}
    title = str(tags.get("title") or fallback.get("title") or "").strip()
    artist = str(tags.get("artist") or fallback.get("artist") or "").strip()
    if not title and not streaming:
        artist_from_name, title = _name_from_stem(Path(text).stem)
        artist = artist or artist_from_name
    if not title:
        title = fallback.get("title") or (Path(urlparse(text).path).stem if text else "Untitled")
    track.title = title or "Untitled"
    track.artist = artist
    track.album = str(tags.get("album") or fallback.get("album") or "")
    track.genre = str(tags.get("genre") or fallback.get("genre") or "")
    track.comments = str(tags.get("comment") or "")
    track.label = str(tags.get("label") or "")
    track.tonality = str(tags.get("key") or "")
    try:
        track.bpm = float(tags.get("bpm") or 0)
    except (TypeError, ValueError):
        track.bpm = 0.0
    duration = float(probed.get("duration") or fallback.get("duration") or 0)
    track.duration = round(duration, 3)
    return track.to_dict()


def _normal(path: object) -> str:
    """A path key that survives a symlink and a case-insensitive disk.

    ``/tmp`` and ``/private/tmp`` are the same folder on macOS, and a library
    imported by one name must not gain a second row when the watch folder sees
    it under the other.  ``strict=False`` resolves what exists and leaves the
    rest as written, so a missing file still has a stable key.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        text = str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        pass
    return os.path.normcase(text)


def _existing_by_path(store) -> dict[str, dict]:
    """The loaded library's local records, keyed by normalised absolute path."""
    found: dict[str, dict] = {}
    for record in store.tracks_by_key().values():
        path = str(record.get("path") or "")
        if path and not record.get("streaming"):
            found[_normal(path)] = record
    return found


def _record_id(prefix: str, path: str) -> str:
    digest = hashlib.sha1(os.path.normcase(str(path)).encode("utf-8", "replace")).hexdigest()
    return f"{prefix}:{digest[:16]}"


def _unique_id(prefix: str, path: str, taken: set[str]) -> str:
    candidate = _record_id(prefix, path)
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    index = 2
    while f"{candidate}-{index}" in taken:
        index += 1
    candidate = f"{candidate}-{index}"
    taken.add(candidate)
    return candidate


# ---------------------------------------------------------------- M3U import


def import_m3u(
    store,
    *,
    path: str | Path | None = None,
    text: str | None = None,
    name: str = "",
    base_dir: str | Path | None = None,
) -> dict:
    """Import one M3U into the library as a playlist, without duplicating rows."""
    if path not in (None, ""):
        parsed = read_m3u(path)
        base = Path(str(path)).expanduser().parent
        source = str(Path(str(path)).expanduser())
    else:
        base = base_dir or ""
        parsed = parse_m3u(text or "", base, name=name or "")
        source = "dropped playlist"
    entries = parsed["entries"]
    if not entries:
        raise PlaylistError("That playlist lists no tracks.")

    existing = _existing_by_path(store)
    taken = set(store.tracks_by_key())
    records: dict[str, dict] = {}
    playlist_ids: list[str] = []
    keys: list[str] = []
    matched = 0
    missing: list[dict] = []
    streaming = 0
    for entry in entries:
        raw_path = entry["path"]
        if entry["streaming"]:
            streaming += 1
            track_id = _unique_id("m3u", raw_path, taken)
            record = track_record(raw_path, track_id, fallback=entry, streaming=True)
            records[track_id] = record
            playlist_ids.append(track_id)
            keys.append(record["key"])
            continue
        local = _normal(raw_path)
        found = existing.get(local) if local else None
        if found is not None:
            matched += 1
            playlist_ids.append(str(found.get("id")))
            keys.append(found["key"])
            continue
        if not raw_path or not Path(raw_path).expanduser().is_file():
            missing.append({"path": raw_path, "title": entry.get("title", "")})
            continue
        track_id = _unique_id("m3u", raw_path, taken)
        record = track_record(raw_path, track_id, fallback=entry)
        records[track_id] = record
        playlist_ids.append(track_id)
        keys.append(record["key"])

    playlist_ids = list(dict.fromkeys(playlist_ids))
    keys = list(dict.fromkeys(keys))
    added = store.add_tracks(
        records, playlist_name=parsed["name"], track_ids=playlist_ids, source=source
    )
    log.info(
        "M3U %s: %d entries, %d new, %d already in the library, %d streaming, %d missing",
        parsed["name"], len(entries), len(records), matched, streaming, len(missing),
    )
    return {
        "playlist": parsed["name"],
        "entries": len(entries),
        "added": len(records),
        "matched": matched,
        "streaming": streaming,
        "missing": missing,
        "keys": keys,
        "library": added,
    }


# --------------------------------------------------------------- watch folder


def _audio_files(folder: Path, recursive: bool) -> list[Path]:
    try:
        walker = folder.rglob("*") if recursive else folder.iterdir()
        return sorted(
            path for path in walker
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
        )
    except OSError as exc:
        raise PlaylistError(f"{folder} could not be read: {exc}") from exc


def scan(store, *, force: bool = False) -> dict:
    """One pass over the watch folder: add files not seen before.

    ``force`` re-imports every file in the folder, which is how a user asks for
    tags that changed outside Parity to be picked up.  A missing or switched-off
    folder is a quiet, reportable no-op: the background thread must not raise.
    """
    settings = store.watch_settings()
    folder_text = str(settings.get("path") or "").strip()
    if not folder_text:
        return {"enabled": bool(settings.get("enabled")), "scanned": 0, "added": 0, "note": "No folder chosen."}
    folder = Path(folder_text).expanduser()
    if not folder.is_dir():
        return {
            "enabled": bool(settings.get("enabled")),
            "scanned": 0,
            "added": 0,
            "note": f"No folder at {folder}; nothing was imported.",
        }
    recursive = bool(settings.get("recursive", True))
    files = _audio_files(folder, recursive)
    seen = {_normal(path) for path in settings.get("seen") or []}
    fresh = [path for path in files if force or _normal(path) not in seen]
    playlist_name = str(settings.get("playlist") or f"Watch: {folder.name}")
    existing = _existing_by_path(store)
    taken = set(store.tracks_by_key())
    records: dict[str, dict] = {}
    playlist_ids: list[str] = []
    added: list[dict] = []
    for path in fresh:
        normal = _normal(path)
        found = existing.get(normal)
        if found is not None and not force:
            continue
        if found is not None:
            # A force scan refreshes the row the library already has, so the
            # playlist points at the same track key.
            track_id = str(found.get("id"))
        else:
            track_id = _unique_id("watch", str(path), taken)
        record = track_record(path, track_id)
        records[track_id] = record
        playlist_ids.append(track_id)
        label = f"{record.get('artist') or ''} - {record.get('title') or ''}".strip(" -")
        added.append({"key": record["key"], "label": label or record["key"], "path": str(path)})

    playlist_ids = list(dict.fromkeys(playlist_ids))
    if records:
        store.add_tracks(
            records,
            playlist_name=playlist_name,
            track_ids=playlist_ids,
            source=f"watch: {folder}",
            append_playlist=True,
        )
    store.set_watch_settings(
        {
            "seen": sorted(seen | {_normal(path) for path in files}),
            "last_scan": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_added": len(records),
            "last_error": "",
        }
    )
    log.info(
        "watch %s: %d files, %d new, %d added to %r",
        folder, len(files), len(fresh), len(records), playlist_name,
    )
    return {
        "enabled": bool(settings.get("enabled")),
        "scanned": len(files),
        "added": len(records),
        "added_tracks": added,
        "playlist": playlist_name,
        "folder": str(folder),
        "note": "" if files else "That folder holds no audio files.",
    }


def status(store) -> dict:
    """The watch folder's settings, counts and what the scanner can read."""
    settings = store.watch_settings()
    folder_text = str(settings.get("path") or "")
    folder = Path(folder_text).expanduser() if folder_text else None
    probe = ingest.status()
    return {
        "enabled": bool(settings.get("enabled")),
        "path": folder_text,
        "exists": bool(folder and folder.is_dir()),
        "recursive": bool(settings.get("recursive", True)),
        "playlist": settings.get("playlist") or (f"Watch: {folder.name}" if folder else ""),
        "interval": float(settings.get("interval") or DEFAULT_INTERVAL),
        "last_scan": settings.get("last_scan") or "",
        "last_added": int(settings.get("last_added") or 0),
        "last_error": settings.get("last_error") or "",
        "seen": len(settings.get("seen") or []),
        "ffprobe": probe.get("ffprobe"),
        "available": bool(probe.get("ffprobe")),
        "error": probe.get("error"),
    }


def configure(store, payload: dict) -> dict:
    """Save the watch folder's settings; validates a folder that is given.

    Turning it on takes effect immediately (the API also starts the thread), and
    pointing it at a different folder forgets what the old one had seen, so the
    first pass over the new folder imports what is in it.
    """
    values = dict(payload or {})
    current = store.watch_settings()
    path_text = str(values.get("path", current.get("path", "")) or "").strip()
    if path_text:
        folder = Path(path_text).expanduser()
        if not folder.is_dir():
            raise PlaylistError(f"There is no folder at {folder}")
        path_text = str(folder.resolve())
    settings = {
        "path": path_text,
        "enabled": bool(values.get("enabled", current.get("enabled", False))),
        "recursive": bool(values.get("recursive", current.get("recursive", True))),
        "playlist": str(values.get("playlist", current.get("playlist", "")) or "").strip(),
        "interval": max(3.0, min(3600.0, float(values.get("interval", current.get("interval") or DEFAULT_INTERVAL)))),
    }
    previous_path = str(current.get("path") or "")
    if previous_path and previous_path != path_text:
        settings["seen"] = []
    saved = store.set_watch_settings(settings)
    log.info("watch folder %s (%s)", "enabled" if settings["enabled"] else "off", path_text or "-")
    return saved
