"""Local persistence.

Everything the user edits (energy overrides, saved sets) plus the imported
library metadata lives in a single JSON file under ``data/``.  The original
rekordbox XML is kept alongside it so exports can reproduce tracks losslessly.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

from . import structure


def default_data_dir() -> Path:
    override = os.environ.get("PARITY_DATA")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "data"


# Bumped when stored track records gain derived fields; older stores are
# repaired by re-parsing the XML (or re-reading the database) again.
#
# 3 -> 4: tracks now carry the cues rekordbox already had (``cues``), which the
# write policy needs to tell a hand-placed cue from one Parity wrote.
LIBRARY_SCHEMA = 4

#: The ingest options that are worth remembering between runs.
INGEST_SETTING_KEYS = {"source", "destination", "format", "recursive", "measure", "trim_silence", "overwrite"}


class Store:
    """Tiny JSON store with a process-wide lock (the HTTP server is threaded)."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.path = self.data_dir / "store.json"
        self.library_xml_path = self.data_dir / "library.xml"
        self._lock = threading.RLock()
        self._data = self._load()
        # Bumped on every mutation; library_version only when something that
        # changes derived track data (library or energy overrides) is written.
        self.version = 0
        self.library_version = 0
        # How many stored cue plans an older layout made unusable, so the UI can
        # say why the cues went away instead of just showing none.
        self.stale_cue_plans = 0
        self._load_stale_cue_plans()
        self._migrate_library()

    # ------------------------------------------------------------- basics

    def _empty(self) -> dict:
        return {
            "version": 1,
            "library": None,
            "energy_overrides": {},
            # Measured audio features + energy per track key (parity.audio),
            # kept beside the library so a re-import cannot silently lose an
            # analysis run that took a minute of ffmpeg time.
            "audio_analysis": {},
            # Waveform peaks per track key (parity.waveform).  Cached for the
            # same reason: reading a track's file costs an ffmpeg decode, and
            # the cue editor wants the picture the moment it opens.
            "waveforms": {},
            # Tempo, key and beat grid measured from the audio
            # (parity.analysis), kept beside the library like the energy.
            "track_analysis": {},
            "cue_plans": {},
            "sets": [],
            "stale_cue_plans": 0,
            # What a write does with a track that already has cues of its own
            # ("overwrite" | "keep"), plus the per-track exceptions to it.
            "cue_policy": "overwrite",
            "cue_overrides": {},
            # Where the last ingest run read from and wrote to, so the folder
            # panel opens where the user left it (see :mod:`parity.ingest`).
            "ingest": {},
            # The watch folder (parity.watchfolder): where it is, whether it is
            # on, and which files it has already imported.
            "watch": {},
        }

    def _migrate_library(self) -> None:
        """Repair records written by an older version of Parity.

        New derived fields (e.g. whether a track is streaming-only, or where its
        rekordbox analysis lives) are added by re-parsing the stored XML -- or by
        re-reading master.db when that is where the library came from, so an
        existing library picks them up without the user re-importing anything.
        """
        library = self._data.get("library")
        if not library:
            return
        # Runs regardless of the schema version: a library imported before the
        # streaming-name check existed is repaired in place, which preserves
        # edits the user made rather than re-importing over them.
        if self._scrub_encrypted_metadata(library):
            self.library_version += 1
            self.save()
        if library.get("schema", 1) >= LIBRARY_SCHEMA:
            return
        if library.get("source_kind") == "db":
            if self.refresh_library_from_db():
                return
        if self.library_xml_path.exists():
            self.refresh_library_from_xml()

    def _scrub_encrypted_metadata(self, library: dict) -> bool:
        """Replace rekordbox-encrypted streaming names in stored records."""
        from . import rekordbox as rb

        changed = False
        for record in (library.get("tracks") or {}).values():
            if isinstance(record, dict) and rb.scrub_metadata(record):
                changed = True
        return changed

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError):
            return self._empty()
        base = self._empty()
        base.update({k: v for k, v in data.items() if k in base})
        return base

    def _load_stale_cue_plans(self) -> None:
        """Discard cue plans written by an older cue layout.

        How many hot cues there are and what each slot means is Parity's own
        decision, so a plan from an older version is not one the current writers
        can honour -- a four-cue plan written today would leave half the pads
        dark.  The plan is dropped rather than patched: the only honest fix is a
        fresh analysis, which needs rekordbox's phrase data anyway.

        The count is saved with the store rather than only held in memory.  The
        process that reads the library at startup builds its own Store, and the
        one that serves the UI builds a second, so a in-memory-only count would
        be reported by the wrong instance and the UI would say "no cues yet"
        instead of why they went.
        """
        plans = self._data.get("cue_plans") or {}
        fresh = {
            key: plan
            for key, plan in plans.items()
            if int(plan.get("planner", 1) or 1) >= structure.CUE_PLAN_VERSION
        }
        dropped = len(plans) - len(fresh)
        total = int(self._data.get("stale_cue_plans") or 0) + dropped
        self.stale_cue_plans = total
        self._data["stale_cue_plans"] = total
        if dropped:
            self._data["cue_plans"] = fresh
            self.save()

    def save(self) -> None:
        with self._lock:
            self.version += 1
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(self._data, handle, separators=(",", ":"))
            temp.replace(self.path)

    # ----------------------------------------------------------- library

    def set_library(self, library, xml_text: str | None, source: str = "", source_kind: str | None = None) -> None:
        """Store a parsed library.

        ``xml_text`` is the original export when the library came from XML.  A
        direct master.db read has no XML of its own; a previously stored export
        is **left on disk** (it is the user's data) but stops being used for
        exports, which :meth:`library_xml` arranges.  Copying its stale
        ``<TRACK>`` elements would describe tracks the database now describes
        differently.
        """
        with self._lock:
            self.library_version += 1
            self.data_dir.mkdir(parents=True, exist_ok=True)
            kind = source_kind or getattr(library, "source_kind", "xml") or "xml"
            if xml_text:
                self.library_xml_path.write_text(xml_text, encoding="utf-8")
            self._data["library"] = {
                "schema": LIBRARY_SCHEMA,
                "source_kind": kind,
                "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": source,
                "product": library.product,
                "track_count": len(library.tracks),
                "tracks": {tid: track.to_dict() for tid, track in library.tracks.items()},
                "playlists": [playlist.to_dict() for playlist in library.playlists],
            }
            # Drop overrides and cue plans for tracks that vanished.
            valid = {t["key"] for t in self._data["library"]["tracks"].values()}
            self._data["energy_overrides"] = {
                k: v for k, v in self._data["energy_overrides"].items() if k in valid
            }
            self._data["cue_plans"] = {
                k: v for k, v in (self._data.get("cue_plans") or {}).items() if k in valid
            }
            self._data["cue_overrides"] = {
                k: v for k, v in (self._data.get("cue_overrides") or {}).items() if k in valid
            }
            self._data["audio_analysis"] = {
                k: v for k, v in (self._data.get("audio_analysis") or {}).items() if k in valid
            }
            self._data["waveforms"] = {
                k: v for k, v in (self._data.get("waveforms") or {}).items() if k in valid
            }
            self._data["track_analysis"] = {
                k: v for k, v in (self._data.get("track_analysis") or {}).items() if k in valid
            }
            self.save()

    def library(self) -> dict | None:
        with self._lock:
            return self._data.get("library")

    def tracks_by_key(self) -> dict[str, dict]:
        with self._lock:
            library = self._data.get("library") or {}
            tracks = library.get("tracks") or {}
            return {track["key"]: track for track in tracks.values()}

    def refresh_library(self) -> bool:
        """Re-read the library from the source it originally came from.

        Deliberately does not switch sources: a library that came from master.db
        must not silently become a stale XML import because the database is
        temporarily unreadable.
        """
        if self.library_origin()["source_kind"] == "db":
            return self.refresh_library_from_db()
        return self.refresh_library_from_xml()

    def refresh_library_from_xml(self) -> bool:
        """Re-parse the stored XML, e.g. after the user re-exported rekordbox.

        The original source path is kept: the stored XML is a copy of that file,
        so remembering where it came from is more useful than "stored".
        """
        if not self.library_xml_path.exists():
            return False
        from . import rekordbox as rb

        previous = (self.library() or {}).get("source") or "stored"
        xml_text = self.library_xml_path.read_text(encoding="utf-8", errors="replace")
        library = rb.parse_collection(xml_text, source=previous)
        library.source_kind = "xml"
        self.set_library(library, xml_text, source=previous, source_kind="xml")
        return True

    def refresh_library_from_db(self) -> bool:
        """Re-read master.db, e.g. to pick up tracks added since the import."""
        from . import rxdb

        previous = (self.library() or {}).get("source") or "master.db"
        try:
            library = rxdb.read_library()
        except rxdb.MasterDbUnavailable:
            return False
        self.set_library(library, None, source=previous, source_kind="db")
        return True

    def library_origin(self) -> dict:
        library = self.library() or {}
        return {
            "source_kind": library.get("source_kind", "xml"),
            "has_xml": self.library_xml_path.exists(),
        }

    def library_xml(self) -> str | None:
        """The source export, or ``None`` when there isn't a usable one.

        A library imported from master.db never reports the stored XML, even if a
        file from an earlier import is still there: its ``<TRACK>`` elements are
        a snapshot of a different library.
        """
        if self.library_origin()["source_kind"] == "db":
            return None
        if not self.library_xml_path.exists():
            return None
        return self.library_xml_path.read_text(encoding="utf-8", errors="replace")

    # -------------------------------------------------------------- cues

    def cue_plans(self) -> dict[str, dict]:
        """Generated cue plans by track key (see :mod:`parity.structure`)."""
        with self._lock:
            return dict(self._data.get("cue_plans") or {})

    def set_cue_plans(self, plans: dict[str, dict], replace: bool = False) -> int:
        with self._lock:
            self.library_version += 1
            current = {} if replace else dict(self._data.get("cue_plans") or {})
            current.update({str(k): v for k, v in plans.items()})
            self._data["cue_plans"] = current
            # Fresh plans answer the "analyse again" note, whatever it said.
            self._data["stale_cue_plans"] = 0
            self.stale_cue_plans = 0
            self.save()
            return len(current)

    def clear_cue_plans(self) -> None:
        with self._lock:
            self.library_version += 1
            self._data["cue_plans"] = {}
            self._data["stale_cue_plans"] = 0
            self.stale_cue_plans = 0
            self.save()

    # -------------------------------------------------------------- cues

    def cue_policy(self) -> str:
        """The write policy for tracks that already have cues of their own."""
        with self._lock:
            policy = str(self._data.get("cue_policy") or "overwrite").strip().lower()
            return policy if policy in {"overwrite", "keep"} else "overwrite"

    def cue_overrides(self) -> dict[str, str]:
        """Per-track exceptions to :meth:`cue_policy`, by track key."""
        with self._lock:
            return {
                str(key): str(value)
                for key, value in (self._data.get("cue_overrides") or {}).items()
                if str(value) in {"overwrite", "keep"}
            }

    def set_cue_policy(self, policy: str | None = None, overrides: dict[str, str] | None = None) -> dict:
        """Save the cue policy and/or per-track exceptions.

        An override of ``None``/``"auto"`` drops the exception, so the track
        follows the global setting again -- the same shape as the energy
        overrides it sits next to.
        """
        with self._lock:
            self.library_version += 1
            if policy is not None:
                wanted = str(policy).strip().lower()
                self._data["cue_policy"] = wanted if wanted in {"overwrite", "keep"} else "overwrite"
            current = self._data.setdefault("cue_overrides", {})
            for key, value in (overrides or {}).items():
                if value in (None, "", "auto"):
                    current.pop(str(key), None)
                elif str(value) in {"overwrite", "keep"}:
                    current[str(key)] = str(value)
            self.save()
            return {"cue_policy": self._data["cue_policy"], "cue_overrides": dict(current)}

    def clear_cue_policy(self) -> None:
        with self._lock:
            self._data["cue_overrides"] = {}
            self.save()

    # -------------------------------------------------- imported tracks

    def add_tracks(
        self,
        records: dict[str, dict],
        *,
        playlist_name: str = "",
        track_ids: list[str] | None = None,
        source: str = "",
        append_playlist: bool = False,
    ) -> dict:
        """Add imported tracks and a playlist to the stored library.

        Used by the M3U import and the watch folder, which can be the *first*
        thing a library comes from.  Existing tracks are not touched: the
        caller has already matched them by path, and an M3U import must not
        overwrite what rekordbox said about a track.  A playlist with the same
        name is updated in place (a re-import replaces its order) rather than
        stacked beside the old one.
        """
        with self._lock:
            records = {str(tid): record for tid, record in (records or {}).items()}
            library = self._data.get("library")
            if not library:
                library = {
                    "schema": LIBRARY_SCHEMA,
                    "source_kind": "files",
                    "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source": source or "imported playlist",
                    "product": {},
                    "track_count": 0,
                    "tracks": {},
                    "playlists": [],
                }
                self._data["library"] = library
            tracks = library.setdefault("tracks", {})
            tracks.update(records)
            library["track_count"] = len(tracks)
            if source and not library.get("source"):
                library["source"] = source

            ids = [str(tid) for tid in (track_ids or []) if str(tid) in tracks]
            playlist = None
            if playlist_name:
                node = {
                    "id": "pl:" + hashlib.sha1(playlist_name.encode("utf-8")).hexdigest()[:12],
                    "name": playlist_name,
                    "count": len(ids),
                    "total": len(ids),
                    "is_folder": False,
                    "track_ids": ids,
                    "children": [],
                }
                playlists = library.setdefault("playlists", [])
                existing = next(
                    (item for item in playlists if item.get("name") == playlist_name), None
                )
                if existing is None:
                    playlists.append(node)
                    existing = node
                elif append_playlist:
                    # The watch folder adds to its playlist as new files land;
                    # an M3U re-import replaces the order it describes.
                    merged = list(dict.fromkeys(list(existing.get("track_ids") or []) + ids))
                    existing["track_ids"] = merged
                    existing["count"] = len(merged)
                    existing["total"] = len(merged)
                else:
                    existing.update(node)
                playlist = existing
            self.library_version += 1
            self.save()
            return {
                "added": len(records),
                "track_count": len(tracks),
                "playlist": dict(playlist) if playlist else None,
                "ids": ids,
            }

    # ------------------------------------------------------------ metadata

    #: Fields an edit may change on a stored library record.  The shape follows
    #: the record itself (``comments`` / ``tonality``), not the editor's labels.
    EDITABLE_FIELDS = {
        "title", "artist", "album", "genre", "comments",
        "year", "track_number", "bpm", "tonality",
    }

    def update_tracks(self, updates: dict[str, dict]) -> int:
        """Apply metadata edits to stored records, by track key.

        The library record is Parity's copy of what the DJ app said; changing it
        here changes what the pool shows and what an export writes.  A key that
        is not in the loaded library is skipped, so a stale editor panel cannot
        invent a track.
        """
        with self._lock:
            library = self._data.get("library") or {}
            records = library.get("tracks") or {}
            by_key = {record.get("key"): record for record in records.values()}
            changed = 0
            for key, fields in (updates or {}).items():
                record = by_key.get(str(key))
                if record is None:
                    continue
                for name, value in (fields or {}).items():
                    if name not in self.EDITABLE_FIELDS:
                        continue
                    record[name] = value
                changed += 1
            if changed:
                self.library_version += 1
                self.save()
            return changed

    # ------------------------------------------------------------- energy

    def overrides(self) -> dict[str, float]:
        with self._lock:
            return dict(self._data.get("energy_overrides") or {})

    # --------------------------------------------------------------- audio

    def audio_analysis(self) -> dict[str, dict]:
        """Measured audio features by track key (see :mod:`parity.audio`)."""
        with self._lock:
            return dict(self._data.get("audio_analysis") or {})

    def set_audio_analysis(self, entries: dict[str, dict], replace: bool = False) -> int:
        """Store measured features; ``replace`` throws the old run away first."""
        with self._lock:
            self.library_version += 1
            current = {} if replace else dict(self._data.get("audio_analysis") or {})
            current.update({str(k): v for k, v in entries.items()})
            self._data["audio_analysis"] = current
            self.save()
            return len(current)

    def clear_audio_analysis(self) -> None:
        with self._lock:
            self.library_version += 1
            self._data["audio_analysis"] = {}
            self.save()

    # ------------------------------------------------------ tempo / key

    def track_analysis(self) -> dict[str, dict]:
        """Measured BPM, key and beat grid by track key (parity.analysis)."""
        with self._lock:
            return dict(self._data.get("track_analysis") or {})

    def set_track_analysis(self, entries: dict[str, dict], replace: bool = False) -> int:
        with self._lock:
            self.library_version += 1
            current = {} if replace else dict(self._data.get("track_analysis") or {})
            current.update({str(k): v for k, v in entries.items()})
            self._data["track_analysis"] = current
            self.save()
            return len(current)

    def clear_track_analysis(self) -> None:
        with self._lock:
            self.library_version += 1
            self._data["track_analysis"] = {}
            self.save()

    # ----------------------------------------------------------- waveforms

    def waveforms(self) -> dict[str, dict]:
        """Cached waveform peaks by track key (see :mod:`parity.waveform`)."""
        with self._lock:
            return dict(self._data.get("waveforms") or {})

    def set_waveform(self, track_key: str, entry: dict) -> dict:
        """Cache one track's peaks, replacing any profile already there."""
        with self._lock:
            waveforms = self._data.setdefault("waveforms", {})
            waveforms[str(track_key)] = entry
            self.save()
            return entry

    def clear_waveforms(self) -> None:
        with self._lock:
            self._data["waveforms"] = {}
            self.save()

    def set_override(self, track_key: str, value: float | None) -> None:
        with self._lock:
            self.library_version += 1
            overrides = self._data.setdefault("energy_overrides", {})
            if value is None:
                overrides.pop(track_key, None)
            else:
                overrides[track_key] = float(value)
            self.save()

    # --------------------------------------------------------------- sets

    def list_sets(self) -> list[dict]:
        with self._lock:
            return list(self._data.get("sets") or [])

    def save_set(self, payload: dict) -> dict:
        with self._lock:
            sets = self._data.setdefault("sets", [])
            set_id = payload.get("id")
            record = {
                "id": set_id or f"set-{int(time.time() * 1000)}",
                "name": payload.get("name") or "Untitled set",
                "playlist_id": payload.get("playlist_id"),
                "order": payload.get("order") or [],
                "locks": {str(k): v for k, v in (payload.get("locks") or {}).items()},
                "config": payload.get("config") or {},
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            existing = next((i for i, item in enumerate(sets) if item["id"] == record["id"]), None)
            if existing is None:
                sets.append(record)
            else:
                sets[existing] = record
            self.save()
            return record

    def delete_set(self, set_id: str) -> bool:
        with self._lock:
            sets = self._data.setdefault("sets", [])
            before = len(sets)
            self._data["sets"] = [item for item in sets if item["id"] != set_id]
            changed = len(self._data["sets"]) != before
            if changed:
                self.save()
            return changed

    # -------------------------------------------------------------- ingest

    def ingest_settings(self) -> dict:
        """The last folder, destination and options used to tidy a download folder."""
        with self._lock:
            return dict(self._data.get("ingest") or {})

    def set_ingest_settings(self, values: dict) -> dict:
        with self._lock:
            saved = dict(self._data.get("ingest") or {})
            for key, value in (values or {}).items():
                if key not in INGEST_SETTING_KEYS:
                    continue
                if value is None or value == "":
                    saved.pop(key, None)
                else:
                    saved[key] = value
            self._data["ingest"] = saved
            self.save()
            return dict(saved)

    # --------------------------------------------------------------- watch

    WATCH_KEYS = {
        "path", "enabled", "recursive", "playlist", "interval",
        "seen", "last_scan", "last_added", "last_error",
    }

    def watch_settings(self) -> dict:
        with self._lock:
            return dict(self._data.get("watch") or {})

    def set_watch_settings(self, values: dict) -> dict:
        with self._lock:
            saved = dict(self._data.get("watch") or {})
            for key, value in (values or {}).items():
                if key not in self.WATCH_KEYS:
                    continue
                if key == "seen":
                    saved["seen"] = sorted({str(path) for path in (value or [])})
                elif value is None or value == "":
                    saved.pop(key, None)
                else:
                    saved[key] = value
            self._data["watch"] = saved
            self.save()
            return dict(saved)

    def reset(self) -> None:
        with self._lock:
            self.version += 1
            self.library_version += 1
            self._data = self._empty()
            if self.path.exists():
                self.path.unlink()
            if self.library_xml_path.exists():
                self.library_xml_path.unlink()

    def export_backup(self, destination: Path) -> Path:
        with self._lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                shutil.copy2(self.path, destination)
            return destination
