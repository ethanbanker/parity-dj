"""Track structure: where the intro, build, drop and mix-out actually are.

Two sources, best first:

1. **rekordbox phrase analysis.**  Every track rekordbox has analysed has an
   ANLZ file (``ANLZ0000.DAT``/``.EXT``) carrying a beat grid (``PQTZ``) and a
   phrase map (``PSSI``) of Intro / Up / Down / Chorus / Outro blocks.  This is
   the same data rekordbox draws its waveform above, so it is far more reliable
   than any generic beat detector -- and it costs one small file read rather
   than decoding the audio.
2. **A tempo/metre heuristic.**  For tracks with no analysis (an old XML
   import, a file that was never analysed) the structure is estimated from the
   duration and BPM on a 32-beat phrase grid.  Those cues are marked
   ``confidence: "low"`` so the UI can flag them.

Nothing in this module imports a heavy dependency at import time, so the
heuristic path always works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: rekordbox phrase kinds, as they read in the "high" mood -- the labels
#: :data:`PHRASE_KINDS` has always carried, and the ones a plain 4/4 club track
#: uses.  Other moods relabel the same numbers, so :data:`PHRASE_LABELS` is what
#: :func:`parse_anlz` actually reads from.
PHRASE_KINDS: dict[int, str] = {
    1: "Intro",
    2: "Up",
    3: "Down",
    4: "Bridge",
    5: "Chorus",
    6: "Outro",
}

#: ``PSSI`` header ``mood`` -> phrase kind -> label.  rekordbox picks one of
#: three vocabularies for a track, and the *same kind number means a different
#: thing in each*: in a mid-mood track kind 9 is a Chorus and kind 10 the Outro,
#: where the high-mood table stops at 6.  Reading a mid-mood track with the
#: high-mood table is why some tracks used to come back with five cues and no
#: drop.  Table from the DJ Link Ecosystem Analysis of the ANLZ format.
PHRASE_LABELS: dict[int, dict[int, str]] = {
    # 168 of the 186 analysed tracks in the reference library are "high".
    1: {1: "Intro", 2: "Up", 3: "Down", 4: "Chorus", 5: "Chorus", 6: "Outro"},
    2: {
        1: "Intro", 2: "Verse 1", 3: "Verse 2", 4: "Verse 3", 5: "Verse 4",
        6: "Verse 5", 7: "Verse 6", 8: "Bridge", 9: "Chorus", 10: "Outro",
    },
    3: {
        1: "Intro", 2: "Verse 1", 3: "Verse 1", 4: "Verse 1", 5: "Verse 2",
        6: "Verse 2", 7: "Verse 2", 8: "Bridge", 9: "Chorus", 10: "Outro",
    },
}

#: ``PSSI`` header ``mood`` -> phrase kind -> the part it plays in a set.  The
#: cue rules below match on the role, never on the raw number, so a mid-mood
#: track gets the same treatment as a high-mood one.
PHRASE_ROLES: dict[int, dict[int, str]] = {
    1: {1: "intro", 2: "build", 3: "breakdown", 4: "drop", 5: "drop", 6: "outro"},
    2: {
        1: "intro", 2: "verse", 3: "verse", 4: "verse", 5: "verse",
        6: "verse", 7: "verse", 8: "bridge", 9: "drop", 10: "outro",
    },
    3: {
        1: "intro", 2: "verse", 3: "verse", 4: "verse", 5: "verse",
        6: "verse", 7: "verse", 8: "bridge", 9: "drop", 10: "outro",
    },
}

#: The mood assumed when nothing says otherwise (hand-built phrases in tests, an
#: older ANLZ without a usable header).
DEFAULT_MOOD = 1

#: Phrase roles that behave like a build, a drop, a breakdown and an outro.
BUILD_ROLES = {"build"}
DROP_ROLES = {"drop"}
#: A mid-mood track has no "Down": the Bridge is where the energy drops out.
BREAKDOWN_ROLES = {"breakdown", "bridge"}
OUTRO_ROLES = {"outro"}
INTRO_ROLES = {"intro"}

#: Beats in the phrase grid.  rekordbox phrases are 32 beats (8 bars) at the
#: default 4-beat bar.
BEATS_PER_BAR = 4
PHRASE_BEATS = 32

#: The cue-plan layout version, stamped into every plan.
#:
#: 1 -- the four cues Parity shipped with (First Downbeat, Build Up, Main
#:      Drop, Mix-Out).
#: 2 -- eight cues, A-H, with a fallback behind every slot.
#:
#: A plan written by an older version is not one the current writers can
#: honour, so :class:`parity.store.Store` drops it on load and the app asks
#: for a fresh analysis instead of showing -- and writing -- a half-filled pad.
CUE_PLAN_VERSION = 2

#: The eight hot cues Parity injects, in the order the user asked for them.
#: ``rgb`` values are rekordbox hot-cue colours; the green is taken verbatim
#: from the user's own rekordbox XML export (``Red="48" Green="210" Blue="110"``)
#: so at least that one is the exact palette entry, and the rest stay in the
#: same family.  Change them here and every export follows.
#:
#: The four cues the tool shipped with (First Downbeat, Build Up, Main Drop and
#: Mix-Out) keep their colours and their meanings; Intro End, Breakdown and
#: Drop 2 fill the gaps between them.  Every slot A-H is Parity's -- a write
#: replaces any hot cue sitting in one of them, and a memory cue that lands on
#: top of one goes with it (see :data:`DUPLICATE_CUE_MS`).
CUE_ROLES: tuple[dict, ...] = (
    {
        "slot": "A",
        "num": 0,
        "role": "first_downbeat",
        "name": "First Downbeat",
        "colour": "red",
        "rgb": (230, 0, 0),
    },
    {
        "slot": "B",
        "num": 1,
        "role": "intro_end",
        "name": "Intro End",
        "colour": "orange",
        "rgb": (230, 130, 20),
    },
    {
        "slot": "C",
        "num": 2,
        "role": "build_up",
        "name": "Build Up",
        "colour": "yellow",
        "rgb": (230, 210, 0),
    },
    {
        "slot": "D",
        "num": 3,
        "role": "drop",
        "name": "Main Drop",
        "colour": "green",
        "rgb": (48, 210, 110),
    },
    {
        "slot": "E",
        "num": 4,
        "role": "breakdown",
        "name": "Breakdown",
        "colour": "aqua",
        "rgb": (0, 200, 200),
    },
    {
        "slot": "F",
        "num": 5,
        "role": "drop_two",
        "name": "Drop 2",
        "colour": "purple",
        "rgb": (150, 60, 230),
    },
    {
        "slot": "G",
        "num": 6,
        "role": "outro",
        "name": "Outro",
        "colour": "pink",
        "rgb": (230, 60, 150),
    },
    {
        "slot": "H",
        "num": 7,
        "role": "mix_out",
        "name": "Mix-Out",
        "colour": "blue",
        "rgb": (40, 120, 230),
    },
)

#: Every comment Parity has ever written onto a hot cue, current layout and
#: the four-cue one before it.  A write uses this to tell its own cues from a
#: cue the user placed by hand: the two look identical in ``DjmdCue`` otherwise,
#: and a policy of "keep what the user already cued" has to be able to tell them
#: apart or the second write would refuse to touch anything it wrote itself.
PARITY_CUE_NAMES: frozenset[str] = frozenset(
    {role["name"].strip().lower() for role in CUE_ROLES}
    | {
        # CUE_ROLES before the eight-cue layout: First Downbeat, Build Up, Main
        # Drop and Mix-Out.  The last one was written as "Mix-Out /
        # Breakdown" when a track had no breakdown phrase of its own.
        "mix-out",
        "mix-out / breakdown",
        "mix out",
    }
)


def is_parity_cue(name: str) -> bool:
    """Whether a ``DjmdCue.Comment`` is one of Parity's own cue names."""
    return (name or "").strip().lower() in PARITY_CUE_NAMES


