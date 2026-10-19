"""Local HTTP server.

Binds to 127.0.0.1 only -- this is a private tool, there is no auth because
there is no network exposure.  Serves the static UI plus a small JSON API.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import (
    analysis as analysis_mod,
    audio as audio_mod,
    autopilot,
    builder,
    camelot,
    duplicates as duplicates_mod,
    energy as energy_mod,
    exporting,
    ingest as ingest_mod,
    metadata as metadata_mod,
    poolfilter,
    preflight as preflight_mod,
    rekordbox,
    rxdb,
    structure as structure_mod,
    tags as tags_mod,
    watchfolder,
    waveform as waveform_mod,
    writedb,
)
from .store import Store

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY = 128 * 1024 * 1024  # large collections export to big XML files

#: Content types for the preview endpoint.  A browser will not play an
#: ``application/octet-stream`` in an <audio> element, so the common rekordbox
#: formats are spelled out rather than guessed.
AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wma": "audio/x-ms-wma",
}

#: Read size while streaming a preview out.
AUDIO_CHUNK = 64 * 1024
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")

LOG = logging.getLogger("parity.server")
CUE_LOG = logging.getLogger("parity.cues")
#: One line per track whose audio is measured, so a run can be audited without
#: re-reading the files.
AUDIO_LOG = logging.getLogger("parity.audio")
#: One line per file the ingest pipeline converts.
INGEST_LOG = logging.getLogger("parity.ingest")
#: One line per waveform read, so a profile that took a second to build can be
#: explained instead of just appearing.
WAVE_LOG = logging.getLogger("parity.waveform")


def configure_logging(level: str | None = None) -> None:
    """One place to turn on the pipeline logs the product promises."""
    resolved = (level or os.environ.get("PARITY_LOG") or "INFO").upper()
    root = logging.getLogger("parity")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[parity] %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


def _safe_filename(name: str, fallback: str = "parity-set") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", (name or "").strip())
    cleaned = cleaned.replace(" ", "-").strip("-.")
    return cleaned or fallback


CUE_POLICIES = ("overwrite", "keep")


def cue_policy_for(key: str, policy: str, overrides: dict[str, str] | None) -> str:
    """The write policy for one track: the per-track choice, else the global one."""
    chosen = (overrides or {}).get(key)
    if chosen in CUE_POLICIES:
        return chosen
    return policy if policy in CUE_POLICIES else "overwrite"


def carry_locked_cues(previous: dict | None, plan: dict) -> int:
    """Put hand-moved, locked cues back onto a freshly generated plan.

    "Analyse cues" rebuilds every cue from rekordbox's phrase map, which would
    otherwise undo the one thing on the screen the user decided themselves.  A
    cue that was moved by hand and left locked goes back at its own timestamp:
    the slot keeps the role and colour the new analysis gave it, and the plan
    says why the position is not the phrase boundary.  Returns how many were put
    back, so a run can report what it did not overwrite.
    """
    locked = {
        cue.get("slot"): cue
        for cue in ((previous or {}).get("hot_cues") or [])
        if cue.get("locked")
    }
    if not locked:
        return 0
    kept = 0
    cues: list[dict] = []
    for cue in plan.get("hot_cues") or []:
        held = locked.get(cue.get("slot"))
        if held is None:
            cues.append(cue)
            continue
        ms = int(held.get("ms") or 0)
        merged = dict(cue)
        merged.update(
            {
                "ms": ms,
                "seconds": round(ms / 1000.0, 3),
                "locked": True,
                "edited": True,
                "reason": f"kept at {_mmss(ms)}: moved by hand and locked",
            }
        )
        cues.append(merged)
        kept += 1
    plan["hot_cues"] = cues
    if kept:
        note = str(plan.get("note") or "")
        kept_note = f"{kept} hand-moved cue(s) kept where you put them"
        plan["note"] = f"{note}; {kept_note}" if note else kept_note
    return kept


def _mmss(ms: int) -> str:
    """A cue position as ``m:ss.mmm``: how the editor reads it out."""
    total = max(0, int(ms))
    minutes, rest = divmod(total, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{minutes}:{seconds:02d}.{millis:03d}"


def existing_cue_summary(cues: list[dict]) -> dict:
    """Count the cues a track already has, keeping Parity's own apart.

    The distinction is the whole point of the write policy: rekordbox's own
    memory cue and cues the DJ placed by hand are "yours" and can be left alone,
    while the eight pads Parity wrote last time are ours to replace -- a
    library full of Parity cues must not be frozen by choosing "keep".
    """
    hot = [cue for cue in cues if str(cue.get("kind", "")) == "hot"]
    memory = [cue for cue in cues if str(cue.get("kind", "")) == "memory"]
    theirs = [cue for cue in hot if not structure_mod.is_parity_cue(str(cue.get("name") or ""))]
    return {
        "total": len(cues),
        "hot": len(hot),
        "memory": len(memory),
        "parity": len(hot) - len(theirs),
        "user": len(theirs),
        "user_cues": [
            {"name": str(cue.get("name") or ""), "seconds": round(float(cue.get("start") or 0), 3)}
            for cue in sorted(theirs, key=lambda cue: float(cue.get("start") or 0))
        ],
    }


class Api:
    """Request handlers, separated from the HTTP plumbing so they are testable."""

    def __init__(self, store: Store):
        self.store = store
        self._views: dict[str, dict] = {}
        self._views_version = -1
        self._cue_plans: dict[str, dict] = {}
        self._analysis: dict[str, dict] = {}
        self._analysis_version = -1
        # Background watch-folder thread (see parity.watchfolder); None until
        # the feature is switched on.
        self._watch_thread = None
        self._watch_stop = threading.Event()

    # ---------------------------------------------------------- conversion

    def _analyses(self) -> dict[str, dict]:
        """Measured tempo/key/grid, memoised until the store changes."""
        if self._analysis_version != self.store.library_version:
            self._analysis = self.store.track_analysis()
            self._analysis_version = self.store.library_version
        return self._analysis

    def track_from_record(self, record: dict) -> rekordbox.Track:
        """Rebuild the parser's track object from a stored record.

        Where the library has no BPM or key, the values measured from the audio
        fill in, so every consumer -- the energy estimate, the cue fallback,
        the exported tempo, the builder -- sees the same numbers as the pool.
        """
        track = rekordbox.Track(track_id=str(record.get("id", "")))
        track.title = record.get("title", "")
        track.artist = record.get("artist", "")
        track.genre = record.get("genre", "")
        track.comments = record.get("comments", "")
        track.tonality = record.get("tonality", "")
        track.location = record.get("location") or record.get("path", "")
        track.bpm = float(record.get("bpm") or 0)
        track.duration = float(record.get("duration") or 0)
        track.rating = int(record.get("rating") or 0)
        track.analysed = bool(record.get("analysed"))
        track.analysis_path = record.get("analysis_path", "") or ""
        measured = self._analyses().get(record.get("key", "")) or {}
        if not track.bpm and measured.get("bpm"):
            track.bpm = float(measured["bpm"])
        if not track.tonality and measured.get("key"):
            track.tonality = str(measured["key"])
        track.sanitize_metadata()
        return track

    # ------------------------------------------------------------- helpers

    def track_view(
        self,
        record: dict,
        overrides: dict[str, float],
        cue_policy: str = "overwrite",
        cue_overrides: dict[str, str] | None = None,
        audio: dict[str, dict] | None = None,
    ) -> dict:
        key = record.get("key", "")
        track = self.track_from_record(record)

        override = overrides.get(key)
        measured = (audio or {}).get(key) or None
        if measured and int(measured.get("model", 0)) != audio_mod.MODEL_VERSION:
            # An older scoring model: keep the features for reference, but do not
            # let their energy override today's answer.
            measured = None
        estimate = energy_mod.estimate(
            track, override, (measured or {}).get("energy")
        )
        code = camelot.parse_key(track.tonality)
        analysed = self._analyses().get(key) or None
        plan = self._cue_plans.get(key) or {}
        hot = plan.get("hot_cues") or []
        memory = plan.get("memory_cues") or []
        existing = existing_cue_summary(record.get("cues") or [])

        view = dict(record)
        view.update(
            {
                "key": key,
                "bpm": track.bpm,
                "stars": record.get("stars", track.stars),
                "camelot": code,
                "musical_key": camelot.CAMELOT_TO_KEY.get(code) if code else None,
                # What the audio said, and whether it is what the pool shows.
                "analysis": analysed,
                "analysed_bpm": (analysed or {}).get("bpm"),
                "analysed_key": (analysed or {}).get("camelot"),
                "key_source": "library" if record.get("tonality") else (
                    "analysed" if code else ""
                ),
                "bpm_source": "library" if record.get("bpm") else (
                    "analysed" if track.bpm else ""
                ),
                "energy": estimate.value,
                "energy_source": estimate.source,
                "energy_confidence": estimate.confidence,
                "energy_override": override is not None,
                "energy_audio": (measured or {}).get("energy"),
                # What the measurement saw, so the UI can explain a score rather
                # than just assert it.
                "audio": measured,
                "needs_review": estimate.needs_review,
                "streaming": bool(record.get("streaming")),
                "metadata_locked": bool(record.get("metadata_locked")),
                "label": track.display,
                "cues": {
                    "hot": len(hot),
                    "memory": len(memory),
                    "analysed": bool(plan),
                    # How many of those were moved by hand in the cue editor: the
                    # ones a later analysis run leaves exactly where they are.
                    "locked": sum(1 for cue in hot if cue.get("locked")),
                    "confidence": (plan.get("structure") or {}).get("confidence", ""),
                    "source": (plan.get("structure") or {}).get("source", ""),
                    "note": plan.get("note", ""),
                },
                # The cues rekordbox already has on the track, split into
                # Parity's own and the DJ's, which is what the write policy
                # turns on.
                "existing_cues": existing,
                "cue_policy": cue_policy_for(key, cue_policy, cue_overrides),
            }
        )
        return view

    def all_views(self) -> dict[str, dict]:
        """Enriched view of every track, memoised until the store changes.

        Large playlists (thousands of tracks) make this worth caching: the
        estimate for every track does not need to be redone per request. The
        returned dicts are shared -- treat them as read-only.
        """
        if self._views_version == self.store.library_version and self._views:
            return self._views
        overrides = self.store.overrides()
        self._cue_plans = self.store.cue_plans()
        policy = self.store.cue_policy()
        track_policies = self.store.cue_overrides()
        measured = self.store.audio_analysis()
        self._views = {
            key: self.track_view(record, overrides, policy, track_policies, measured)
            for key, record in self.store.tracks_by_key().items()
        }
        self._views_version = self.store.library_version
        return self._views

    def candidate(self, view: dict) -> builder.Candidate:
        return builder.Candidate(
            key=view["key"],
            title=view.get("title", ""),
            artist=view.get("artist", ""),
            genre=view.get("genre", ""),
            camelot=view.get("camelot"),
            bpm=float(view.get("bpm") or 0),
            energy=float(view.get("energy") or 5),
            duration=float(view.get("duration") or 0),
            confidence=view.get("energy_confidence", ""),
        )

    def result_payload(self, result: builder.BuildResult, views: dict[str, dict], config: builder.BuildConfig) -> dict:
        order = [views[key] for key in result.order if key in views]
        return {
            "order": order,
            "order_keys": result.order,
            "transitions": [asdict(t) for t in result.transitions],
            "targets": result.targets,
            "cost": result.cost,
            "warnings": result.warnings,
            "stats": result.stats,
            "config": config.to_dict(),
        }

    # ------------------------------------------------------------ endpoints

    def state(self) -> dict:
        library = self.store.library()
        overrides = self.store.overrides()
        sets = self.store.list_sets()
        playlists = _public_playlists((library or {}).get("playlists") or [])
        records = ((library or {}).get("tracks") or {})
        streaming = sum(1 for record in records.values() if record.get("streaming"))
        analysed = sum(1 for record in records.values() if record.get("analysed"))
        views = list(self.all_views().values()) if library else []
        origin = self.store.library_origin()
        policy = self.store.cue_policy()
        track_policies = self.store.cue_overrides()
        with_user_cues = sum(1 for view in views if (view.get("existing_cues") or {}).get("user"))
        low_confidence = sum(1 for view in views if str(view.get("energy_confidence")) == "low")
        return {
            "has_library": bool(library),
            "source": (library or {}).get("source", ""),
            "source_kind": origin["source_kind"],
            "has_source_xml": origin["has_xml"],
            "imported_at": (library or {}).get("imported_at", ""),
            "track_count": (library or {}).get("track_count", 0),
            "streaming_count": streaming,
            "analysed_count": analysed,
            "cue_plan_count": len(self.store.cue_plans()),
            "stale_cue_plans": self.store.stale_cue_plans,
            "local_count": len(records) - streaming,
            "folders": poolfilter.folder_options(views) if views else [],
            "playlists": playlists,
            "rekordbox": {
                "pyrekordbox": rxdb.extras_status(),
                "db_path": str(rxdb.master_db_path()),
                "db_exists": rxdb.master_db_path().is_file(),
            },
            "saved_sets": [
                {"id": s["id"], "name": s["name"], "count": len(s.get("order") or []), "updated_at": s.get("updated_at")}
                for s in sets
            ],
            "energy_overrides": len(overrides),
            "low_confidence_energy": low_confidence,
            "audio": self.audio_status() if library else {"available": audio_mod.status()["available"]},
            "analysis": self.analysis_status(),
            "watch": watchfolder.status(self.store),
            "cue_policy": policy,
            "cue_override_count": len(track_policies),
            "tracks_with_user_cues": with_user_cues,
            "tracks_with_cues": sum(1 for view in views if (view.get("existing_cues") or {}).get("hot")),
            "templates": {
                name: points for name, points in builder.TEMPLATES.items()
            },
            "defaults": {
                "template": "journey",
                "length_tracks": 20,
                "energy_weight": 1.0,
                "key_weight": 1.0,
                "bpm_weight": 1.0,
                "bpm_tolerance": 6.0,
                "strictness": "normal",
                "artist_repeat_window": 3,
                "cue_policy": policy,
            },
            "cue_policies": list(CUE_POLICIES),
        }

    def import_xml(self, xml_text: str, source: str) -> dict:
        try:
            library = rekordbox.parse_collection(xml_text, source=source)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as a 400
            raise ValueError(f"Could not read that file as rekordbox XML: {exc}") from exc
        if not library.tracks:
            raise ValueError("That XML contains no tracks. Export with File > Export Collection in xml format.")
        library.source_kind = "xml"
        self.store.set_library(library, xml_text, source=source, source_kind="xml")
        return self.state()

    def tracks(self, playlist_id: str | None) -> dict:
        views = self.all_views()
        library = self.store.library()
        if not library:
            return {"tracks": [], "count": 0}

        if playlist_id:
            track_ids = _playlist_track_ids(library["playlists"], playlist_id)
            records = library["tracks"]
            ordered = []
            for track_id in track_ids:
                record = records.get(track_id)
                if record:
                    ordered.append(views[record["key"]])
            tracks = ordered
        else:
            tracks = list(views.values())
            tracks.sort(key=lambda t: (t.get("artist") or "", t.get("title") or ""))
        return {"tracks": tracks, "count": len(tracks)}

    def edit_track(self, payload: dict | None = None) -> dict:
        """Change one track's metadata, in the library record and optionally
        in the file's own tags.

        The order matters: the file is written first, so a file that cannot be
        written leaves the library record alone rather than disagreeing with the
        tags on disk.  ``write_tags`` is off unless asked -- editing what the
        pool shows is reversible, rewriting a music file is not.
        """
        payload = payload or {}
        key = str(payload.get("key") or "")
        if not key:
            raise ValueError("No track requested.")
        record = self.store.tracks_by_key().get(key)
        if record is None:
            raise ValueError("That track is not in the loaded library.")
        fields = metadata_mod.normalise(payload.get("fields") or {})
        written = None
        if payload.get("write_tags"):
            path = str(record.get("path") or "")
            if record.get("streaming") or not path:
                raise ValueError(
                    "That track is streaming only - there is no local file to tag."
                )
            written = metadata_mod.write_tags(path, fields)
        changed = self.store.update_tracks({key: fields})
        LOG.info(
            "metadata %s: %s%s",
            record.get("label") or record.get("title") or key,
            ", ".join(f"{name}={value!r}" for name, value in sorted(fields.items())),
            " (written to the file)" if written else " (library only)",
        )
        return {
            "ok": True,
            "changed": fields,
            "records": changed,
            "tags": written,
            "track": self.all_views().get(key),
        }

    def set_energy(self, payload: dict) -> dict:
        values = payload.get("values") or {}
        if "track_key" in payload:
            values[payload["track_key"]] = payload.get("value")
        for key, value in values.items():
            self.store.set_override(key, None if value in (None, "", "auto") else float(value))
        return {"ok": True, "energy_overrides": len(self.store.overrides())}

    def set_cue_policy(self, payload: dict | None = None) -> dict:
        """Save what a write does with a track that already has cues of its own.

        ``policy`` is the default for the whole library, ``values`` holds the
        per-track exceptions the set list's switch writes.
        """
        payload = payload or {}
        values = dict(payload.get("values") or {})
        if "track_key" in payload:
            values[str(payload["track_key"])] = payload.get("value")
        saved = self.store.set_cue_policy(payload.get("policy"), values or None)
        return {
            "ok": True,
            "cue_policy": saved["cue_policy"],
            "cue_override_count": len(saved["cue_overrides"]),
            "state": self.state(),
        }

    def build(self, payload: dict) -> dict:
        config = builder.BuildConfig.from_dict(payload.get("config") or {})
        views = self.all_views()
        pool_keys = payload.get("keys")
        if not pool_keys and payload.get("filter"):
            # The UI sends the filter it used for the pool preview, so a build
            # can never quietly reach past it into streaming or missing files.
            filter_config = poolfilter.FilterConfig.from_dict(payload["filter"])
            pool_keys = [view["key"] for view in self._filter(filter_config).kept]
        if not pool_keys and payload.get("playlist_id"):
            library = self.store.library() or {}
            records = library.get("tracks") or {}
            pool_keys = [
                records[tid]["key"]
                for tid in _playlist_track_ids(library.get("playlists") or [], payload["playlist_id"])
                if tid in records
            ]
        if not pool_keys:
            pool_keys = list(views.keys())

        pool = [self.candidate(views[key]) for key in pool_keys if key in views]
        if not pool:
            raise ValueError("No tracks selected.")
        if not config.length_tracks:
            config.length_tracks = min(len(pool), 20)
        result = builder.build_set(pool, config)
        return self.result_payload(result, views, config)

    def optimize(self, payload: dict) -> dict:
        config = builder.BuildConfig.from_dict(payload.get("config") or {})
        views = self.all_views()
        keys = payload.get("order") or []
        pool = [self.candidate(views[key]) for key in keys if key in views]
        if not pool:
            raise ValueError("Nothing to optimize.")
        config.length_tracks = len(pool)
        targets = payload.get("targets")
        result = builder.optimize_set(pool, config, targets=targets)
        return self.result_payload(result, views, config)

    # ------------------------------------------------------------ preview

    def audio_path(self, key: str) -> Path:
        """The file behind a track key, for the preview player.

        Only tracks in the loaded library resolve: the key has to match a stored
        record, so the endpoint cannot be pointed at an arbitrary path on disk.
        Streaming stubs and files that have gone away raise a message the UI can
        show as-is.
        """
        if not key:
            raise ValueError("No track requested.")
        record = self.store.tracks_by_key().get(key)
        if record is None:
            raise ValueError("That track is not in the loaded library.")
        path = record.get("path") or ""
        if record.get("streaming") or not path:
            raise ValueError("That track is streaming only - there is no local file to preview.")
        target = Path(path)
        if not target.is_file():
            raise ValueError(f"No file at {target}")
        return target

    def cue_plan(self, key: str) -> dict:
        """The stored cue plan for one track: the preview's start points, and
        everything the cue editor draws around them.

        The grid travels with it because the editor needs to know where the
        beats are to snap a dragged cue, and the phrase map because that is what
        the track's own shape is drawn from.  Milliseconds are sent beside
        seconds: a cue is placed to the millisecond and a rounded second is not
        enough to put one back where it was.
        """
        plan = self.store.cue_plans().get(key) or {}
        structure = plan.get("structure") or {}
        record = self.store.tracks_by_key().get(key) or {}
        duration_ms = int(structure.get("duration_ms") or 0)
        if not duration_ms:
            duration_ms = int(round(float(record.get("duration") or 0) * 1000))
        measured = self._analyses().get(key) or {}
        return {
            "key": key,
            "hot_cues": [
                {
                    "slot": cue.get("slot", ""),
                    "role": cue.get("role", ""),
                    "name": cue.get("name", ""),
                    "ms": int(cue.get("ms") or 0),
                    "seconds": round(int(cue.get("ms") or 0) / 1000.0, 3),
                    "confidence": cue.get("confidence", ""),
                    "colour": cue.get("colour", ""),
                    "rgb": cue.get("rgb") or [],
                    "reason": cue.get("reason", ""),
                    # True once the cue has been moved by hand, which is also what
                    # keeps it through the next analysis run.
                    "locked": bool(cue.get("locked")),
                    "edited": bool(cue.get("edited")),
                }
                for cue in (plan.get("hot_cues") or [])
            ],
            "analysed": bool(plan),
            "source": structure.get("source", ""),
            "note": plan.get("note", ""),
            "planner": plan.get("planner"),
            "bpm": structure.get("bpm") or float(record.get("bpm") or 0)
            or float(measured.get("bpm") or 0),
            "first_beat_ms": int(structure.get("first_beat_ms") or measured.get("first_beat_ms") or 0),
            "beats": int(structure.get("beats") or 0),
            "duration_ms": duration_ms,
            "phrases": [
                {
                    "role": phrase.get("role", ""),
                    "name": phrase.get("name", ""),
                    "start_ms": int(phrase.get("start_ms") or 0),
                    "end_ms": int(phrase.get("end_ms") or 0),
                    "bars": phrase.get("bars", 0),
                }
                for phrase in (structure.get("phrases") or [])
            ],
        }

    # -------------------------------------------------- waveform & cue edits

    def waveform(self, key: str, force: bool = False) -> dict:
        """Peaks for one track: read from the file the first time, then cached.

        Reading a file is the expensive part of the cue editor, so the profile
        is kept in the store next to the measured energy and re-used until the
        file itself changes (mtime and size are stored with it).
        """
        if not key:
            raise ValueError("No track requested.")
        path = self.audio_path(key)
        record = self.store.tracks_by_key().get(key) or {}
        try:
            stat = path.stat()
        except OSError as exc:
            raise ValueError(f"Could not read {path}: {exc}") from exc

        cached = self.store.waveforms().get(key) or {}
        if (
            not force
            and int(cached.get("model", 0)) == waveform_mod.MODEL_VERSION
            and cached.get("mtime") == stat.st_mtime
            and cached.get("size") == stat.st_size
        ):
            return {"key": key, "cached": True, "waveform": cached}

        started = time.perf_counter()
        try:
            result = waveform_mod.analyse(path, seconds=float(record.get("duration") or 0) or None)
        except audio_mod.AudioUnavailable as exc:
            # A missing ffmpeg or an unreadable file is the user's problem to
            # fix, not a server fault: it comes back as a plain message.
            raise ValueError(str(exc)) from exc
        entry = result.to_dict()
        entry.update({"mtime": stat.st_mtime, "size": stat.st_size, "path": str(path)})
        self.store.set_waveform(key, entry)
        WAVE_LOG.info(
            "%s | %d peaks/band | %s | %s | %.3fs of audio in %.0f ms",
            record.get("label") or path.name,
            entry["peaks_per_band"],
            entry["engine"],
            "truncated" if entry["truncated"] else "whole file",
            entry["seconds"],
            (time.perf_counter() - started) * 1000,
        )
        return {"key": key, "cached": False, "waveform": entry}

    def edit_cue(self, payload: dict | None = None) -> dict:
        """Move, lock or unlock one hot cue on a track's plan.

        The plan is what an export and a write read, so an edit here is what
        reaches rekordbox on the next write -- there is no separate copy of the
        cue list.  Moving a cue by hand locks it, because a position the user
        chose is the one thing a later "Analyse cues" must not silently undo;
        ``locked`` may also be passed on its own to lock or unlock.
        """
        payload = payload or {}
        key = str(payload.get("key") or "")
        slot = str(payload.get("slot") or "").strip().upper()
        if not key:
            raise ValueError("No track requested.")
        if not slot:
            raise ValueError("No cue slot requested.")
        plans = self.store.cue_plans()
        plan = plans.get(key)
        if not plan:
            raise ValueError("Analyse this track's cues first.")
        cue = next(
            (item for item in (plan.get("hot_cues") or []) if item.get("slot") == slot),
            None,
        )
        if cue is None:
            raise ValueError(f"That track has no {slot} cue to move.")

        record = self.store.tracks_by_key().get(key) or {}
        structure = plan.get("structure") or {}
        limit = int(structure.get("duration_ms") or 0)
        if not limit:
            limit = int(round(float(record.get("duration") or 0) * 1000))

        moved = False
        if payload.get("ms") is not None or payload.get("seconds") is not None:
            if payload.get("ms") is not None:
                ms = int(round(float(payload["ms"])))
            else:
                ms = int(round(float(payload["seconds"]) * 1000))
            ms = max(0, ms)
            if limit:
                ms = min(ms, limit)
            was = int(cue.get("ms") or 0)
            cue["ms"] = ms
            cue["seconds"] = round(ms / 1000.0, 3)
            cue["edited"] = True
            # The lock follows the hand, not the mouse button: a moved cue is the
            # user's decision, and the next analysis pass has to leave it alone.
            cue["locked"] = True
            cue["reason"] = f"moved by hand to {_mmss(ms)} (was {_mmss(was)}, {cue.get('reason') or 'no reason recorded'})"
            moved = ms != was
        if payload.get("locked") is not None:
            cue["locked"] = bool(payload["locked"])

        self.store.set_cue_plans({key: plan})
        CUE_LOG.info(
            "%s | cue %s %s | %s | locked=%s",
            record.get("label") or key,
            slot,
            "moved" if moved else "updated",
            _mmss(int(cue.get("ms") or 0)),
            cue.get("locked"),
        )
        return {
            "key": key,
            "slot": slot,
            "moved": moved,
            "cue": {
                "slot": slot,
                "ms": int(cue.get("ms") or 0),
                "seconds": round(int(cue.get("ms") or 0) / 1000.0, 3),
                "locked": bool(cue.get("locked")),
                "edited": bool(cue.get("edited")),
                "reason": cue.get("reason", ""),
            },
            "plan": self.cue_plan(key),
        }

    def save_export(self, payload: dict) -> dict:
        """Write an export straight to disk and report where it went.

        The native app uses this: a WKWebView ignores the usual blob download, so
        asking the server to save the file is both simpler and more predictable.
        """
        content, filename, _ = self.export(payload)
        directory = Path(payload.get("directory") or "").expanduser() if payload.get("directory") else _download_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / filename
            target.write_bytes(content)
        except OSError as exc:
            raise ValueError(f"Could not save to {directory}: {exc}") from exc
        return {
            "saved_to": str(target),
            "directory": str(directory),
            "filename": filename,
            "bytes": len(content),
        }

    # ------------------------------------------------------------- cues

    def export_cue_plans(self, keys: list[str], *, honour_policy: bool = True) -> dict[str, dict]:
        """Cue plans for the given track keys, ready for the exporter.

        Generated cues come from the store; the cues rekordbox already had are
        attached as ``existing`` so the exporter can keep the ones outside the
        eight hot slots.  The beat grid travels with the plan so the exported XML
        carries a real ``<TEMPO>``.

        Under the ``keep`` policy a track whose cues are the DJ's own carries
        those marks and none of ours, so re-importing the XML changes nothing
        about where that track's cues sit -- it still gets its beat grid, which
        is what the import needs to place them on the right beats.  The direct
        master.db writer re-checks against the live database anyway and asks for
        the plans unfiltered (``honour_policy=False``), since the database, not
        the stored library, is what it has to agree with.
        """
        stored = self.store.cue_plans()
        records = self.store.tracks_by_key()
        policy = self.store.cue_policy()
        overrides = self.store.cue_overrides()
        plans: dict[str, dict] = {}
        for key in keys:
            plan = dict(stored.get(key) or {})
            record = records.get(key) or {}
            existing = [cue for cue in (record.get("cues") or [])]
            structure = plan.get("structure") or {}
            grid = None
            if structure.get("bpm"):
                grid = {
                    "inizio": (structure.get("first_beat_ms") or 0) / 1000.0,
                    "bpm": structure["bpm"],
                    "metro": "4/4",
                    "battito": 1,
                }
            elif (self._analyses().get(key) or {}).get("bpm"):
                # No cue plan, but the audio has been measured: the exported
                # track can still arrive beat-gridded.
                measured = self._analyses()[key]
                grid = {
                    "inizio": (measured.get("first_beat_ms") or 0) / 1000.0,
                    "bpm": measured["bpm"],
                    "metro": "4/4",
                    "battito": 1,
                    "source": "analysed",
                }
            if honour_policy and cue_policy_for(key, policy, overrides) == "keep":
                if existing_cue_summary(existing)["user"]:
                    # Under "keep" the DJ's own cues stay and ours are left out --
                    # except the ones they moved by hand and locked, which are a
                    # decision of theirs made after the cues that make this track
                    # one of "theirs".  The lock exists to be honoured here.
                    held = [cue for cue in (plan.get("hot_cues") or []) if cue.get("locked")]
                    kept: dict = {"existing": existing, "kept": True, "hot_cues": held}
                    if held:
                        kept["kept_locked"] = len(held)
                    if grid:
                        kept["beat_grid"] = grid
                    plans[key] = kept
                    continue
            if existing:
                plan["existing"] = existing
            if grid:
                plan["beat_grid"] = grid
            if plan:
                plans[key] = plan
        return plans

    def beat_grids(self, keys: list[str]) -> dict[str, dict]:
        """Grid-only plans for the tracks that have been measured.

        Used when an export carries no cues: the cue toggle should not also turn
        off the tempo marker, since a beat grid is what rekordbox needs to place
        the track on the beat even when Parity is not providing cues.
        """
        plans: dict[str, dict] = {}
        analyses = self._analyses()
        for key in keys:
            measured = analyses.get(key) or {}
            if not measured.get("bpm"):
                continue
            plans[key] = {
                "beat_grid": {
                    "inizio": (measured.get("first_beat_ms") or 0) / 1000.0,
                    "bpm": measured["bpm"],
                    "metro": "4/4",
                    "battito": 1,
                    "source": "analysed",
                }
            }
        return plans

    def analyse(self, payload: dict | None = None) -> dict:
        """Generate structure cues for a set of tracks and store them.

        Every processed track is logged with its cue timestamps, so a run can be
        audited without opening rekordbox.
        """
        payload = payload or {}
        views = self.all_views()
        if payload.get("filter"):
            config = poolfilter.FilterConfig.from_dict(payload["filter"])
            keys = [view["key"] for view in self._filter(config).kept]
        elif payload.get("keys"):
            keys = [key for key in payload["keys"] if key in views]
        else:
            keys = list(views.keys())

        if payload.get("local_only"):
            keys = [key for key in keys if not views[key].get("streaming")]
        limit = int(payload.get("limit") or 0)
        if limit > 0:
            keys = keys[:limit]

        records = self.store.tracks_by_key()
        previous_plans = self.store.cue_plans()
        plans: dict[str, dict] = {}
        results: list[dict] = []
        sources: dict[str, int] = {}
        kept_locked: dict[str, int] = {}
        started = time.perf_counter()
        for key in keys:
            record = records.get(key)
            if record is None:
                continue
            result = structure_mod.analyse_track(self.track_from_record(record))
            result["label"] = views[key].get("label", "")
            result["path"] = record.get("path", "")
            # A cue the user moved by hand and locked stays where they put it:
            # re-analysing is about the tracks, not about overruling the editor.
            kept = carry_locked_cues(previous_plans.get(key), result)
            if kept:
                kept_locked[key] = kept
            plans[key] = result

            structure = result["structure"]
            sources[structure["source"]] = sources.get(structure["source"], 0) + 1
            hot = {cue["slot"]: cue for cue in result["hot_cues"]}
            CUE_LOG.info(
                "%s | %s | mood=%s phrases=%d beats=%d bpm=%s | %s | %d memory%s",
                result["label"],
                structure["source"],
                structure.get("mood") or "-",
                structure["phrase_count"],
                structure["beats"],
                structure["bpm"],
                " ".join(
                    f"{slot}={hot[slot]['seconds']:.3f}" for slot in "ABCDEFGH" if slot in hot
                )
                or "no cues",
                len(result["memory_cues"]),
                f" | {result['note']}" if result.get("note") else "",
            )
            if CUE_LOG.isEnabledFor(logging.DEBUG):
                for cue in result["hot_cues"]:
                    CUE_LOG.debug(
                        "  %s %s @ %.3fs (%s) - %s",
                        cue["slot"], cue["name"], cue["seconds"], cue["confidence"], cue["reason"],
                    )

            results.append(
                {
                    "key": key,
                    "label": result["label"],
                    "source": structure["source"],
                    "confidence": structure["confidence"],
                    "mood": structure.get("mood", 0),
                    "phrase_count": structure["phrase_count"],
                    "bpm": structure["bpm"],
                    "hot_cues": [
                        {
                            "slot": cue["slot"],
                            "name": cue["name"],
                            "seconds": cue["seconds"],
                            "reason": cue["reason"],
                        }
                        for cue in result["hot_cues"]
                    ],
                    "memory_count": len(result["memory_cues"]),
                    "note": result.get("note", ""),
                }
            )

        stored = self.store.set_cue_plans(plans)
        elapsed = time.perf_counter() - started
        LOG.info(
            "cue analysis: %d tracks in %.2fs (sources=%s, %d stored, %d hand-moved kept)",
            len(results), elapsed, sources, stored, sum(kept_locked.values()),
        )
        return {
            "analysed": len(results),
            "stored": stored,
            "elapsed": round(elapsed, 3),
            "by_source": sources,
            "kept_locked": kept_locked,
            "kept": sum(kept_locked.values()),
            "results": results,
        }

    # --------------------------------------------------------- ingest

    def _ingest_settings(self, payload: dict) -> dict:
        """The folder, destination and switches for one scan or run."""
        saved = self.store.ingest_settings()
        values = {
            "source": str(payload.get("source") or saved.get("source") or "").strip(),
            "destination": str(payload.get("destination") or saved.get("destination") or "").strip(),
            "format": str(payload.get("format") or saved.get("format") or ingest_mod.DEFAULT_FORMAT),
            "recursive": bool(payload.get("recursive", saved.get("recursive", True))),
            "measure": bool(payload.get("measure", saved.get("measure", True))),
            "trim_silence": bool(payload.get("trim_silence", saved.get("trim_silence", False))),
            "overwrite": bool(payload.get("overwrite", saved.get("overwrite", False))),
        }
        # What is stored is what is worth remembering next time; what is returned
        # is the whole request, because a blank destination is a choice (the
        # default "converted" folder) and not a missing value.
        self.store.set_ingest_settings(values)
        return values

    def ingest_status(self) -> dict:
        """What the folder tidy-up can do here, and where it last ran."""
        status = ingest_mod.status()
        saved = self.store.ingest_settings()
        status["settings"] = saved
        source = saved.get("source") or ""
        status["source_exists"] = bool(source) and Path(source).expanduser().is_dir()
        status["destination"] = saved.get("destination") or (
            str((Path(source).expanduser() / "converted")) if source else ""
        )
        return status

    def ingest_scan(self, payload: dict | None = None) -> dict:
        """Read a staging folder and report what it would do; write nothing."""
        settings = self._ingest_settings(payload or {})
        plan = ingest_mod.scan(
            settings["source"],
            settings["destination"],
            settings["format"],
            recursive=settings["recursive"],
            measure=settings["measure"],
            overwrite=settings["overwrite"],
        )
        summary = plan.to_dict()["summary"]
        LOG.info(
            "ingest scan of %s: %d files, %d to rename, %d to convert to %s",
            plan.root, summary["found"], summary["renames"], summary["converts"], plan.format.upper(),
        )
        return {
            "plan": plan.to_dict(),
            "report": ingest_mod.run(plan, dry_run=True),
            "status": self.ingest_status(),
        }

    def ingest_run(self, payload: dict | None = None) -> dict:
        """Do it: convert and copy the folder's files into the destination."""
        payload = payload or {}
        settings = self._ingest_settings(payload)
        plan = ingest_mod.scan(
            settings["source"],
            settings["destination"],
            settings["format"],
            recursive=settings["recursive"],
            measure=settings["measure"],
            overwrite=settings["overwrite"],
        )
        report = ingest_mod.run(
            plan,
            dry_run=bool(payload.get("dry_run")),
            overwrite=settings["overwrite"],
            trim_silence=settings["trim_silence"],
        )
        summary = report["summary"]
        INGEST_LOG.info(
            "ingest into %s: %d converted, %d copied, %d skipped, %d failed in %.1fs",
            summary["folder"], summary["converted"], summary["copied"],
            summary["skipped"], summary["failed"], report["elapsed"],
        )
        return {"report": report, "plan": plan.to_dict(), "status": self.ingest_status()}

    # -------------------------------------------------- energy from audio

    def audio_status(self) -> dict:
        """What the audio energy feature can do here, and how much it has done."""
        status = audio_mod.status()
        stored = self.store.audio_analysis()
        records = self.store.tracks_by_key()
        local = [key for key, record in records.items() if not record.get("streaming")]
        current = [
            key
            for key in local
            if int((stored.get(key) or {}).get("model", 0)) == audio_mod.MODEL_VERSION
        ]
        status.update(
            {
                "analysed": len(current),
                "local": len(local),
                "stale": len(stored) - len(current),
                "with_features": len(stored),
            }
        )
        views = self.all_views()
        status["using_audio"] = sum(
            1 for view in views.values() if view.get("energy_source") == "audio"
        )
        return status

    def analyse_audio(self, payload: dict | None = None) -> dict:
        """Measure loudness, brightness, dynamics and drive for each track.

        The measured energy then replaces the metadata guess for that track (see
        :mod:`parity.energy`).  Work is cached: a track whose file has not
        changed since the last run is not decoded again, so the second press of
        the button is instant.  ``dry_run`` measures without storing, and
        ``limit`` bounds one request so the UI can work through a big library in
        chunks and show progress.
        """
        payload = payload or {}
        views = self.all_views()
        if payload.get("filter"):
            config = poolfilter.FilterConfig.from_dict(payload["filter"])
            keys = [view["key"] for view in self._filter(config).kept]
        elif payload.get("keys"):
            keys = [key for key in payload["keys"] if key in views]
        else:
            keys = list(views.keys())
        if payload.get("local_only", True):
            keys = [key for key in keys if not views[key].get("streaming")]
        limit = int(payload.get("limit") or 0)

        stored = self.store.audio_analysis()
        records = self.store.tracks_by_key()
        force = bool(payload.get("force"))
        entries: dict[str, dict] = {}
        results: list[dict] = []
        skipped: list[dict] = []
        engines: dict[str, int] = {}
        # The already-measured keys are named, not just counted, because a caller
        # asks for a big pool in chunks: it has to know which keys are finished to
        # stop sending them, and a bare count gave it no way to tell a newly
        # cached track from one it had already been told about.  Without this the
        # UI counted every cached track once per chunk -- a 14-track pool
        # reported "26 already cached".
        cached_keys: list[str] = []
        started = time.perf_counter()

        for key in keys:
            if limit > 0 and len(results) + len(skipped) >= limit:
                break
            record = records.get(key) or {}
            label = views[key].get("label", "")
            try:
                path = self.audio_path(key)
            except ValueError as exc:
                skipped.append({"key": key, "label": label, "reason": str(exc)})
                continue
            stat = path.stat()
            previous = stored.get(key) or {}
            if (
                not force
                and int(previous.get("model", 0)) == audio_mod.MODEL_VERSION
                and previous.get("mtime") == stat.st_mtime
                and previous.get("size") == stat.st_size
            ):
                cached_keys.append(key)
                continue

            track_started = time.perf_counter()
            try:
                features = audio_mod.analyse(path)
            except audio_mod.AudioUnavailable as exc:
                skipped.append({"key": key, "label": label, "reason": str(exc)})
                continue
            entry = features.to_dict()
            entry.update({"mtime": stat.st_mtime, "size": stat.st_size, "path": str(path)})
            entries[key] = entry
            engines[entry["engine"]] = engines.get(entry["engine"], 0) + 1
            elapsed_ms = (time.perf_counter() - track_started) * 1000
            AUDIO_LOG.info(
                "%s | E%s (%s) | loudness %.2f dBFS | centroid %.0f Hz | "
                "range %.2f dB | drive %.2f/s | %.1fs of audio in %.0f ms",
                label,
                entry["energy"],
                entry["engine"],
                entry["loudness_dbfs"],
                entry["centroid_hz"],
                entry["dynamic_range_db"],
                entry["onset_rate"],
                entry["seconds"],
                elapsed_ms,
            )
            results.append(
                {
                    "key": key,
                    "label": label,
                    "energy": entry["energy"],
                    "energy_was": float(record.get("energy") or 0) or None,
                    "features": entry,
                    "ms": round(elapsed_ms, 1),
                }
            )

        dry_run = bool(payload.get("dry_run"))
        if entries and not dry_run:
            self.store.set_audio_analysis(entries)
        elapsed = time.perf_counter() - started
        LOG.info(
            "audio energy (%s): %d measured, %d cached, %d skipped in %.2fs (engines=%s)",
            "dry run" if dry_run else "stored",
            len(results),
            len(cached_keys),
            len(skipped),
            elapsed,
            engines or "none",
        )
        return {
            "analysed": len(results),
            "cached": len(cached_keys),
            "cached_keys": cached_keys,
            "skipped": skipped,
            "dry_run": dry_run,
            "elapsed": round(elapsed, 3),
            "by_engine": engines,
            "model": audio_mod.MODEL_VERSION,
            "results": results,
            "status": self.audio_status(),
        }

    # -------------------------------------------- playlists & watch folder

    def import_m3u(self, payload: dict | None = None) -> dict:
        """Import an M3U/M3U8 as a playlist, adding any tracks it names.

        A path is used when the app has one (the Mac shell, a script); a
        dropped file arrives as text, with absolute paths or a ``base`` folder
        for the relative ones.
        """
        payload = payload or {}
        path = str(payload.get("path") or "").strip()
        try:
            if path:
                report = watchfolder.import_m3u(self.store, path=path)
            else:
                text = str(payload.get("text") or "")
                if not text.strip():
                    raise ValueError("No playlist supplied.")
                report = watchfolder.import_m3u(
                    self.store,
                    text=text,
                    name=str(payload.get("name") or ""),
                    base_dir=str(payload.get("base") or ""),
                )
        except watchfolder.PlaylistError as exc:
            raise ValueError(str(exc)) from exc
        LOG.info(
            "M3U import %r: %d entries, %d new, %d matched, %d missing",
            report["playlist"], report["entries"], report["added"],
            report["matched"], len(report["missing"]),
        )
        return {"ok": True, **report, "state": self.state()}

    def watch_status(self) -> dict:
        status = watchfolder.status(self.store)
        status["running"] = bool(self._watch_thread and self._watch_thread.is_alive())
        # What the playlist currently holds, so the panel can say whether the
        # import is landing somewhere the pool can see.
        library = self.store.library() or {}
        name = status.get("playlist") or ""
        playlist = next(
            (node for node in library.get("playlists") or [] if node.get("name") == name),
            None,
        )
        status["playlist_count"] = (playlist or {}).get("count", 0)
        status["m3u"] = "Import an .m3u or .m3u8 file as a playlist."
        return status

    def watch_configure(self, payload: dict | None = None) -> dict:
        """Save the watch folder and run one pass so the choice has a result."""
        payload = payload or {}
        try:
            watchfolder.configure(self.store, payload)
        except watchfolder.PlaylistError as exc:
            raise ValueError(str(exc)) from exc
        settings = self.store.watch_settings()
        report = None
        if settings.get("enabled"):
            report = watchfolder.scan(self.store)
            self._start_watch()
        else:
            self._stop_watch()
        return {"settings": settings, "report": report, "status": self.watch_status(), "state": self.state()}

    def watch_tick(self) -> dict:
        """Scan once, now, whether or not the timer is on."""
        report = watchfolder.scan(self.store)
        return {"report": report, "status": self.watch_status(), "state": self.state()}

    def _start_watch(self) -> None:
        """Start the background scanner if it is not already running."""
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()

        def loop() -> None:
            while not self._watch_stop.is_set():
                interval = float(self.store.watch_settings().get("interval") or 5.0)
                if self._watch_stop.wait(max(3.0, interval)):
                    break
                if not self.store.watch_settings().get("enabled"):
                    continue
                try:
                    report = watchfolder.scan(self.store)
                    if report.get("added"):
                        LOG.info("watch folder imported %d new track(s)", report["added"])
                except Exception as exc:  # noqa: BLE001 - the next tick may succeed
                    LOG.warning("watch folder scan failed: %s", exc)
                    self.store.set_watch_settings({"last_error": str(exc)})

        self._watch_thread = threading.Thread(target=loop, name="parity-watch", daemon=True)
        self._watch_thread.start()

    def _stop_watch(self) -> None:
        self._watch_stop.set()
        self._watch_thread = None

    # --------------------------------------------------- tempo / key / grid

    def analysis_status(self) -> dict:
        """What the tempo/key analysis can do here, and how much it has done."""
        status = analysis_mod.status()
        stored = self.store.track_analysis()
        records = self.store.tracks_by_key()
        local = [key for key, record in records.items() if not record.get("streaming")]
        current = [
            key
            for key in local
            if int((stored.get(key) or {}).get("model", 0)) == analysis_mod.MODEL_VERSION
        ]
        status.update(
            {
                "analysed": len(current),
                "local": len(local),
                "stale": len(stored) - len(current),
                "with_features": len(stored),
            }
        )
        views = self.all_views()
        status["using_bpm"] = sum(1 for view in views.values() if view.get("bpm_source") == "analysed")
        status["using_key"] = sum(1 for view in views.values() if view.get("key_source") == "analysed")
        return status

    def analyse_track_audio(self, payload: dict | None = None) -> dict:
        """Measure BPM, key and a beat grid for the tracks in play.

        Cached by file (path, size, mtime) and model, like the energy run, so a
        second press is instant and only changed files are decoded again.  A
        track the library already describes keeps its own BPM and key: this
        fills what is missing rather than second-guessing rekordbox.
        """
        payload = payload or {}
        views = self.all_views()
        if payload.get("filter"):
            config = poolfilter.FilterConfig.from_dict(payload["filter"])
            keys = [view["key"] for view in self._filter(config).kept]
        elif payload.get("keys"):
            keys = [key for key in payload["keys"] if key in views]
        else:
            keys = list(views.keys())
        if payload.get("local_only", True):
            keys = [key for key in keys if not views[key].get("streaming")]
        limit = int(payload.get("limit") or 0)

        stored = self.store.track_analysis()
        records = self.store.tracks_by_key()
        force = bool(payload.get("force"))
        entries: dict[str, dict] = {}
        results: list[dict] = []
        skipped: list[dict] = []
        cached_keys: list[str] = []
        started = time.perf_counter()

        for key in keys:
            if limit > 0 and len(results) + len(skipped) >= limit:
                break
            record = records.get(key) or {}
            label = views[key].get("label", "")
            try:
                path = self.audio_path(key)
            except ValueError as exc:
                skipped.append({"key": key, "label": label, "reason": str(exc)})
                continue
            stat = path.stat()
            previous = stored.get(key) or {}
            if (
                not force
                and int(previous.get("model", 0)) == analysis_mod.MODEL_VERSION
                and previous.get("mtime") == stat.st_mtime
                and previous.get("size") == stat.st_size
            ):
                cached_keys.append(key)
                continue

            track_started = time.perf_counter()
            try:
                measured = analysis_mod.analyse(path)
            except analysis_mod.AnalysisUnavailable as exc:
                skipped.append({"key": key, "label": label, "reason": str(exc)})
                continue
            entry = measured.to_dict()
            entry.update({"mtime": stat.st_mtime, "size": stat.st_size, "path": str(path)})
            entries[key] = entry
            elapsed_ms = (time.perf_counter() - track_started) * 1000
            AUDIO_LOG.info(
                "%s | %.2f BPM (%s) | %s %s | first beat %d ms | %.1fs of audio in %.0f ms",
                label,
                entry["bpm"],
                entry["bpm_confidence"],
                entry["key"] or "?",
                entry["camelot"] or "?",
                entry["first_beat_ms"],
                entry["seconds"],
                elapsed_ms,
            )
            results.append(
                {
                    "key": key,
                    "label": label,
                    "bpm": entry["bpm"],
                    "bpm_confidence": entry["bpm_confidence"],
                    "key": entry["key"],
                    "camelot": entry["camelot"],
                    "key_confidence": entry["key_confidence"],
                    "first_beat_ms": entry["first_beat_ms"],
                    "interval_ms": entry["interval_ms"],
                    "beats": entry["beats"],
                    "note": entry["note"],
                    "ms": round(elapsed_ms, 1),
                }
            )

        dry_run = bool(payload.get("dry_run"))
        if entries and not dry_run:
            self.store.set_track_analysis(entries)
            self._analysis_version = -1
        elapsed = time.perf_counter() - started
        LOG.info(
            "tempo/key analysis (%s): %d measured, %d cached, %d skipped in %.2fs",
            "dry run" if dry_run else "stored",
            len(results), len(cached_keys), len(skipped), elapsed,
        )
        return {
            "analysed": len(results),
            "cached": len(cached_keys),
            "cached_keys": cached_keys,
            "skipped": skipped,
            "dry_run": dry_run,
            "elapsed": round(elapsed, 3),
            "model": analysis_mod.MODEL_VERSION,
            "results": results,
            "status": self.analysis_status(),
        }

    # -------------------------------------------------------- pull & filter

    def rekordbox_status(self, db_path: Path | None = None) -> dict:
        """Database + optional-extra status, with counts when we have a library."""
        status = rxdb.db_status(db_path)
        library = self.store.library() or {}
        records = library.get("tracks") or {}
        status["library"] = {
            "loaded": bool(library),
            "source": library.get("source", ""),
            "source_kind": library.get("source_kind", ""),
            "tracks": len(records),
            "local": sum(1 for record in records.values() if not record.get("streaming")),
            "streaming": sum(1 for record in records.values() if record.get("streaming")),
            "analysed": sum(1 for record in records.values() if record.get("analysed")),
        }
        return status

    def connect_rekordbox(self, payload: dict | None = None) -> dict:
        """Import straight from master.db, with the XML export as a fallback."""
        payload = payload or {}
        db_path = Path(payload["db_path"]).expanduser() if payload.get("db_path") else None
        fallback = Path(payload["fallback_xml"]).expanduser() if payload.get("fallback_xml") else None
        started = time.perf_counter()
        try:
            library, kind, detail = rxdb.read_library_or_xml(fallback, db_path)
        except rxdb.MasterDbUnavailable as exc:
            raise ValueError(str(exc)) from exc

        if library.source_kind == "xml" and fallback is not None:
            self.store.set_library(
                library,
                fallback.read_text(encoding="utf-8", errors="replace"),
                source=str(fallback),
                source_kind="xml",
            )
        else:
            self.store.set_library(library, None, source=library.source, source_kind="db")

        elapsed = time.perf_counter() - started
        records = library.tracks
        streaming = sum(1 for track in records.values() if track.is_streaming)
        analysed = sum(1 for track in records.values() if track.analysed)
        LOG.info(
            "connected via %s: %d tracks (%d local, %d streaming, %d analysed) in %.2fs%s",
            kind, len(records), len(records) - streaming, streaming, analysed, elapsed,
            f" - {detail}" if detail else "",
        )
        return {
            "connected": True,
            "origin": kind,
            "detail": detail,
            "elapsed": round(elapsed, 3),
            "track_count": len(records),
            "local_count": len(records) - streaming,
            "streaming_count": streaming,
            "analysed_count": analysed,
            "playlists": len(library.playlists),
            "state": self.state(),
        }

    def _filter(self, config: poolfilter.FilterConfig) -> poolfilter.FilterResult:
        views = list(self.all_views().values())
        playlist_keys = None
        if config.playlist_id:
            library = self.store.library() or {}
            records = library.get("tracks") or {}
            playlist_keys = {
                records[track_id]["key"]
                for track_id in _playlist_track_ids(library.get("playlists") or [], config.playlist_id)
                if track_id in records
            }
        return poolfilter.apply_filter(views, config, playlist_keys)

    def pool(self, payload: dict | None = None) -> dict:
        """Preview what a filter keeps, without building anything."""
        payload = payload or {}
        config = poolfilter.FilterConfig.from_dict(payload.get("filter") or payload)
        result = self._filter(config)
        if not self.store.library():
            raise ValueError("No library loaded yet.")
        warning = poolfilter.volume_warning(config.folder or "")
        return {
            "filter": config.to_dict(),
            "report": result.report(),
            "keys": [view["key"] for view in result.kept],
            "warning": warning,
        }

    # -------------------------------------------------------------- export

    def xml_bytes(
        self,
        tracks: list[dict],
        name: str,
        with_cues: bool,
        tags: dict[str, str] | None = None,
    ) -> bytes:
        """The rekordbox XML for a set, cues and tag comments included."""
        records = self.store.tracks_by_key()
        fallback = {}
        for track in tracks:
            record = records.get(track["key"])
            if record:
                fallback[str(record.get("id"))] = {**record, **track}

        cue_plans_by_id: dict[str, dict] = {}
        keys = [track["key"] for track in tracks]
        plans = self.export_cue_plans(keys) if with_cues else self.beat_grids(keys)
        for track in tracks:
            plan = plans.get(track["key"])
            if plan:
                cue_plans_by_id[str(track.get("id"))] = plan
        return exporting.set_to_rekordbox_xml(
            self.store.library_xml(),
            [str(track.get("id")) for track in tracks],
            set_name=name,
            fallback_tracks=fallback,
            cue_plans=cue_plans_by_id,
            tags=tags,
        )

    def tag_plan(self, tracks: list[dict]) -> dict[str, dict]:
        """The set tags for an ordered set, by track key (see :mod:`parity.tags`)."""
        return tags_mod.plan_tags(tracks)

    def set_tags(self, payload: dict | None = None) -> dict:
        """The set tags for an ordered set, without writing anything anywhere.

        The tag (``[E7] [peak] 14/24``) belongs to the export and the write, but
        it is decided from the set -- energy band, the part each track plays,
        where it sits -- and the person building the set had no way to read one
        before either ran.  Same arithmetic as the file, so the tag shown on a
        row is the tag the XML will carry; nothing here touches the library.
        """
        payload = payload or {}
        views = self.all_views()
        keys = [key for key in (payload.get("order") or []) if key in views]
        if not keys:
            return {"count": 0, "roles": {}, "rows": []}
        return tags_mod.tag_report(self.tag_plan([views[key] for key in keys]))

    @staticmethod
    def tags_by_track_id(tracks: list[dict], plan: dict[str, dict]) -> dict[str, str]:
        """The same tags keyed the way the XML keys its tracks (``TrackID``)."""
        return {
            str(track.get("id")): plan[track["key"]]["tag"]
            for track in tracks
            if track.get("key") in plan
        }

    def duplicates(self, payload: dict | None = None) -> dict:
        """The duplicate groups in a set, a pool, or the whole library.

        The set is what the UI checks by default -- a track listed twice costs a
        slot and is the one kind a built set can hold -- while ``keys`` or
        ``scope: pool`` asks about the pool it was built from.  Nothing is
        removed here; the report names the row worth keeping and the positions
        that would go.
        """
        payload = payload or {}
        views = self.all_views()
        if payload.get("order"):
            keys = [key for key in payload["order"] if key in views]
        elif payload.get("keys"):
            keys = [key for key in payload["keys"] if key in views]
        elif payload.get("filter"):
            config = poolfilter.FilterConfig.from_dict(payload["filter"])
            keys = [view["key"] for view in self._filter(config).kept]
        else:
            keys = list(views.keys())
        report = duplicates_mod.report([views[key] for key in keys])
        report["scope"] = "set" if payload.get("order") else "pool"
        return report

    def remove_duplicates(self, payload: dict | None = None) -> dict:
        """Drop duplicate positions from a set, keeping the chosen row of each.

        ``keep`` maps a group id from :meth:`duplicates` to the 1-based position
        the user picked.  The answer is the surviving order as keys, so the UI
        applies it to the set it already holds -- no track is deleted from the
        library and no file is touched.
        """
        payload = payload or {}
        views = self.all_views()
        order = payload.get("order") or []
        tracks = [views[key] for key in order if key in views]
        if not tracks:
            raise ValueError("Nothing to de-duplicate: build or load a set first.")
        plan = duplicates_mod.plan_removal(tracks, payload.get("keep") or {})
        LOG.info(
            "duplicates: %d groups, %d row(s) removed from a set of %d",
            len(plan["groups"]), plan["removed_count"], len(tracks),
        )
        return plan

    def preflight(self, payload: dict | None = None) -> dict:
        """Check a set before it is exported or written into a library.

        Takes the set as the UI holds it (``order`` of track keys) and answers
        with the issues -- a missing file, a track listed twice, a track with no
        cues yet.  Nothing here blocks anything: an export is a copy, and the
        person building the set decides what belongs in it.
        """
        payload = payload or {}
        views = self.all_views()
        if payload.get("keys"):
            keys = [key for key in payload["keys"] if key in views]
        else:
            keys = [key for key in (payload.get("order") or []) if key in views]
        tracks = [views[key] for key in keys]
        return preflight_mod.inspect(
            tracks,
            check_cues=bool(payload.get("cues", True)),
            set_name=str(payload.get("name") or ""),
        )

    def _set_manifest(self, tracks: list[dict], name: str, filename: str, payload: dict) -> bytes:
        """The companion manifest the AUTO/PILOT bundle and the package share."""
        return exporting.autopilot_manifest(
            tracks,
            name,
            transitions=payload.get("transitions"),
            stats=payload.get("stats"),
            source=(self.store.library() or {}).get("source", ""),
            xml_filename=f"{filename}.xml",
            cue_plans=self.export_cue_plans([track["key"] for track in tracks]),
        )

    def _package(
        self,
        tracks: list[dict],
        name: str,
        filename: str,
        with_cues: bool,
        tags_by_id: dict[str, str],
        payload: dict,
    ) -> bytes:
        """The portable set package: everything about one set, in one zip.

        The XML is the import file, but a set that has been handed over or put
        away needs to be explainable without Parity: the set list says what
        the order is, the cue log says where every cue sits and why, the
        pre-flight report says what was already wrong when it was written, and
        the manifest is the same set as JSON.  ``README.txt`` is generated from
        this list, so a part cannot be added to the zip without a line saying
        what it is for.
        """
        plans = self.export_cue_plans([track["key"] for track in tracks])
        check = preflight_mod.inspect(tracks, check_cues=with_cues, set_name=name)
        manifest_name = f"{filename}.autopilot.json"
        manifest = self._set_manifest(tracks, name, filename, payload)
        entries = [
            (
                f"{filename}.xml",
                self.xml_bytes(tracks, name, with_cues, tags_by_id),
                "the set as rekordbox XML: cues, tag comments and beat grids - import this one",
            ),
            (
                f"{filename}.txt",
                exporting.set_to_txt(tracks, name),
                "the set list: one line per track with BPM, key and energy",
            ),
            (
                f"{filename}.csv",
                exporting.set_to_csv(tracks),
                "the same set list as a spreadsheet",
            ),
            (
                f"{filename}.cues.txt",
                exporting.cue_log(tracks, plans, name, tags_by_id),
                "the cue log: every hot and memory cue, its colour and why it landed there",
            ),
            (
                f"{filename}.preflight.txt",
                check["text"].encode("utf-8"),
                "the pre-flight check: missing files, duplicate entries, tracks with no cues",
            ),
            (
                manifest_name,
                manifest,
                "the set as JSON: order, transitions, stats and every cue timestamp",
            ),
        ]
        files = [(entry, description) for entry, _content, description in entries]
        files.append(("README.txt", "this file: what is in the package and how to use it"))
        readme = exporting.package_readme(
            name,
            files,
            source=(self.store.library() or {}).get("source", ""),
            preflight=check,
        )
        stems = {entry: content for entry, content, _description in entries}
        stems["README.txt"] = readme
        return exporting.set_to_bundle(filename, stems=stems)

    def export(self, payload: dict) -> tuple[bytes, str, str]:
        """Return (content, filename, content type)."""
        views = self.all_views()
        keys = payload.get("order") or []
        tracks = [views[key] for key in keys if key in views]
        if not tracks:
            raise ValueError("Nothing to export.")
        name = payload.get("name") or "Parity Set"
        fmt = (payload.get("format") or "rekordbox").lower()
        filename = _safe_filename(name)
        with_cues = bool(payload.get("cues", True))
        # An export writes a copy, so the tags are on unless they are turned
        # off, the way the cues are.  The write into master.db is the one that
        # changes the library, and it asks for them explicitly.
        tag_rows = self.tag_plan(tracks) if bool(payload.get("tags", True)) else {}
        tags_by_id = self.tags_by_track_id(tracks, tag_rows) if tag_rows else {}

        if fmt in {"rekordbox", "xml"}:
            return self.xml_bytes(tracks, name, with_cues, tags_by_id), f"{filename}.xml", "application/xml"

        if fmt in {"autopilot", "bundle", "zip"}:
            xml = self.xml_bytes(tracks, name, with_cues, tags_by_id)
            bundle = exporting.set_to_bundle(
                filename,
                stems={
                    f"{filename}.xml": xml,
                    f"{filename}.txt": exporting.set_to_txt(tracks, name),
                    f"{filename}.csv": exporting.set_to_csv(tracks),
                },
                manifest_bytes=self._set_manifest(tracks, name, filename, payload),
            )
            return bundle, f"{filename}.zip", "application/zip"

        if fmt in {"package", "set-package"}:
            return (
                self._package(tracks, name, filename, with_cues, tags_by_id, payload),
                f"{filename}.zip",
                "application/zip",
            )

        if fmt == "m3u8":
            return exporting.set_to_m3u8(tracks, name), f"{filename}.m3u8", "audio/x-mpegurl"
        if fmt == "csv":
            return exporting.set_to_csv(tracks), f"{filename}.csv", "text/csv"
        if fmt == "txt":
            return exporting.set_to_txt(tracks, name), f"{filename}.txt", "text/plain"
        if fmt == "html":
            tags_by_key = {key: row["tag"] for key, row in tag_rows.items()}
            return exporting.set_to_html(tracks, name, tags_by_key), f"{filename}.html", "text/html; charset=utf-8"
        if fmt == "pdf":
            tags_by_key = {key: row["tag"] for key, row in tag_rows.items()}
            return exporting.set_to_pdf(tracks, name, tags_by_key), f"{filename}.pdf", "application/pdf"
        raise ValueError(f"Unknown export format: {fmt}")

    # ------------------------------------------------------------ pipeline

    def pipeline(self, payload: dict | None = None) -> dict:
        """End-to-end: select, purge, sequence, cue, export.

        One call for the whole workflow the tool exists for, so the UI never has
        to orchestrate five requests and a failure leaves a partial set.
        """
        payload = payload or {}
        if not self.store.library():
            raise ValueError(
                "No library loaded. Connect to rekordbox or import an XML export first."
            )

        config = poolfilter.FilterConfig.from_dict(payload.get("filter") or {})
        if not (config.playlist_id or config.folder or config.keys):
            # No explicit target: the safe default is "playable tracks only".
            config.local_only = True
        filtered = self._filter(config)
        keys = [view["key"] for view in filtered.kept]
        if not keys:
            reasons = ", ".join(
                f"{count} {reason}" for reason, count in sorted(filtered.reasons.items())
            )
            raise ValueError(
                f"Nothing left after filtering: kept {len(filtered.kept)} of "
                f"{filtered.scanned} tracks" + (f" ({reasons})" if reasons else "")
            )

        build_config = builder.BuildConfig.from_dict(payload.get("build") or {})
        requested = int(build_config.length_tracks or 20)
        build_config.length_tracks = max(1, min(requested, len(keys)))
        views = self.all_views()
        result = builder.build_set([self.candidate(views[key]) for key in keys], build_config)
        order = [key for key in result.order if key in views]
        if not order:
            raise ValueError("The builder returned an empty set.")

        analysis = {"analysed": 0, "by_source": {}}
        if payload.get("cues", True):
            analysis = self.analyse({"keys": order})
            # Cue plans change the track views, so re-read them: the set handed
            # back should be the one that was exported, cues included.
            views = self.all_views()
            order = [key for key in order if key in views]

        name = payload.get("name") or "Parity Set"
        bundle = bool(payload.get("bundle", True))
        content, filename, _ = self.export(
            {
                "order": order,
                "name": name,
                "format": "autopilot" if bundle else "rekordbox",
                "cues": bool(payload.get("cues", True)),
                "transitions": [asdict(t) for t in result.transitions],
                "stats": result.stats,
            }
        )
        directory = (
            Path(payload["directory"]).expanduser() if payload.get("directory") else _download_dir()
        )
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / filename
            target.write_bytes(content)
        except OSError as exc:
            raise ValueError(f"Could not save to {directory}: {exc}") from exc

        LOG.info(
            "pipeline: kept %d of %d, set of %d, wrote %s (%d bytes)",
            len(filtered.kept), filtered.scanned, len(order), target, len(content),
        )
        return {
            "filter": filtered.report(),
            "warning": poolfilter.volume_warning(config.folder or ""),
            "order": [views[key] for key in order],
            "order_keys": order,
            "transitions": [asdict(t) for t in result.transitions],
            "targets": result.targets,
            "stats": result.stats,
            "warnings": result.warnings,
            "config": build_config.to_dict(),
            "cues": {
                "analysed": analysis.get("analysed", 0),
                "by_source": analysis.get("by_source", {}),
            },
            "export": {
                "saved_to": str(target),
                "directory": str(directory),
                "filename": filename,
                "bytes": len(content),
            },
        }

    # -------------------------------------------------------- direct writes

    def write_status(self, payload: dict | None = None) -> dict:
        """Whether the direct writers can run right now, and where their files are."""
        payload = payload or {}
        db_path = Path(payload["db_path"]).expanduser() if payload.get("db_path") else None
        ap_path = Path(payload["autopilot_db"]).expanduser() if payload.get("autopilot_db") else None
        return {
            "rekordbox": writedb.status(db_path),
            "autopilot": autopilot.status(ap_path),
        }

    def _write_targets(self, payload: dict) -> tuple[list[dict], list[str], str]:
        views = self.all_views()
        if not views:
            raise ValueError("No library loaded. Connect to rekordbox or import an XML export first.")
        keys = [key for key in (payload.get("order") or []) if key in views]
        if not keys:
            raise ValueError("Nothing to write: build a set first.")
        name = str(payload.get("playlist") or payload.get("name") or "").strip()
        return [views[key] for key in keys], keys, name

    def write_rekordbox(self, payload: dict | None = None) -> dict:
        """Write the set's cues and playlist order into ``master.db``.

        A dry run is the default: the caller has to pass ``dry_run: false``.  The
        result lists every ``djmdCue`` and ``djmdSongPlaylist`` row touched.
        """
        payload = payload or {}
        tracks, keys, name = self._write_targets(payload)
        with_cues = bool(payload.get("cues", True))
        plans = self.export_cue_plans(keys, honour_policy=False) if with_cues else {}
        db_path = Path(payload["db_path"]).expanduser() if payload.get("db_path") else None
        backup_root = (
            Path(payload["backup_dir"]).expanduser() if payload.get("backup_dir") else None
        )
        base_policy = str(payload.get("cue_policy") or self.store.cue_policy())
        overrides = self.store.cue_overrides()
        policies = {key: cue_policy_for(key, base_policy, overrides) for key in keys}
        # Tags are the one write here that changes something the DJ reads in the
        # library itself, so they are written only when asked for -- and the tag
        # is merged with the comment already there, never over it.
        tag_rows = self.tag_plan(tracks) if bool(payload.get("tags")) else {}
        report = writedb.write_set(
            tracks,
            plans,
            playlist=name,
            set_name=str(payload.get("name") or name),
            cues=with_cues,
            memory=bool(payload.get("memory")),
            dry_run=bool(payload.get("dry_run", True)),
            cue_policy=base_policy,
            cue_policies=policies,
            tags={key: row["tag"] for key, row in tag_rows.items()},
            db_path=db_path,
            backup_root=backup_root,
        )
        report["tag_plan"] = (
            tags_mod.tag_report(tag_rows, written=not report["dry_run"], dry_run=report["dry_run"])
            if tag_rows
            else None
        )
        # The write is the last moment a broken track can be caught, and the
        # report already says what will happen to every row -- so the check
        # rides along with it instead of being a button to remember.
        report["preflight"] = preflight_mod.inspect(
            tracks, check_cues=with_cues, set_name=str(payload.get("name") or name)
        )
        LOG.info(
            "write rekordbox (%s): %d cue rows, %d comment tag(s), %d kept (%s), "
            "playlist %r, backup %s",
            "dry run" if report["dry_run"] else "committed",
            report["cues"]["created"],
            report["tags"]["changed"],
            len(report["cues"].get("kept") or []),
            report.get("cue_policy") or base_policy,
            report["playlist"]["name"] if report.get("playlist") else "-",
            report.get("backup") or "none",
        )
        for issue in report["preflight"]["issues"][:5]:
            LOG.warning("pre-flight: %s | %s", issue["detail"], issue["label"])
        report["status"] = writedb.status(db_path)
        return report

    # ---------------------------------------------------------- library mgmt

    def library(self) -> dict:
        """Both libraries, as the management panel needs to see them.

        rekordbox's playlists come from the loaded library -- they are the same
        ids and names the tree already shows -- while AUTO/PILOT's are read from
        its own database, since Parity never loads that library.
        """
        library = self.store.library() or {}
        return {
            "loaded": bool(library),
            "source": library.get("source", ""),
            "source_kind": library.get("source_kind", ""),
            "rekordbox": {
                "playlists": _public_playlists(library.get("playlists") or []),
                "status": writedb.status(),
            },
            "autopilot": autopilot.status(),
        }

    def _library_records(self, keys) -> list[dict]:
        records = self.store.tracks_by_key()
        return [records[key] for key in (keys or []) if key in records]

    def _reread_after_library_write(self, report: dict) -> None:
        """Re-read the library once a change has been committed.

        The manage panel and the sidebar list playlists from the *stored*
        library -- the copy Parity read at startup -- so without this a
        playlist deleted from master.db would keep showing up until the next
        launch.  A dry run changes nothing and reads nothing.
        """
        if report.get("dry_run"):
            return
        try:
            self.store.refresh_library()
        except Exception as exc:  # noqa: BLE001 - the write itself succeeded
            LOG.warning("the library could not be re-read after the write: %s", exc)

    @staticmethod
    def _rekordbox_playlist_id(value) -> str:
        """The ``djmdPlaylist.ID`` behind a library playlist id.

        The loaded library names its playlists ``pl:<ID>`` (see
        :func:`parity.rxdb.read_library`), which is what the UI hands back;
        master.db wants the bare id.  A bare id or a playlist name passes
        through untouched.
        """
        text = str(value or "")
        return text[3:] if text.startswith("pl:") else text

    def _library_payload(self, payload: dict) -> tuple[str, bool, dict]:
        """Shared plumbing for the library edits: target, dry run, paths."""
        target = str(payload.get("target") or "rekordbox").strip().lower()
        if target not in {"rekordbox", "autopilot"}:
            raise ValueError(f"Unknown library: {target}")
        options = {
            "dry_run": bool(payload.get("dry_run", True)),
            "backup_root": Path(payload["backup_dir"]).expanduser()
            if payload.get("backup_dir")
            else None,
        }
        if target == "autopilot":
            options["db_path"] = (
                Path(payload["autopilot_db"]).expanduser() if payload.get("autopilot_db") else None
            )
        else:
            options["db_path"] = (
                Path(payload["db_path"]).expanduser() if payload.get("db_path") else None
            )
        return target, bool(payload.get("dry_run", True)), options

    def library_playlist(self, action: str, payload: dict | None = None) -> dict:
        """Rename or delete a playlist in either library."""
        payload = payload or {}
        target, dry_run, options = self._library_payload(payload)
        playlist_id = payload.get("id")
        if playlist_id in (None, ""):
            raise ValueError("No playlist chosen.")
        if target == "rekordbox":
            playlist_id = self._rekordbox_playlist_id(playlist_id)
        if action == "rename":
            function = (
                autopilot.rename_playlist if target == "autopilot" else writedb.rename_playlist
            )
            report = function(playlist_id, payload.get("name"), **options)
        elif action == "delete":
            function = (
                autopilot.delete_playlist if target == "autopilot" else writedb.delete_playlist
            )
            report = function(playlist_id, **options)
        else:
            raise ValueError(f"Unknown playlist action: {action}")
        LOG.info(
            "%s %s playlist %r in %s (%s)",
            action,
            target,
            (report.get("playlist") or {}).get("name", playlist_id),
            report["database"],
            "dry run" if dry_run else "committed",
        )
        self._reread_after_library_write(report)
        return report

    def library_remove_tracks(self, payload: dict | None = None) -> dict:
        """Take tracks out of a playlist in either library."""
        payload = payload or {}
        target, dry_run, options = self._library_payload(payload)
        playlist_id = payload.get("id")
        if playlist_id in (None, ""):
            raise ValueError("No playlist chosen.")
        records = self._library_records(payload.get("keys"))
        if not records:
            raise ValueError("No tracks selected to remove.")
        if target == "autopilot":
            paths = autopilot.track_ids_for_paths(
                [record.get("path") for record in records], options.get("db_path")
            )
            missing = [
                record for record in records if str(record.get("path")) not in paths
            ]
            report = autopilot.remove_playlist_tracks(
                playlist_id, list(paths.values()), **options
            )
            report["skipped"].extend(
                {"id": record.get("key"), "label": record.get("title"), "reason": "not in AUTO/PILOT"}
                for record in missing
            )
        else:
            report = writedb.remove_playlist_tracks(
                self._rekordbox_playlist_id(playlist_id),
                [str(record.get("id")) for record in records],
                **options,
            )
        LOG.info(
            "remove %d tracks from %s playlist %r (%s)",
            report["removed"],
            target,
            (report.get("playlist") or {}).get("name", playlist_id),
            "dry run" if dry_run else "committed",
        )
        self._reread_after_library_write(report)
        return report

    def library_delete_tracks(self, payload: dict | None = None) -> dict:
        """Delete tracks from either library; the files stay on disk."""
        payload = payload or {}
        target, dry_run, options = self._library_payload(payload)
        records = self._library_records(payload.get("keys"))
        if not records:
            raise ValueError("No tracks selected to delete.")
        if target == "autopilot":
            paths = autopilot.track_ids_for_paths(
                [record.get("path") for record in records], options.get("db_path")
            )
            report = autopilot.delete_tracks(list(paths.values()), **options)
            report["skipped"].extend(
                {
                    "id": record.get("key"),
                    "label": record.get("title"),
                    "reason": "not in AUTO/PILOT",
                }
                for record in records
                if str(record.get("path")) not in paths
            )
        else:
            report = writedb.delete_tracks(
                [str(record.get("id")) for record in records], **options
            )
        LOG.info(
            "delete %d tracks from %s (%s)",
            report["removed"],
            target,
            "dry run" if dry_run else "committed",
        )
        self._reread_after_library_write(report)
        return report


