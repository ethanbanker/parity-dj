"""Camelot wheel helpers.

Rekordbox stores key information in several different notations depending on the
user's display setting, and it can also be written by third party tools.  This
module normalises all of those into a single Camelot code (``"8A"``) and scores
how well two tracks sit together harmonically.

Supported input notations:

* Camelot:            ``8A``, ``8a``, ``8 A``
* Open Key:           ``8m`` (minor) / ``8d`` (major)
* Musical:            ``Am``, ``A min``, ``A minor``, ``C``, ``F#m``, ``Bbmaj``
* Bare numbers:       ``8`` is rejected -- we cannot know the mode.
"""

from __future__ import annotations

import re

MINOR = "A"
MAJOR = "B"

# Index 1..12 -> canonical spelling for each mode.
_MINOR_BY_NUMBER = {
    1: "Abm",
    2: "Ebm",
    3: "Bbm",
    4: "Fm",
    5: "Cm",
    6: "Gm",
    7: "Dm",
    8: "Am",
    9: "Em",
    10: "Bm",
    11: "F#m",
    12: "C#m",
}
_MAJOR_BY_NUMBER = {
    1: "B",
    2: "F#",
    3: "C#",
    4: "Ab",
    5: "Eb",
    6: "Bb",
    7: "F",
    8: "C",
    9: "G",
    10: "D",
    11: "A",
    12: "E",
}

CAMELOT_TO_KEY: dict[str, str] = {}
for _n, _name in _MINOR_BY_NUMBER.items():
    CAMELOT_TO_KEY[f"{_n}{MINOR}"] = _name
for _n, _name in _MAJOR_BY_NUMBER.items():
    CAMELOT_TO_KEY[f"{_n}{MAJOR}"] = _name

# Enharmonic aliases: every spelling of a key maps to the same Camelot code.
_ENHARMONIC = {
    "G#": "Ab",
    "D#": "Eb",
    "A#": "Bb",
    "Db": "C#",
    "Gb": "F#",
    "Cb": "B",
    "Fb": "E",
    "E#": "F",
    "B#": "C",
}

_KEY_TO_CAMELOT: dict[str, str] = {}
for _code, _name in CAMELOT_TO_KEY.items():
    _KEY_TO_CAMELOT[_name.lower()] = _code

# Every spelling of a key that is musically identical to a canonical one.
_KEY_ALIASES = {
    "abm": "1A",
    "g#m": "1A",
    "ebm": "2A",
    "d#m": "2A",
    "bbm": "3A",
    "a#m": "3A",
    "dbm": "12A",
    "cbm": "10A",
    "ab": "4B",
    "g#": "4B",
    "eb": "5B",
    "d#": "5B",
    "bb": "6B",
    "a#": "6B",
    "db": "3B",
    "gb": "2B",
    "cb": "1B",
}
_KEY_TO_CAMELOT.update(_KEY_ALIASES)

_CAMELOT_RE = re.compile(r"^\s*(\d{1,2})\s*([ab])\s*$", re.IGNORECASE)
_OPENKEY_RE = re.compile(r"^\s*(\d{1,2})\s*([md])\s*$", re.IGNORECASE)
_MUSICAL_RE = re.compile(
    r"^\s*([A-Ga-g])\s*([#♯b♭]?)\s*(m|min|minor|maj|major|d|dur)?\s*$",
    re.IGNORECASE,
)


def parse_key(raw: object) -> str | None:
    """Return the Camelot code for ``raw`` or ``None`` if it cannot be read.

    ``None`` is a normal answer: plenty of tracks are simply unanalysed.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    match = _CAMELOT_RE.match(text)
    if match:
        number, letter = int(match.group(1)), match.group(2).upper()
        if 1 <= number <= 12:
            return f"{number}{letter}"
        return None

    match = _OPENKEY_RE.match(text)
    if match:
        # Open Key: 'd' = dur = major, 'm' = moll = minor. Numbers match Camelot.
        number, letter = int(match.group(1)), match.group(2).lower()
        if 1 <= number <= 12:
            return f"{number}{MAJOR if letter == 'd' else MINOR}"
        return None

    match = _MUSICAL_RE.match(text)
    if match:
        note, accidental, quality = match.group(1), match.group(2) or "", (match.group(3) or "").lower()
        root = _ENHARMONIC.get(note.upper() + accidental, note.upper() + accidental)
        lowered = quality[:3]
        is_minor = lowered in {"m", "min"}
        lookup = (root + ("m" if is_minor else "")).lower()
        code = _KEY_TO_CAMELOT.get(lookup)
        if code is None and not is_minor:
            # "C major" / "Cmaj" -> plain root.
            code = _KEY_TO_CAMELOT.get(root.lower())
        return code

    return None


def parse_number(raw: object) -> int | None:
    """Numeric part of a Camelot code (1..12), or ``None``."""
    code = parse_key(raw)
    return int(code[:-1]) if code else None


def is_minor(code: str | None) -> bool | None:
    if not code:
        return None
    return code.endswith(MINOR)


def wheel_distance(a: str, b: str) -> int:
    """Shortest distance around the 12-step wheel (ignores A/B side)."""
    na, nb = parse_number(a), parse_number(b)
    if na is None or nb is None:
        return 99
    diff = abs(na - nb)
    return min(diff, 12 - diff)


def transpose(code: str, steps: int) -> str:
    """Move ``steps`` positions around the wheel, keeping the A/B side."""
    number = parse_number(code)
    if number is None:
        return code
    moved = ((number - 1 + steps) % 12) + 1
    return f"{moved}{code[-1].upper()}"


def compat_score(a: str | None, b: str | None) -> float:
    """How well track B follows track A harmonically, from 1.0 (ideal) to 0.0.

    Unknown keys score a neutral 0.5 so unanalysed tracks are usable rather than
    being pushed to the end of every set.
    """
    if not a or not b:
        return 0.5
    if a == b:
        return 1.0

    distance = wheel_distance(a, b)
    same_mode = a[-1].upper() == b[-1].upper()

    # Relative major/minor: same number, other side ("energy" move).
    if distance == 0:
        return 0.95
    # One step around the wheel: the classic safe mix.
    if distance == 1 and same_mode:
        return 0.9
    # Diagonal: one step + mode change. Still musical, lifts or drops the mood.
    if distance == 1:
        return 0.75
    # Two steps: a lift people feel, but not seamless.
    if distance == 2 and same_mode:
        return 0.5
    if distance == 2:
        return 0.35
    # Tritone / distant keys: avoid.
    return 0.0


def compat_label(a: str | None, b: str | None) -> str:
    """Human readable verdict for the transition A -> B."""
    if not a or not b:
        return "unknown"
    if a == b:
        return "perfect"
    distance = wheel_distance(a, b)
    same_mode = a[-1].upper() == b[-1].upper()
    if distance == 0:
        return "relative"
    if distance == 1 and same_mode:
        return "adjacent"
    if distance == 1:
        return "diagonal"
    if distance == 2:
        return "two-step"
    return "clash"