#: Phrase kinds that open a track (used to find where the intro gives way).
INTRO_KINDS = {1}

#: How close two cue points may be before Parity moves one of them along.
MIN_CUE_GAP_MS = 400

#: How close a *rekordbox* memory cue has to sit to one of Parity's hot cues
#: before the write removes it as a duplicate.  rekordbox puts a memory cue
#: named ``1.1Bars`` on the first beat of most analysed tracks, which is exactly
#: where hot cue A goes, so the two markers used to sit on top of each other.
#: Wide enough to absorb the rounding between the ANLZ beat grid and
#: ``DjmdCue.InMsec``, narrow enough that a memory cue the user placed on a
#: different beat is left alone.
DUPLICATE_CUE_MS = 100

#: The tail lengths the mix-out cue walks through when the outro is too short to
#: leave room for a separate point after it, longest first.
TAIL_BEATS = (PHRASE_BEATS, PHRASE_BEATS // 2, PHRASE_BEATS // 4)

#: The eight-section shape a club edit usually follows, as fractions of the
#: track: quiet intro, build, drop, a second run, a breakdown, the second drop,
#: then the outro.  Used when there is no rekordbox phrase map to read.
SHAPE: tuple[tuple[int, str, float], ...] = (
    (1, "Intro", 0.0),
    (2, "Up", 0.08),
    (5, "Chorus", 0.20),
    (5, "Chorus", 0.38),
    (3, "Down", 0.52),
    (2, "Up", 0.62),
    (5, "Chorus", 0.74),
    (6, "Outro", 0.90),
)


class AnalysisUnavailable(RuntimeError):
    """The ANLZ file or pyrekordbox is missing; fall back to the heuristic."""


@dataclass
class Phrase:
    """One rekordbox phrase block."""

    index: int
    kind: int
    name: str
    start_beat: int
    end_beat: int
    start_ms: int
    end_ms: int
    #: The part this phrase plays (``intro``, ``build``, ``drop``, ...).  Filled
    #: in by :func:`parse_anlz` from the mood, and defaulted here so a hand-built
    #: Phrase still behaves like a high-mood one.
    role: str = ""

    def __post_init__(self) -> None:
        if not self.role:
            self.role = PHRASE_ROLES[DEFAULT_MOOD].get(self.kind, "")

    @property
    def bars(self) -> float:
        return round((self.end_beat - self.start_beat) / BEATS_PER_BAR, 1)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "role": self.role,
            "start_beat": self.start_beat,
            "end_beat": self.end_beat,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "bars": self.bars,
        }