def _download_dir() -> Path:
    """Where a locally saved export should land."""
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def _playlist_track_ids(playlists: list[dict], playlist_id: str) -> list[str]:
    """Track ids for a playlist id, walking folder children depth-first."""

    def walk(node: dict, collected: list[str]) -> None:
        collected.extend(node.get("track_ids") or [])
        for child in node.get("children") or []:
            walk(child, collected)

    def find(nodes: list[dict]) -> list[str] | None:
        for node in nodes:
            if node.get("id") == playlist_id:
                collected: list[str] = []
                walk(node, collected)
                return collected
            found = find(node.get("children") or [])
            if found is not None:
                return found
        return None

    return find(playlists) or []


def _public_playlists(playlists: list[dict]) -> list[dict]:
    """Playlist tree for the UI -- counts only, no track id lists."""
    return [
        {
            "id": node.get("id"),
            "name": node.get("name"),
            "count": node.get("count", 0),
            "total": node.get("total", 0),
            "is_folder": node.get("is_folder", False),
            "children": _public_playlists(node.get("children") or []),
        }
        for node in playlists
    ]


class Handler(BaseHTTPRequestHandler):
    server_version = "Parity"
    protocol_version = "HTTP/1.1"

    # Bound by :func:`serve`.
    api: Api = None  # type: ignore[assignment]
    data_dir: Path | None = None

    def log_message(self, fmt: str, *args) -> None:  # quieter, single-line logs
        print(f"[parity] {self.address_string()} {fmt % args}")

    # ------------------------------------------------------------- plumbing

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route.startswith("/api/"):
                return self._handle_api_get(route, parse_qs(parsed.query))
            return self._serve_static(route)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except writedb.WriteBlocked as exc:
            self._send_json({"error": str(exc), "blocked": True}, status=409)
        except Exception as exc:  # noqa: BLE001 - never kill the server loop
            self._send_json({"error": f"Server error: {exc}"}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            return self._handle_api_post(parsed.path, payload)
        except writedb.WriteBlocked as exc:
            # The app is open or the database is missing: the request was fine,
            # the timing was not.
            self._send_json({"error": str(exc), "blocked": True}, status=409)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"Server error: {exc}"}, status=500)

    def _read_json(self) -> dict:
        length_header = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_header)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("That file is too large.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Malformed JSON request.") from exc
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    # ----------------------------------------------------------------- api

    def _handle_api_get(self, route: str, query: dict[str, list[str]]) -> None:
        api = self.api
        if route == "/api/state":
            return self._send_json(api.state())
        if route == "/api/tracks":
            playlist_id = (query.get("playlist") or [None])[0]
            return self._send_json(api.tracks(playlist_id))
        if route == "/api/cues":
            key = (query.get("key") or [None])[0]
            if not key:
                raise ValueError("No track requested.")
            return self._send_json(api.cue_plan(key))
        if route == "/api/waveform":
            key = (query.get("key") or [None])[0]
            force = str((query.get("force") or [""])[0]).lower() in {"1", "true", "yes"}
            return self._send_json(api.waveform(key or "", force))
        if route == "/api/audio":
            key = (query.get("key") or [None])[0]
            try:
                path = api.audio_path(key or "")
            except ValueError as exc:
                # A missing file is a 404, so the UI can tell it apart from a
                # file the browser refuses to decode.
                return self._send_json({"error": str(exc)}, status=404)
            return self._serve_audio(path, key or "")
        if route == "/api/energy/audio":
            return self._send_json(api.audio_status())
        if route == "/api/analyze":
            return self._send_json(api.analysis_status())
        if route == "/api/watch":
            return self._send_json(api.watch_status())
        if route == "/api/ingest":
            return self._send_json(api.ingest_status())
        if route == "/api/sets":
            return self._send_json({"sets": api.store.list_sets()})
        if route == "/api/library":
            return self._send_json(api.library())
        if route == "/api/preflight":
            keys = [key for key in (query.get("key") or []) if key]
            return self._send_json(
                api.preflight({"order": keys, "cues": (query.get("cues") or ["1"])[0] != "0"})
            )
        if route == "/api/rekordbox":
            db_path = (query.get("db") or [None])[0]
            return self._send_json(api.rekordbox_status(Path(db_path).expanduser() if db_path else None))
        if route == "/api/write/status":
            return self._send_json(api.write_status())
        return self._send_json({"error": "Unknown endpoint"}, status=404)

    def _handle_api_post(self, route: str, payload: dict) -> None:
        api = self.api
        if route == "/api/import":
            if payload.get("path"):
                path = Path(payload["path"]).expanduser()
                if not path.is_file():
                    raise ValueError(f"No file at {path}")
                xml_text = path.read_text(encoding="utf-8", errors="replace")
                return self._send_json(api.import_xml(xml_text, source=str(path)))
            xml_text = payload.get("xml") or ""
            if not xml_text.strip():
                raise ValueError("No XML supplied.")
            return self._send_json(api.import_xml(xml_text, source=payload.get("source") or "upload"))

        if route == "/api/reload":
            # Follows the origin: a library read from master.db is re-read from
            # master.db, never silently swapped for a leftover XML export.
            if not api.store.refresh_library():
                raise ValueError("No stored library to reload.")
            return self._send_json(api.state())

        if route == "/api/rekordbox/connect":
            return self._send_json(api.connect_rekordbox(payload))

        if route == "/api/write/rekordbox":
            return self._send_json(api.write_rekordbox(payload))

        if route in {"/api/library/playlist/delete", "/api/library/playlist/rename"}:
            action = route.rsplit("/", 1)[-1]
            return self._send_json(api.library_playlist(action, payload))
        if route == "/api/library/playlist/remove-tracks":
            return self._send_json(api.library_remove_tracks(payload))
        if route == "/api/library/tracks/delete":
            return self._send_json(api.library_delete_tracks(payload))

        if route == "/api/pool":
            return self._send_json(api.pool(payload))

        if route == "/api/energy/audio":
            return self._send_json(api.analyse_audio(payload))
        if route == "/api/analyze":
            return self._send_json(api.analyse_track_audio(payload))
        if route == "/api/import/m3u":
            return self._send_json(api.import_m3u(payload))
        if route == "/api/watch":
            return self._send_json(api.watch_configure(payload))
        if route == "/api/watch/tick":
            return self._send_json(api.watch_tick())
        if route == "/api/analyze/clear":
            api.store.clear_track_analysis()
            api._analysis_version = -1
            return self._send_json({"cleared": True, "status": api.analysis_status()})
        if route == "/api/ingest":
            return self._send_json(api.ingest_scan(payload))
        if route == "/api/ingest/run":
            return self._send_json(api.ingest_run(payload))

        if route == "/api/energy/audio/clear":
            api.store.clear_audio_analysis()
            return self._send_json({"cleared": True, "status": api.audio_status()})

        if route == "/api/cues":
            return self._send_json(api.analyse(payload))

        if route == "/api/cues/edit":
            return self._send_json(api.edit_cue(payload))

        if route == "/api/cues/clear":
            api.store.clear_cue_plans()
            return self._send_json({"cleared": True, "state": api.state()})

        if route == "/api/pipeline":
            return self._send_json(api.pipeline(payload))

        if route == "/api/energy":
            return self._send_json(api.set_energy(payload))
        if route == "/api/cue-policy":
            return self._send_json(api.set_cue_policy(payload))
        if route == "/api/track/edit":
            return self._send_json(api.edit_track(payload))
        if route == "/api/build":
            return self._send_json(api.build(payload))
        if route == "/api/optimize":
            return self._send_json(api.optimize(payload))

        if route == "/api/duplicates":
            return self._send_json(api.duplicates(payload))
        if route == "/api/duplicates/remove":
            return self._send_json(api.remove_duplicates(payload))

        if route == "/api/preflight":
            return self._send_json(api.preflight(payload))

        if route == "/api/tags":
            return self._send_json(api.set_tags(payload))

        if route == "/api/export":
            if payload.get("save"):
                return self._send_json(api.save_export(payload))
            content, filename, content_type = api.export(payload)
            return self._send_bytes(content, content_type, filename)

        if route == "/api/sets":
            return self._send_json({"set": api.store.save_set(payload)})
        if route == "/api/sets/delete":
            set_id = payload.get("id") or ""
            return self._send_json({"deleted": api.store.delete_set(set_id)})

        if route == "/api/reset":
            api.store.reset()
            return self._send_json(api.state())

        return self._send_json({"error": "Unknown endpoint"}, status=404)

    # -------------------------------------------------------------- output

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(
        self, body: bytes, content_type: str, filename: str | None = None, status: int = 200
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        """Headers only. The UI uses this to tell a missing file from a bad format."""
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/audio":
                key = (parse_qs(parsed.query).get("key") or [None])[0]
                return self._serve_audio(self.api.audio_path(key or ""), key or "", send_body=False)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=404)
        except Exception as exc:  # noqa: BLE001 - never kill the server loop
            self._send_json({"error": f"Server error: {exc}"}, status=500)

    def _serve_audio(self, path: Path, key: str, send_body: bool = True) -> None:
        """Stream a track file, honouring a single-range ``Range`` request.

        Seeking in an <audio> element means byte ranges, and a 5-minute WAV is
        tens of megabytes -- so the file is streamed in chunks instead of being
        read into memory.  The response is always ``Accept-Ranges: bytes``, so
        the browser knows it can scrub.
        """
        size = path.stat().st_size
        content_type = (
            AUDIO_TYPES.get(path.suffix.lower())
            or mimetypes.guess_type(str(path))[0]
            or "application/octet-stream"
        )
        start, end, status = 0, size - 1, 200

        header = (self.headers.get("Range") or "").strip()
        if header:
            match = RANGE_RE.match(header)
            if match is None:
                # An unparsable Range is ignored, which is what RFC 9110 says.
                pass
            else:
                first, last = match.group(1), match.group(2)
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    start = max(0, size - int(last))
                    end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206

        length = end - start + 1
        LOG.debug("preview %s: bytes %d-%d of %d", Path(path).name, start, end, size)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        if not send_body:
            return

        remaining = length
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    chunk = handle.read(min(AUDIO_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # The user pressed stop, or seeked: entirely normal here.
            LOG.debug("preview stopped early for %s", key)

    def _serve_static(self, route: str) -> None:
        if route in {"", "/"}:
            route = "/index.html"
        if route == "/favicon.ico":
            return self._send_bytes(b"", "image/x-icon", status=204)

        relative = unquote(route).lstrip("/")
        target = (WEB_DIR / relative).resolve()
        # Containment, not a string prefix: "/../webX" resolves to a sibling of
        # the web folder, and "/x/webX/y".startswith("/x/web") is true, so a
        # prefix test would serve it.  is_relative_to compares path components.
        if not target.is_relative_to(WEB_DIR) or not target.is_file():
            return self._send_json({"error": "Not found"}, status=404)

        content_type = (
            _CONTENT_TYPES.get(target.suffix)
            or mimetypes.guess_type(str(target))[0]
            or "application/octet-stream"
        )
        self._send_bytes(target.read_bytes(), content_type)


def serve(host: str = "127.0.0.1", port: int = 8765, data_dir: Path | None = None) -> ThreadingHTTPServer:
    """Start the server and return it (already bound, not yet shutting down)."""
    configure_logging()
    api = Api(Store(data_dir))
    handler = type("BoundHandler", (Handler,), {"api": api, "data_dir": data_dir})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.api = api  # type: ignore[attr-defined] - so shutdown can stop the watcher
    if api.store.watch_settings().get("enabled"):
        api._start_watch()
    return httpd


def serve_forever(host: str = "127.0.0.1", port: int = 8765, data_dir: Path | None = None) -> None:
    httpd = serve(host, port, data_dir)
    print(f"[parity] running at http://{host}:{port} (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[parity] stopped")
    finally:
        api = getattr(httpd, "api", None)
        if api is not None:
            api._stop_watch()
        httpd.server_close()


def start_in_thread(host: str, port: int, data_dir: Path | None = None) -> ThreadingHTTPServer:
    httpd = serve(host, port, data_dir)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd
