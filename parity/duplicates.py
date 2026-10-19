"""Duplicate tracks: find them, decide which one stays, remove the rest.

A library ends up with duplicates in three ways, and they are not the same
problem:

* **one file, two rows** -- the same path imported twice, or a collection with a
  stale row beside a fresh one;
* **one row, two places** -- the same track key added to a set twice (which is
  the only kind a built set can hold);
* **two files, one track** -- an MP3 and the WAV of the same record, or a
  download and a re-download under another name.

They are grouped together with a union-find, so a track that is both the same
path and the same name is reported once with the strongest reason rather than
as two overlapping problems.  Nothing here deletes a file or a library row:
the plan says which *position* to keep and which ones to drop, and the caller
decides whether it is a set, the pool, or something else.  That is the same
conservatism as the pre-flight check, with the removal the user actually asked
for on top.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

#: The reason that wins when a group is several kinds at once, worst first.
KINDS = ("same_track", "same_file", "same_name")

KIND_TEXT = {
    "same_file": "One file, listed as two tracks",
    "same_track": "The same track listed twice",
    "same_name": "Same artist and title, different files",
}

KIND_SUMMARY = {
    "same_file": "one file, listed as two tracks",
    "same_track": "the same track takes two slots",
    "same_name": "looks like a duplicate import",
}

_NAME_JUNK = re.compile(r"[^0-9a-zA-Z\u00c0-\uffff]+")


def _label(track: dict) -> str:
    artist = str(track.get("artist") or "").strip()
    title = str(track.get("title") or "").strip() or "Untitled"
    return f"{artist} - {title}" if artist else title


def _norm_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        text = str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        pass
    return os.path.normcase(text)


def _norm_name(track: dict) -> str:
    """Artist and title as one comparable string, or ``""`` when unusable.

    A track with no title has nothing to compare: grouping every unnamed row
    together would report a whole pool as one duplicate.  Accents and other
    alphabets are kept -- they are part of the name -- while punctuation and
    case, which differ between a download and a re-tag, are not.
    """
    title = str(track.get("title") or "").strip()
    if not title:
        return ""
    raw = f"{track.get('artist') or ''} {title}"
    return " ".join(_NAME_JUNK.sub(" ", raw.casefold()).split())


def _key(track: dict) -> str:
    return str(track.get("key") or "")


def _keep_score(track: dict, index: int) -> tuple:
    """Lower is kept: a playable, named, already-cued track, earliest first."""
    cues = track.get("cues") or {}
    existing = track.get("existing_cues") or {}
    has_cues = bool(cues.get("analysed")) or bool(existing.get("hot"))
    return (
        0 if not track.get("streaming") else 1,
        0 if str(track.get("title") or "").strip() else 1,
        0 if has_cues else 1,
        -int(track.get("rating") or 0),
        index,
    )


class _Union:
    """Union-find over row indices, so overlapping reasons make one group."""

    def __init__(self, count: int):
        self.parent = list(range(count))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


def _edges(tracks: list[dict]) -> dict[str, list[tuple[str, int, int]]]:
    """Every duplicate relation, by kind, as ``(value, a, b)`` edges."""
    by_path: dict[str, list[int]] = {}
    by_key: dict[str, list[int]] = {}
    by_name: dict[str, list[int]] = {}

    for index, track in enumerate(tracks):
        path = _norm_path(track.get("path"))
        if path and not track.get("streaming"):
            by_path.setdefault(path, []).append(index)
        key = _key(track)
        if key:
            by_key.setdefault(key, []).append(index)
        name = _norm_name(track)
        if name:
            by_name.setdefault(name, []).append(index)

    edges: dict[str, list[tuple[str, int, int]]] = {kind: [] for kind in KINDS}
    for value, indices in by_path.items():
        edges["same_file"].extend((value, indices[0], other) for other in indices[1:])
    for value, indices in by_key.items():
        edges["same_track"].extend((value, indices[0], other) for other in indices[1:])
    for value, indices in by_name.items():
        # Two rows for one file are already a same-file group; a name edge
        # between them would just be the same problem said twice.
        paths = {_norm_path(tracks[index].get("path")) for index in indices}
        if len(paths) < 2 and all(paths):
            continue
        edges["same_name"].extend((value, indices[0], other) for other in indices[1:])
    return edges


def find_duplicates(tracks: list[dict]) -> list[dict]:
    """Group the duplicate rows in ``tracks``, in the order they were given.

    Each group carries every row, the one to keep (the best playable, named,
    cued row, then the earliest), and the positions that would go.  A group is
    reported once even when it is a duplicate by path *and* by name.
    """
    rows = list(tracks)
    union = _Union(len(rows))
    reasons: dict[tuple[int, int], str] = {}
    edges = _edges(rows)
    for kind in KINDS:
        for _value, a, b in edges[kind]:
            union.union(a, b)
            # The strongest kind seen anywhere in the component is the label.
            pair = (min(a, b), max(a, b))
            if pair not in reasons or KINDS.index(kind) < KINDS.index(reasons[pair]):
                reasons[pair] = kind

    components: dict[int, list[int]] = {}
    for index in range(len(rows)):
        components.setdefault(union.find(index), []).append(index)

    groups: list[dict] = []
    for indices in components.values():
        if len(indices) < 2:
            continue
        kinds = {reasons[pair] for pair in reasons if pair[0] in indices and pair[1] in indices}
        kind = next((candidate for candidate in KINDS if candidate in kinds), "same_name")
        keep = min(indices, key=lambda index: _keep_score(rows[index], index))
        members = [
            {
                "position": index + 1,
                "key": _key(rows[index]),
                "label": _label(rows[index]),
                "path": str(rows[index].get("path") or ""),
                "streaming": bool(rows[index].get("streaming")),
                "kept": index == keep,
            }
            for index in indices
        ]
        digest = hashlib.sha1(
            ":".join(str(index) for index in indices).encode("ascii")
        ).hexdigest()[:12]
        groups.append(
            {
                "id": f"{kind}:{digest}",
                "kind": kind,
                "reason": KIND_TEXT[kind],
                "summary": KIND_SUMMARY[kind],
                "keep_position": keep + 1,
                "positions": [index + 1 for index in indices],
                "tracks": members,
                "remove_positions": [index + 1 for index in indices if index != keep],
            }
        )

    groups.sort(key=lambda group: group["positions"][0])
    return groups


def report(tracks: list[dict]) -> dict:
    """The preview the API hands the UI: groups plus the count that would go."""
    groups = find_duplicates(tracks)
    removable = sum(len(group["remove_positions"]) for group in groups)
    return {
        "checked": len(tracks),
        "groups": groups,
        "group_count": len(groups),
        "duplicate_count": removable,
    }


def plan_removal(tracks: list[dict], keep: dict[str, int] | None = None) -> dict:
    """Return the order with duplicates dropped, keeping the chosen rows.

    ``keep`` maps a group id to a **1-based position** in ``tracks``: the UI
    sends the row the user picked.  A group id the incoming data no longer
    produces is ignored, and a position outside its group falls back to the
    group's own recommendation -- a stale choice must not remove the wrong row.
    """
    rows = list(tracks)
    groups = find_duplicates(rows)
    keep = keep or {}
    drop: set[int] = set()
    kept: list[dict] = []
    for group in groups:
        chosen = int(keep.get(group["id"]) or group["keep_position"])
        if chosen not in group["positions"]:
            chosen = group["keep_position"]
        kept.append(
            {
                "id": group["id"],
                "kind": group["kind"],
                "position": chosen,
                "label": _label(rows[chosen - 1]),
            }
        )
        drop.update(position for position in group["positions"] if position != chosen)

    order = [rows[index]["key"] for index in range(len(rows)) if index + 1 not in drop]
    removed = [
        {
            "position": position,
            "key": _key(rows[position - 1]),
            "label": _label(rows[position - 1]),
            "path": str(rows[position - 1].get("path") or ""),
        }
        for position in sorted(drop)
    ]
    return {
        "order": [key for key in order if key],
        "removed": removed,
        "removed_count": len(removed),
        "kept": kept,
        "groups": groups,
    }