@dataclass
class Structure:
    """Beat grid plus phrase map for one track."""

    bpm: float = 0.0
    duration_ms: int = 0
    beat_ms: list[int] = field(default_factory=list)  # absolute beat -> ms
    phrases: list[Phrase] = field(default_factory=list)
    source: str = "heuristic"  # "rekordbox-anlz" | "heuristic"
    confidence: str = "low"
    #: The ``PSSI`` mood the phrase labels came from (0 when there is no phrase
    #: map).  It decides which vocabulary kind numbers are read against.
    mood: int = 0

    @property
    def analysed(self) -> bool:
        return self.source == "rekordbox-anlz"

    @property
    def last_beat(self) -> int:
        return len(self.beat_ms)

    def beat_time(self, beat: int) -> int:
        """Milliseconds of a 1-based absolute beat, extrapolating past the grid."""
        if not self.beat_ms:
            if not self.bpm:
                return 0
            return int(round((beat - 1) * 60000.0 / self.bpm))
        if beat <= 1:
            return self.beat_ms[0]
        if beat <= len(self.beat_ms):
            return self.beat_ms[beat - 1]
        step = 60000.0 / self.bpm if self.bpm else (
            (self.beat_ms[-1] - self.beat_ms[-2]) if len(self.beat_ms) > 1 else 500.0
        )
        return int(round(self.beat_ms[-1] + (beat - len(self.beat_ms)) * step))

    def to_dict(self) -> dict:
        return {
            "bpm": self.bpm,
            "duration_ms": self.duration_ms,
            "source": self.source,
            "confidence": self.confidence,
            "mood": self.mood,
            "phrase_count": len(self.phrases),
            "beats": len(self.beat_ms),
            "first_beat_ms": self.beat_ms[0] if self.beat_ms else 0,
            "phrases": [phrase.to_dict() for phrase in self.phrases],
        }


