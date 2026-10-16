"""Set tags, written into the comment field.

rekordbox has no field for "which set was this, and what did it do there", so
the tag goes where every DJ tool looks for it: the comment.  Three facts, one
line, greppable in rekordbox's comment search and in the exported XML:

    [E7] [peak] 14/24

* ``[E7]``    the track's energy as a whole number, 1 to 10.  This is the same
  tag :func:`parity.energy.tag_energy` already reads, so a set tagged here
  is read back by Parity itself on the next import.
* ``[peak]``  the part it played in this set: ``opener``, ``build``, ``peak``
  or ``closer``.
* ``14/24``   where it sat: track 14 of 24.

**Nothing the user wrote is ever lost.**  A tag is *appended* to whatever is
already in the comment, and only Parity's own tag is removed on a re-write
(see :func:`merge_comment`), so tagging a set twice does not stack two tags and
a comment the DJ typed survives any number of writes.  The tag is also fully
reversible: :func:`strip_tag` is the whole of what a write adds.

The role is deliberately positional rather than a guess about the music: the
set opens with the opener and closes with the closer, anything as loud as the
set's own peak is a peak, and everything else is a build -- including the slide
down after the peak, because one word has to carry the arc and the exact energy
of every track is in the package's manifest beside it.
"""

from __future__ import annotations

import math
import re

#: The roles a track can play, in set order.  Kept to four so a tag stays
#: readable and a filter in rekordbox stays useful.
SET_TAG_ROLES: tuple[str, ...] = ("opener", "build", "peak", "closer")

#: How close to the set's own maximum energy a track has to be to count as a
#: peak.  Half a point is inside the noise of the estimate and wide enough that
#: a plateau of peak-time tracks is tagged as one.
PEAK_BAND = 0.5

#: The lowest and highest energy band a tag can carry.
MIN_BAND = 1
MAX_BAND = 10

#: Parity's own tag, exactly as it is written.  Spacing is tolerated on the
#: way in (a tag that has been through a text field or a rekordbox round trip
#: may have lost it), the way out is always the canonical form.
PARITY_TAG_RE = re.compile(
    r"\[\s*E\s*(?P<energy>\d{1,2}(?:\.\d)?)\s*\]"
    r"\s*\[\s*(?P<role>" + "|".join(SET_TAG_ROLES) + r")\s*\]"
    r"\s*(?P<position>\d+)\s*/\s*(?P<total>\d+)",
    re.IGNORECASE,
)


def energy_band(value: object) -> int:
    """The energy reading as a whole number band 1-10.

    Rounded half-up rather than Python's round(), which rounds 6.5 down: a tag
    that sometimes says E6 and sometimes E7 for the same value is worse than a
    tag that is one band off, because the DJ cannot trust either.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 5
    if math.isnan(number):
        return 5
    return max(MIN_BAND, min(MAX_BAND, int(math.floor(number + 0.5))))


def format_tag(band: int, role: str, position: int, total: int) -> str:
    """One tag, in the canonical form: ``[E7] [peak] 14/24``."""
    name = str(role or "").strip().lower()
    if name not in SET_TAG_ROLES:
        name = "build"
    return f"[E{energy_band(band)}] [{name}] {int(position)}/{int(total)}"


def parse_tag(comment: str) -> dict | None:
    """The tag already in a comment, or ``None``.

    Used to report which tracks still carry a tag from another set, and to let
    the writer replace its own tag instead of appending a second one.
    """
    match = PARITY_TAG_RE.search(comment or "")
    if match is None:
        return None
    return {
        "energy": energy_band(match.group("energy")),
        "role": match.group("role").lower(),
        "position": int(match.group("position")),
        "total": int(match.group("total")),
        "text": match.group(0),
    }


def strip_tag(comment: str) -> str:
    """The comment without Parity's own tag, whitespace tidied.

    Everything else in the field -- the DJ's own words, another tool's tags --
    is left exactly as it was; only trailing space left by a removed tag goes.
    """
    cleaned = PARITY_TAG_RE.sub(" ", comment or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+$", "", cleaned.strip())
    return cleaned.strip()


def merge_comment(existing: str, tag: str) -> str:
    """``existing`` with ``tag`` on the end, our own previous tag dropped."""
    base = strip_tag(existing)
    if not tag:
        return base
    return f"{base} {tag}".strip() if base else tag


def role_for(position: int, total: int, energy: float, peak_energy: float) -> tuple[str, str]:
    """The part one track plays in the set, and why.

    Position decides the ends of the set -- the first track opens it, the last
    one closes it -- and energy decides the peak; everything in between is a
    build.  The reason is carried with the tag so the report can explain a tag
    rather than just assert it.
    """
    position = max(1, int(position))
    total = max(1, int(total))
    if position == 1:
        return "opener", "the set opens with it"
    if position >= total:
        return "closer", "the set closes with it"
    if float(energy or 0) >= float(peak_energy or 0) - PEAK_BAND:
        return "peak", (
            f"within {PEAK_BAND:g} of the set's peak energy ({float(peak_energy or 0):g})"
        )
    return "build", "between the opener and the peak"


def _label(track: dict) -> str:
    artist = str(track.get("artist") or "").strip()
    title = str(track.get("title") or "").strip() or "Untitled"
    return f"{artist} - {title}" if artist else title


def plan_tags(tracks: list[dict]) -> dict[str, dict]:
    """A tag for every track in an ordered set, by track key.

    ``tracks`` is the set as the API hands it over: ``key``, ``energy``, the
    names, and the comment the track already carries (``comments``).  Every
    entry carries the comment *before* and *after* the write, so a report can
    show what actually changed rather than only what was added.
    """
    rows = list(tracks)
    total = len(rows)
    if not total:
        return {}
    energies = [float(track.get("energy") or 0) for track in rows]
    peak_energy = max(energies) if energies else 0.0

    plan: dict[str, dict] = {}
    for index, track in enumerate(rows, start=1):
        key = str(track.get("key") or "")
        if not key:
            continue
        energy = float(track.get("energy") or 0)
        role, reason = role_for(index, total, energy, peak_energy)
        band = energy_band(energy)
        tag = format_tag(band, role, index, total)
        existing = str(track.get("comments") or "")
        previous = parse_tag(existing)
        plan[key] = {
            "key": key,
            "label": _label(track),
            "position": index,
            "total": total,
            "energy": round(energy, 1),
            "band": band,
            "role": role,
            "reason": reason,
            "tag": tag,
            "before": existing,
            "after": merge_comment(existing, tag),
            # A tag from an earlier set is the normal case: a track gets used
            # in more than one set, and saying so is the point of a report.
            "previous": previous,
        }
    return plan


def tag_report(plan: dict[str, dict], *, written: bool = False, dry_run: bool = False) -> dict:
    """The tags as a report: how many, which roles, and every row."""
    rows = [plan[key] for key in sorted(plan, key=lambda k: plan[k]["position"])]
    roles: dict[str, int] = {}
    for row in rows:
        roles[row["role"]] = roles.get(row["role"], 0) + 1
    return {
        "written": bool(written),
        "dry_run": bool(dry_run),
        "count": len(rows),
        "roles": roles,
        "replaced": sum(1 for row in rows if row.get("previous")),
        "rows": rows,
    }
