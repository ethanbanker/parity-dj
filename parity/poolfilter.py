"""Pool filtering: pick a target playlist or folder, purge what cannot be played.

This is the gate between "everything rekordbox knows about" and "the tracks
that belong in this set".  It removes, and reports on, four kinds of noise:

* **streaming stubs** -- SoundCloud / Tidal / Beatport entries rekordbox stores
  as ``file://localhostsoundcloud:tracks:123`` with no file behind them;
* **broken paths** -- a local path that no longer exists (external drive not
  mounted, file renamed);
* **anything outside the chosen playlist or folder**;
* **duplicate paths**, so an export never lists the same file twice.

Every removal carries a reason, and the report is what the UI shows, so nothing
ever disappears silently.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("parity.pool")

#: Location prefixes that are never playable files.
REMOTE_PREFIXES = ("http://", "https://", "soundcloud:", "spotify:", "beatport:")

#: Everything rekordbox can put in front of a streaming id.
REMOTE_PATTERN = re.compile(r"^(https?://|soundcloud:|spotify:|beatport:|tidal:)", re.IGNORECASE)


@dataclass
class FilterConfig:
    """What the user selected in the UI."""

    playlist_id: str | None = None
    folder: str | None = None
    include_subfolders: bool = True
    local_only: bool = True
    require_file: bool = True
    dedupe: bool = True
    keys: list[str] | None = None  # explicit selection wins over playlist/folder

    def to_dict(self) -> dict:
        return {
            "playlist_id": self.playlist_id,
            "folder": self.folder,
            "include_subfolders": self.include_subfolders,
            "local_only": self.local_only,
            "require_file": self.require_file,
            "dedupe": self.dedupe,
            "keys": list(self.keys) if self.keys else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FilterConfig":
        known = {f for f in cls().to_dict()}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        if clean.get("keys"):
            clean["keys"] = [str(k) for k in clean["keys"]]
        return cls(**clean)


@dataclass
class Removed:
    key: str
    label: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "reason": self.reason, "detail": self.detail}


@dataclass
class FilterResult:
    kept: list[dict] = field(default_factory=list)
    removed: list[Removed] = field(default_factory=list)
    scanned: int = 0

    @property
    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.removed:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts

    def report(self) -> dict:
        return {
            "scanned": self.scanned,
            "kept": len(self.kept),
            "removed": len(self.removed),
            "reasons": self.reasons,
            "examples": [item.to_dict() for item in self.removed[:25]],
        }


def is_remote(location: str) -> bool:
    """True when a rekordbox ``Location`` can never point at a file.

    Streaming entries arrive in two shapes: a service URL
    (``https://…``, ``spotify:…``) or a file URL whose host *is* the service
    (``file://localhostsoundcloud:tracks:123``).  Both count as remote.
    """
    text = (location or "").strip()
    if not text:
        return False
    if REMOTE_PATTERN.match(text):
        return True
    if text.lower().startswith("file://"):
        rest = text[len("file://"):]
        if rest.lower().startswith("localhost"):
            rest = rest[len("localhost"):]
        # A local file URL always continues with an absolute path.
        return not rest.startswith("/")
    return False


def missing_file(path: str, exists=os.path.isfile) -> bool:
    if not path:
        return True
    return not exists(path)


def in_folder(path: str, folder: str, include_subfolders: bool = True) -> bool:
    """Whether ``path`` sits in ``folder`` (or below it)."""
    if not folder:
        return True
    if not path:
        return False
    parent = os.path.normpath(folder)
    candidate = os.path.normpath(path)
    if candidate == parent:
        return True
    if not candidate.startswith(parent.rstrip(os.sep) + os.sep):
        return False
    if include_subfolders:
        return True
    return os.path.dirname(candidate) == parent


def apply_filter(
    views: list[dict],
    config: FilterConfig,
    playlist_keys: set[str] | None = None,
) -> FilterResult:
    """Filter track views (see :meth:`parity.server.Api.all_views`).

    ``playlist_keys`` is the set of track keys belonging to
    ``config.playlist_id``; the caller resolves it because only the store knows
    the playlist tree.
    """
    result = FilterResult(scanned=len(views))
    selected = set(config.keys) if config.keys else None
    seen_paths: dict[str, str] = {}

    for view in views:
        key = view.get("key", "")
        label = view.get("label") or view.get("title") or key
        path = view.get("path") or ""
        location = view.get("location") or ""

        if selected is not None and key not in selected:
            continue

        # The caller only supplies playlist keys when a playlist was chosen, so
        # a plain `None` means "no playlist restriction".  An explicit selection
        # is already the answer and is not narrowed further.
        if selected is None and playlist_keys is not None and key not in playlist_keys:
            continue

        if config.folder and not in_folder(path, config.folder, config.include_subfolders):
            continue

        if config.local_only:
            if view.get("streaming") or is_remote(location) or not path:
                result.removed.append(
                    Removed(key, label, "streaming", location or "no file location")
                )
                continue

        if config.require_file and missing_file(path):
            result.removed.append(Removed(key, label, "missing_file", path))
            continue

        if config.dedupe:
            normalised = os.path.normcase(os.path.normpath(path))
            previous = seen_paths.get(normalised)
            if previous is not None:
                result.removed.append(Removed(key, label, "duplicate", path))
                continue
            seen_paths[normalised] = key

        result.kept.append(view)

    log.info(
        "pool filter scanned=%d kept=%d removed=%d reasons=%s",
        result.scanned,
        len(result.kept),
        len(result.removed),
        result.reasons,
    )
    for item in result.removed:
        log.debug("pool filter dropped %s (%s): %s", item.label, item.reason, item.detail)
    return result


def folder_options(views: list[dict], limit: int = 200) -> list[dict]:
    """Distinct folders in the library with track counts, for a picker."""
    counts: dict[str, dict] = {}
    for view in views:
        folder = view.get("folder") or ""
        if not folder:
            continue
        entry = counts.setdefault(folder, {"folder": folder, "local": 0, "streaming": 0, "total": 0})
        entry["total"] += 1
        if view.get("streaming"):
            entry["streaming"] += 1
        else:
            entry["local"] += 1
    ordered = sorted(counts.values(), key=lambda item: (-item["local"], item["folder"]))
    return ordered[:limit]


def volume_warning(folder: str) -> str:
    """Warn when a selected folder lives on an unmounted volume.

    ``/Volumes/EXT SSD/RekordBox Library`` depends on an external drive being
    plugged in, so a filter that matches nothing is usually this rather than an
    empty folder.
    """
    if not folder:
        return ""
    parts = Path(folder).parts
    if len(parts) >= 3 and parts[0] == os.sep and parts[1] == "Volumes":
        volume = Path(os.sep) / "Volumes" / parts[2]
        if not volume.exists():
            return f"{volume} is not mounted; no file under {folder} can be read."
    return ""
