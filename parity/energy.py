"""Energy estimation.

Rekordbox does not export an energy column, so energy is derived from what the
export *does* contain, in this order of trust:

1. A value the user typed in the UI (``override``)
2. An explicit tag in the ``Comments`` field (``E7``, ``Energy: 8``, ``[6/10]``)
3. **The audio itself**, when it has been measured -- see :mod:`parity.audio`,
   which reads loudness, brightness, dynamic range and drive off the file
4. The star rating, which DJs overwhelmingly use as a crude energy marker
5. The genre, mapped to a typical intensity
6. A small BPM nudge, because a 135 BPM techno cut is not a 100 BPM one

A hand-typed tag sits above the measurement because it is a decision the user
made; everything below it is a guess the measurement beats.  Every estimate
carries a confidence label and its source, so the UI can show which of these won,
and overrides are stored locally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .rekordbox import Track

MIN_ENERGY = 1.0
MAX_ENERGY = 10.0

# Typical intensity per genre, matched as substrings against the genre field,
# longest pattern first so "tech house" beats "house".
GENRE_ENERGY: dict[str, float] = {
    "hard techno": 9.4,
    "hardstyle": 9.8,
    "hardcore": 9.9,
    "schranz": 9.2,
    "industrial techno": 9.0,
    "peak time techno": 9.0,
    "drum & bass": 8.6,
    "drum n bass": 8.6,
    "dnb": 8.6,
    "jungle": 8.5,
    "dubstep": 8.2,
    "riddim": 8.4,
    "trap": 7.4,
    "techno": 8.2,
    "psytrance": 8.6,
    "hardgroove": 8.6,
    "big room": 8.4,
    "electro house": 8.0,
    "electro": 7.4,
    "breakbeat": 7.4,
    "trance": 7.6,
    "progressive trance": 7.2,
    "garage": 6.6,
    "bass house": 8.0,
    "uk garage": 6.6,
    "tech house": 6.6,
    "bassline": 7.2,
    "house": 6.0,
    "afro house": 6.2,
    "melodic house": 5.6,
    "organic house": 5.0,
    "progressive house": 6.2,
    "deep house": 4.6,
    "minimal": 4.6,
    "minimal house": 4.6,
    "lo-fi": 2.6,
    "lofi": 2.6,
    "disco": 5.8,
    "nu disco": 5.8,
    "funk": 5.4,
    "soul": 4.4,
    "hip hop": 5.4,
    "hip-hop": 5.4,
    "rap": 5.4,
    "r&b": 4.6,
    "pop": 5.4,
    "reggaeton": 6.6,
    "dancehall": 6.4,
    "amapiano": 5.8,
    "moombahton": 7.0,
    "reggae": 3.6,
    "dub": 3.0,
    "downtempo": 3.0,
    "trip hop": 3.4,
    "chillout": 2.2,
    "chill": 2.2,
    "ambient": 1.8,
    "breakcore": 9.6,
    "gabber": 9.8,
    "country": 3.4,
    "rock": 6.0,
    "metal": 8.4,
    "classical": 3.0,
    "jazz": 3.6,
    "world": 4.6,
    "latin": 6.0,
    "salsa": 6.4,
}

# "E7", "E 7", "energy 7", "energy: 7/10", "[7]", "EN7"
_TAG_PATTERNS = (
    re.compile(r"\benergy\s*[:=\-]?\s*(\d{1,2}(?:\.\d)?)\b", re.IGNORECASE),
    re.compile(r"(?:^|[\s\[\(|])e\s*[:=\-]?\s*(\d{1,2}(?:\.\d)?)\s*(?:/10|/\s*10)?(?:[\s\]\)|,]|$)", re.IGNORECASE),
    re.compile(r"\[\s*(\d{1,2})\s*/\s*10\s*\]"),
)


@dataclass
class EnergyEstimate:
    value: float
    source: str  # "override" | "tag" | "rating" | "genre" | "bpm" | "default"
    confidence: str  # "high" | "medium" | "low"

    @property
    def needs_review(self) -> bool:
        return self.confidence == "low"


def tag_energy(comments: str) -> float | None:
    """Read an explicit energy tag out of the comments field."""
    if not comments:
        return None
    for pattern in _TAG_PATTERNS:
        for match in pattern.finditer(comments):
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if MIN_ENERGY <= value <= 10:
                return round(value, 1)
            if 11 <= value <= 100:
                # Some people tag energy 0-100.
                return round(max(MIN_ENERGY, min(MAX_ENERGY, value / 10)), 1)
    return None


def genre_energy(genre: str) -> float | None:
    if not genre:
        return None
    lowered = genre.lower()
    best: tuple[int, float] | None = None
    for name, value in GENRE_ENERGY.items():
        if name in lowered and (best is None or len(name) > best[0]):
            best = (len(name), value)
    return best[1] if best else None


def estimate(
    track: Track,
    override: float | None = None,
    audio: float | None = None,
) -> EnergyEstimate:
    """Best available energy value for ``track``.

    ``audio`` is the measured value for this track, when it has been analysed
    (:mod:`parity.audio`); ``None`` means it has not, and the metadata
    heuristics answer instead.
    """
    if override is not None:
        return EnergyEstimate(_clamp(override), "override", "high")

    tagged = tag_energy(track.comments)
    if tagged is not None:
        return EnergyEstimate(_clamp(tagged), "tag", "high")

    if audio is not None:
        return EnergyEstimate(_clamp(audio), "audio", "high")

    genre = genre_energy(track.genre)
    stars = track.stars

    if genre is not None and stars > 0:
        # Genre sets the neighbourhood, the rating moves within it.
        rating_shift = (stars - 2.5) * 0.7
        return EnergyEstimate(_clamp(genre + rating_shift), "rating", "medium")

    if genre is not None:
        value = genre
        if track.bpm:
            value += _bpm_nudge(track.bpm)
        return EnergyEstimate(_clamp(value), "genre", "medium")

    if stars > 0:
        return EnergyEstimate(_clamp(3.0 + stars * 1.1), "rating", "low")

    if track.bpm:
        # Thin evidence: only the tempo.
        return EnergyEstimate(_clamp(4.0 + _bpm_nudge(track.bpm)), "bpm", "low")

    return EnergyEstimate(5.0, "default", "low")


def _bpm_nudge(bpm: float) -> float:
    nudge = (bpm - 120.0) / 40.0
    return max(-1.5, min(1.5, nudge))


def _clamp(value: float) -> float:
    return round(max(MIN_ENERGY, min(MAX_ENERGY, value)), 1)