# --------------------------------------------------------------- ANLZ reader


def _import_anlz():
    try:
        from pyrekordbox.anlz import AnlzFile  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    return AnlzFile, None


def _valor(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _entries(struct: dict, container: str = "content"):
    """Pull a list of entries out of a parsed ANLZ tag."""
    try:
        return list(struct["content"][container])  # type: ignore[index]
    except Exception:  # noqa: BLE001
        return []


def parse_anlz(dat_path: str | Path, bpm: float = 0.0) -> Structure:
    """Read a rekordbox ANLZ ``.DAT`` (plus its ``.EXT``) into a Structure."""
    anlz_file, error = _import_anlz()
    if anlz_file is None:
        raise AnalysisUnavailable(f"pyrekordbox is not available ({error})")

    dat_path = Path(dat_path)
    if not dat_path.is_file():
        raise AnalysisUnavailable(f"No ANLZ file at {dat_path}")

    root = anlz_file.parse_file(str(dat_path))
    structure = Structure(source="rekordbox-anlz", confidence="high", bpm=bpm)

    beats = _entries(root.get_tag("PQTZ").struct, "entries")
    structure.beat_ms = [_valor(entry["time"]) for entry in beats]
    if not structure.bpm and beats:
        # PQTZ stores tempo as BPM * 100.
        structure.bpm = round(_valor(beats[0]["tempo"]) / 100.0, 2)

    ext_path = dat_path.with_suffix(".EXT")
    if ext_path.is_file():
        try:
            ext = anlz_file.parse_file(str(ext_path))
            tag = ext.get_tag("PSSI")
        except Exception:  # noqa: BLE001 - older ANLZ has no phrase map
            tag = None
        if tag is not None:
            header = tag.struct["content"]
            mood = _valor(header.get("mood")) if hasattr(header, "get") else 0
            # The kind numbers are only meaningful against the mood's table: in a
            # mid-mood track 9 is a Chorus and 10 the Outro, which the high-mood
            # table renders as "Phrase 9" and "Phrase 10".
            labels = PHRASE_LABELS.get(mood) or PHRASE_KINDS
            roles = PHRASE_ROLES.get(mood) or PHRASE_ROLES[DEFAULT_MOOD]
            structure.mood = mood if mood in PHRASE_LABELS else 0
            raw = _entries(tag.struct, "entries")
            end_beat = _valor(header.get("end_beat")) if hasattr(header, "get") else 0
            for position, entry in enumerate(raw):
                start_beat = _valor(entry["beat"], position * PHRASE_BEATS + 1)
                if position + 1 < len(raw):
                    next_beat = _valor(raw[position + 1]["beat"], start_beat + PHRASE_BEATS)
                else:
                    next_beat = end_beat or start_beat + PHRASE_BEATS
                kind = _valor(entry["kind"])
                structure.phrases.append(
                    Phrase(
                        index=position + 1,
                        kind=kind,
                        name=labels.get(kind, f"Phrase {kind}"),
                        role=roles.get(kind, ""),
                        start_beat=start_beat,
                        end_beat=max(next_beat, start_beat + 1),
                        start_ms=structure.beat_time(start_beat),
                        end_ms=structure.beat_time(max(next_beat, start_beat + 1)),
                    )
                )

    if structure.beat_ms:
        structure.duration_ms = structure.beat_ms[-1]
    if not structure.phrases and not structure.beat_ms:
        raise AnalysisUnavailable(f"{dat_path} has no usable beat grid")
    return structure


# --------------------------------------------------------------- heuristic


def heuristic_structure(duration_ms: int, bpm: float = 0.0) -> Structure:
    """Estimate structure from duration and tempo alone.

    Lays :data:`SHAPE` across the track, snapped to the bar when the tempo is
    known, so a track with no rekordbox phrase map still gets the full set of
    cues.  Marked ``confidence: "low"`` wherever it is used.
    """
    bpm = bpm or 0.0
    duration_ms = max(0, int(duration_ms))
    structure = Structure(
        bpm=round(bpm, 2),
        duration_ms=duration_ms,
        source="heuristic",
        confidence="low",
    )
    if duration_ms <= 0:
        return structure

    beat_ms = 60000.0 / bpm if bpm > 0 else 0.0
    step = BEATS_PER_BAR * beat_ms if beat_ms > 0 else 4000.0
    # A track too short to hold eight bars per section gets a smaller step, so
    # the shape still spreads across it instead of piling up on the last
    # millisecond.
    if step * len(SHAPE) > duration_ms:
        step = max(1.0, duration_ms / len(SHAPE))

    starts: list[int] = []
    for _kind, _name, fraction in SHAPE:
        start_ms = int(round(fraction * duration_ms / step)) * step
        if starts:
            start_ms = max(start_ms, starts[-1] + step)
        starts.append(min(duration_ms, int(round(start_ms))))

    for position, (kind, name, _fraction) in enumerate(SHAPE):
        start_ms = starts[position]
        end_ms = starts[position + 1] if position + 1 < len(starts) else duration_ms
        end_ms = max(end_ms, start_ms)
        beat = int(round(start_ms / beat_ms)) + 1 if beat_ms > 0 else 0
        structure.phrases.append(
            Phrase(
                index=position + 1,
                kind=kind,
                name=name,
                start_beat=beat,
                end_beat=int(round(end_ms / beat_ms)) + 1 if beat_ms > 0 else beat + PHRASE_BEATS,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    return structure


# ------------------------------------------------------------- cue placement


def _first_start(phrases: list[Phrase], roles: set[str], after: int):
    """The earliest phrase playing one of ``roles`` at or after ``after``."""
    for phrase in phrases:
        if phrase.role in roles and phrase.start_ms >= after:
            return phrase
    return None


def _last_start_between(phrases: list[Phrase], roles: set[str], low: int, high: int):
    """The latest phrase playing one of ``roles`` starting inside ``[low, high)``."""
    chosen = None
    for phrase in phrases:
        if phrase.role in roles and low <= phrase.start_ms < high:
            chosen = phrase
    return chosen


def _first_start_between(phrases: list[Phrase], roles: set[str], low: int, high: int):
    """The earliest phrase playing one of ``roles`` starting inside ``[low, high)``."""
    for phrase in phrases:
        if phrase.role in roles and low <= phrase.start_ms < high:
            return phrase
    return None


def _boundaries(phrases: list[Phrase]) -> list[int]:
    """Every phrase start and end in order -- the lines a DJ counts from."""
    marks: set[int] = set()
    for phrase in phrases:
        marks.add(phrase.start_ms)
        marks.add(phrase.end_ms)
    return sorted(marks)


def _snap(marks: list[int], target: int, low: int, high: int) -> int:
    """The phrase line inside ``[low, high]`` closest to ``target``.

    Fallback cues land on a line rather than on an arbitrary millisecond, so a
    guessed cue still sits where rekordbox draws a phrase.  ``target`` is
    returned untouched when there is no line to snap to.
    """
    best = None
    for mark in marks:
        if low <= mark <= high and (best is None or abs(mark - target) < abs(best - target)):
            best = mark
    return target if best is None else best


def place_cues(structure: Structure) -> list[dict]:
    """Turn a structure into the eight hot cues, in A/B/C/D/E/F/G/H order.

    Parity owns all eight hot-cue slots, so this fills every one of them.  A
    role with no phrase to land on -- a track with no second drop, an edit whose
    outro runs straight off the end -- falls back to the nearest phrase line
    that is still in order, and ``reason`` says which rule produced it.

    Rules, in the order a DJ thinks about a track:

    * **A (red) First Downbeat** -- the first beat of the first phrase, the top
      of the track.  rekordbox's own ``1.1Bars`` memory cue sits on that same
      beat on most analysed tracks; the write removes it as a duplicate, since
      a pad you can hit beats a memory cue you can only look at.
    * **B (orange) Intro End** -- where the intro gives way to the body, which
      is the mix-in point.
    * **C (yellow) Build Up** -- the last ``Up`` phrase before the drop, i.e. the
      run-up you actually count down.
    * **D (green) Main Drop** -- the first ``Chorus`` after the intro.
    * **E (aqua) Breakdown** -- the first ``Down``/``Bridge`` after the drop.
    * **F (purple) Drop 2** -- the next ``Chorus`` after the breakdown.
    * **G (pink) Outro** -- where the outro starts.
    * **H (blue) Mix-Out** -- the closing 32, 16 or 8 bars, so it never lands on
      the same millisecond as the outro.
    """
    phrases = structure.phrases
    if not phrases and structure.duration_ms:
        # A beat grid with no phrase map -- a track rekordbox never ran phrase
        # analysis on, or an XML import.  Use the estimated shape for placement
        # only: the stored structure stays honest about having no phrases.
        phrases = heuristic_structure(structure.duration_ms, structure.bpm).phrases
    marks = _boundaries(phrases)
    limit = structure.duration_ms or None
    gap = MIN_CUE_GAP_MS
    # Without a beat grid and without a tempo there is nothing to tell one
    # millisecond from another, so the rules below all land on 0 and the gap
    # rules are meaningless.  Emit the cue set anyway rather than nothing.
    timed = bool(structure.beat_ms or structure.bpm)

    def clamp(ms: int) -> int:
        ms = max(0, int(ms))
        return min(ms, limit) if limit else ms

    def snap(target: int, low: int, high: int) -> int:
        """The phrase line inside ``[low, high]`` closest to ``target``."""
        return _snap(marks, target, low, high)

    def beat_length() -> float:
        if structure.bpm:
            return 60000.0 / structure.bpm
        if len(structure.beat_ms) > 1:
            return float(structure.beat_ms[-1] - structure.beat_ms[-2])
        return 500.0

    # -- A: first downbeat -------------------------------------------------
    if phrases:
        a_ms, a_reason = phrases[0].start_ms, f"first phrase ({phrases[0].name}) starts here"
    else:
        a_ms, a_reason = structure.beat_time(1), "first beat of the grid"
    a_ms = clamp(a_ms)

    # -- B: where the intro ends ------------------------------------------
    leading = []
    for phrase in phrases:
        if phrase.role in INTRO_ROLES:
            leading.append(phrase)
        else:
            break
    if leading:
        b_ms = leading[-1].end_ms
        b_reason = f"intro phrase {leading[-1].index} ends here"
        if len(leading) > 1:
            b_reason = f"intro ends after phrase {leading[-1].index}"
    elif len(phrases) > 1:
        b_ms = phrases[0].end_ms
        b_reason = f"phrase {phrases[0].index} ({phrases[0].name}) ends here"
    else:
        b_ms = structure.beat_time(PHRASE_BEATS)
        b_reason = "no phrases; 32 beats in"
    b_ms = clamp(b_ms)

    # -- D: the drop -------------------------------------------------------
    # Anchored to the intro end, so a body that opens straight onto a Chorus
    # (rekordbox marks plenty of edits that way) is skipped and the next real
    # drop is used instead of stacking B and D on one millisecond.
    drop = _first_start(phrases, DROP_ROLES, b_ms + gap)
    if drop is not None:
        d_ms, d_reason = drop.start_ms, f"{drop.name} phrase {drop.index} starts here"
    else:
        builds = [p for p in phrases if p.role in BUILD_ROLES and p.start_ms >= b_ms]
        if builds:
            longest = max(builds, key=lambda p: p.end_ms - p.start_ms)
            d_ms, d_reason = longest.end_ms, f"end of the longest build (phrase {longest.index})"
        else:
            # A low- or mid-mood track can have no Chorus at all -- just verses,
            # bridges and an outro.  Take the middle of the body rather than a
            # fixed "64 beats in", which lands *before* the intro has even ended
            # on a slow track and would drop the cue as out of order.
            body = [p for p in phrases if p.role in {"verse", "bridge"} and p.start_ms > b_ms]
            if body:
                middle = body[len(body) // 2]
                d_ms, d_reason = middle.start_ms, f"no Chorus; middle of the body ({middle.name})"
            else:
                d_ms, d_reason = structure.beat_time(2 * PHRASE_BEATS), "no phrases; 64 beats in"
    d_ms = clamp(d_ms)

    # -- C: the build into that drop ---------------------------------------
    build = _last_start_between(phrases, BUILD_ROLES, b_ms + gap, max(b_ms + gap, d_ms - gap))
    if build is not None:
        c_ms = build.start_ms
        c_reason = f"{build.name} phrase {build.index} starts here (run-up to the drop)"
    else:
        # The intro usually ends exactly where the build begins, and rekordbox
        # marks plenty of edits with no Up phrase at all.  Put the build cue
        # halfway through the run-up instead of on top of the intro end.
        c_ms = b_ms + (d_ms - b_ms) // 2
        c_reason = "no Up phrase before the drop; halfway through the run-up"
    c_ms = clamp(c_ms)

    # -- G: the outro ------------------------------------------------------
    # Placed before the breakdown and the second drop so both have a wall to
    # land against: on a short edit the outro sits before the second drop
    # would, and a cue put past it would come out of order.
    outro = _first_start(phrases, OUTRO_ROLES, d_ms + gap)
    if outro is not None:
        g_ms, g_reason = outro.start_ms, f"outro phrase {outro.index} starts here"
    elif phrases and phrases[-1].start_ms > d_ms + gap:
        last = phrases[-1]
        g_ms, g_reason = last.start_ms, f"no Outro phrase; the final phrase ({last.name}) starts here"
    else:
        g_ms = structure.beat_time(max(1, structure.last_beat - PHRASE_BEATS))
        g_reason = "no Outro phrase; the final 32 beats of the grid"
    g_ms = clamp(g_ms)
    # The open run E and F share: after the drop, before the outro.
    ceiling = g_ms - gap if g_ms > d_ms + gap else d_ms

    # -- E: the breakdown --------------------------------------------------
    breakdown = _first_start_between(phrases, BREAKDOWN_ROLES, d_ms + gap, ceiling)
    if breakdown is not None:
        e_ms, e_reason = breakdown.start_ms, f"{breakdown.name} phrase {breakdown.index} starts here"
    else:
        next_build = _first_start_between(phrases, BUILD_ROLES, d_ms + gap, ceiling)
        if next_build is not None:
            e_ms, e_reason = next_build.start_ms, f"no breakdown; the next {next_build.name} phrase starts here"
        else:
            # No Down, no Bridge and no Up: a straight two-chorus edit.  Take
            # the middle of the run to the outro rather than leaving the pad
            # dark.
            middle = d_ms + (ceiling - d_ms) // 2
            e_ms = snap(middle, d_ms + gap, ceiling)
            e_reason = "no breakdown after the drop; the middle of the run to the outro"

    # -- F: the second drop ------------------------------------------------
    # Anchored to whatever E landed on, so a track that never breaks down
    # still gets its second drop instead of losing two cues at once.
    second = _first_start_between(phrases, DROP_ROLES, e_ms + gap, ceiling)
    if second is not None:
        f_ms, f_reason = second.start_ms, f"{second.name} phrase {second.index} starts here"
    else:
        middle = e_ms + (ceiling - e_ms) // 2
        f_ms = snap(middle, e_ms + gap, ceiling)
        f_reason = "no second drop; the middle of the run to the outro"

    # -- H: the mix-out tail -----------------------------------------------
    # A duration is all this needs: the tail is measured back from the end of
    # the file, longest first, and a short outro gets the middle of what is
    # left instead of no cue at all.
    tail = beat_length()
    h_ms, h_reason = None, ""
    if limit:
        for beats in TAIL_BEATS:
            candidate = int(round(limit - beats * tail))
            if candidate > g_ms + gap:
                h_ms = candidate
                h_reason = f"final {beats} beats of the track"
                break
        if h_ms is None and limit - g_ms > gap:
            h_ms = (g_ms + limit) // 2
            h_reason = "the outro is shorter than 8 beats; the middle of it"
    if h_ms is None:
        h_ms = g_ms
        h_reason = "no duration to measure a tail against"

    planned = [
        ("A", a_ms, a_reason),
        ("B", b_ms, b_reason),
        ("C", c_ms, c_reason),
        ("D", d_ms, d_reason),
        ("E", e_ms, e_reason),
        ("F", f_ms, f_reason),
        ("G", g_ms, g_reason),
        ("H", h_ms, h_reason),
    ]
    roles = {role["slot"]: role for role in CUE_ROLES}

    cues: list[dict] = []
    previous: int | None = None
    for slot, ms, reason in planned:
        role = roles[slot]
        ms = clamp(ms)
        if previous is not None and timed and ms < previous + gap:
            # Every slot belongs to Parity, so a cue with nowhere left to go
            # moves up to the smallest position that keeps the gap rather than
            # being dropped and leaving a pad dark.
            ms = previous + gap if limit is None else min(previous + gap, limit)
        cues.append(
            {
                "slot": role["slot"],
                "num": role["num"],
                "role": role["role"],
                "name": role["name"],
                "colour": role["colour"],
                "rgb": list(role["rgb"]),
                "ms": ms,
                "seconds": round(ms / 1000.0, 3),
                "reason": reason,
                "confidence": structure.confidence,
                "source": structure.source,
            }
        )
        previous = ms
    return cues


def memory_cues(structure: Structure, generated: list[dict] | None = None) -> list[dict]:
    """A memory cue at every phrase boundary, except where a hot cue sits."""
    taken = {cue["ms"] for cue in (generated or [])}
    cues: list[dict] = []
    for phrase in structure.phrases[1:]:
        if any(abs(phrase.start_ms - ms) < MIN_CUE_GAP_MS for ms in taken):
            continue
        if any(abs(phrase.start_ms - cue["ms"]) < MIN_CUE_GAP_MS for cue in cues):
            continue
        cues.append(
            {
                "num": -1,
                "role": "phrase",
                "name": f"{phrase.name} {phrase.index}",
                "ms": phrase.start_ms,
                "seconds": round(phrase.start_ms / 1000.0, 3),
                "confidence": structure.confidence,
                "source": structure.source,
            }
        )
    return cues


def analyse_track(track, anlz_reader=parse_anlz) -> dict:
    """Structure + cues for one :class:`~parity.rekordbox.Track`.

    ``track`` only needs ``duration``, ``bpm`` and ``analysis_path``, so this
    works for XML imports too (which fall back to the heuristic).  The cues the
    track already carries do not change the plan: Parity owns all eight hot
    cue slots and replaces them on every write, and :func:`place_cues` always
    fills all eight, so a track never comes back with a pad missing.
    """
    duration_ms = int(round(float(getattr(track, "duration", 0) or 0) * 1000))
    bpm = float(getattr(track, "bpm", 0) or 0)
    analysis_path = str(getattr(track, "analysis_path", "") or "")

    structure: Structure
    note = ""
    if analysis_path:
        try:
            structure = anlz_reader(analysis_path, bpm)
        except AnalysisUnavailable as exc:
            structure = heuristic_structure(duration_ms, bpm)
            note = f"rekordbox analysis unreadable, used the heuristic ({exc})"
        except Exception as exc:  # noqa: BLE001 - never let one track break a run
            structure = heuristic_structure(duration_ms, bpm)
            note = f"rekordbox analysis failed, used the heuristic ({type(exc).__name__}: {exc})"
    else:
        structure = heuristic_structure(duration_ms, bpm)
        note = "track has no rekordbox analysis yet"

    if not structure.duration_ms and duration_ms:
        structure.duration_ms = duration_ms
    hot = place_cues(structure)
    return {
        "planner": CUE_PLAN_VERSION,
        "structure": structure.to_dict(),
        "hot_cues": hot,
        "memory_cues": memory_cues(structure, hot),
        "note": note,
    }
