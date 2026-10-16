"""Set building.

Two jobs live here:

* :func:`build_set` -- choose tracks from a pool and order them so they follow a
  target energy curve while staying harmonically and tempo compatible.
* :func:`optimize_set` -- re-order tracks the user already arranged (honouring
  locked positions) to reduce the same cost function.

Choosing is curve-first (:func:`fit_curve`): the tracks that fit the target
curve best are picked as a *set*, by rank, so the quietest target gets the
quietest track of the chosen group.  Filling slot by slot instead spends a
track an early slot can afford on one a later slot needed -- and it cannot see
that one track in the pool is a whole energy point closer to two different
targets.  The chosen order is then reworked by simulated annealing, which
weighs key and tempo, all inside :func:`total_cost`.

With playlists in the low hundreds of tracks this stays in the tens of
milliseconds, which is what makes the UI feel instant.
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, field

from . import camelot

# Target energy at fractional positions through the set (0.0 -> 1.0).
TEMPLATES: dict[str, list[tuple[float, float]]] = {
    "warm_up": [(0.0, 3.0), (0.45, 5.0), (1.0, 7.0)],
    "journey": [(0.0, 3.0), (0.25, 5.0), (0.5, 6.5), (0.75, 8.0), (1.0, 6.0)],
    "peak_time": [(0.0, 6.0), (0.3, 8.0), (0.55, 9.5), (0.8, 9.0), (1.0, 7.5)],
    "festival": [(0.0, 5.0), (0.2, 7.5), (0.5, 9.0), (0.7, 10.0), (0.9, 8.5), (1.0, 6.0)],
    "cool_down": [(0.0, 8.0), (0.35, 7.0), (0.7, 5.0), (1.0, 3.0)],
    "chill": [(0.0, 2.5), (0.5, 4.0), (1.0, 3.0)],
    "flat": [(0.0, 6.0), (1.0, 6.0)],
    "build_and_hold": [(0.0, 4.0), (0.3, 7.0), (0.5, 8.5), (1.0, 8.5)],
    "opener_to_peak": [(0.0, 4.0), (0.5, 7.0), (1.0, 10.0)],
}

STRICTNESS_KEY_WEIGHT = {"strict": 8.0, "normal": 3.0, "free": 0.8}

#: How much one energy point of deviation costs, squared, before the user's own
#: ``energy_weight``.  At 1.0 the curve was worth less than the mixing
#: penalties, so a build would rather live with a key clash than leave the curve,
#: and the set came out flat at the bottom of the pool.
#:
#: Three, with the outlier charge below, is what measuring the alternatives said.
#: On a 194-track library over 60 builds of three curve shapes each:
#:
#:   scale 2.0, no charge    correlation  0.83 (worst build 0.63)   fit 0.38   harmonic 88%
#:   scale 3.0 + charge      correlation  0.95 (worst build 0.93)   fit 0.31   harmonic 82%
#:   scale 4.0 + charge      correlation  0.94 (worst build 0.91)   fit 0.31   harmonic 84%
#:
#: The shape of the set is what this tool is for, so the six points of harmonic
#: mixing it costs are the trade to make -- ``key_weight`` and ``bpm_weight`` are
#: there for anyone who would rather have them back.
ENERGY_SCALE = 3.0

#: Where a deviation stops being ordinary and starts visibly bending the curve
#: (energy points), and what a point of that excess costs on top of the squared
#: term.  Squared error alone tolerates a single visible outlier -- the opening
#: track traded for a slightly better mix, leaving the first slot a whole energy
#: point off -- and that is the one thing the chart makes obvious.  A 1.0 miss is
#: normal in a library that has nothing closer; a 1.5 miss then costs roughly
#: another energy point, and a 2.0 miss three.
ENERGY_OUTLIER_AT = 1.0
ENERGY_OUTLIER_SCALE = 12.0

#: What a key clash costs in Strict mode, in units of the curve's own weight, so
#: that "strict" keeps meaning "no clashes" however the energy term is tuned.
STRICT_CLASH_BONUS = 20.0

#: Largest dynamic-programming table the curve fit will build, in cells.  Past
#: this (a very long set drawn from a very large pool) :func:`_fit_walk` takes
#: over: linear, monotone, and close enough on a pool where the difference
#: between the best and second-best fit is noise.
MAX_FIT_CELLS = 1_000_000


@dataclass
class Candidate:
    """The subset of track data the builder needs."""

    key: str
    title: str = ""
    artist: str = ""
    genre: str = ""
    camelot: str | None = None
    bpm: float = 0.0
    energy: float = 5.0
    duration: float = 0.0
    confidence: str = ""  # energy confidence: "high" | "medium" | "low"

    def label(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title or self.key


@dataclass
class BuildConfig:
    """Everything the user can tune in the UI."""

    template: str = "journey"
    curve_points: list[tuple[float, float]] | None = None  # overrides template
    length_tracks: int = 20
    energy_weight: float = 1.0
    key_weight: float = 1.0
    bpm_weight: float = 1.0
    bpm_tolerance: float = 6.0  # BPM; larger jumps are penalised
    bpm_hard_limit: float = 18.0  # 0 disables the hard limit
    strictness: str = "normal"
    artist_repeat_window: int = 3
    min_energy: float = 1.0
    max_energy: float = 10.0
    min_bpm: float = 0.0
    max_bpm: float = 0.0
    locks: dict[int, str] = field(default_factory=dict)  # position -> track key
    excluded: set[str] = field(default_factory=set)
    start_key: str | None = None
    end_key: str | None = None
    seed: int | None = None
    iterations: int = 4000

    def key_weight_effective(self) -> float:
        weight = STRICTNESS_KEY_WEIGHT.get(self.strictness, 3.0) * self.key_weight
        if self.strictness == "strict":
            # Strict means harmony first, so its weight rides the curve's own
            # scale: turning the energy term up must not quietly turn Strict
            # into Normal, and the two are only comparable in the same units.
            weight *= ENERGY_SCALE
        return weight

    def to_dict(self) -> dict:
        return {
            "template": self.template,
            "curve_points": self.curve_points,
            "length_tracks": self.length_tracks,
            "energy_weight": self.energy_weight,
            "key_weight": self.key_weight,
            "bpm_weight": self.bpm_weight,
            "bpm_tolerance": self.bpm_tolerance,
            "bpm_hard_limit": self.bpm_hard_limit,
            "strictness": self.strictness,
            "artist_repeat_window": self.artist_repeat_window,
            "min_energy": self.min_energy,
            "max_energy": self.max_energy,
            "min_bpm": self.min_bpm,
            "max_bpm": self.max_bpm,
            "locks": {str(k): v for k, v in self.locks.items()},
            "excluded": sorted(self.excluded),
            "start_key": self.start_key,
            "end_key": self.end_key,
            "seed": self.seed,
            "iterations": self.iterations,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BuildConfig":
        known = {f for f in cls().to_dict()}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        if clean.get("locks"):
            clean["locks"] = {int(k): v for k, v in clean["locks"].items()}
        if clean.get("excluded"):
            clean["excluded"] = set(clean["excluded"])
        if clean.get("curve_points"):
            clean["curve_points"] = [(float(p[0]), float(p[1])) for p in clean["curve_points"]]
        config = cls(**clean)
        config.strictness = config.strictness if config.strictness in STRICTNESS_KEY_WEIGHT else "normal"
        return config


@dataclass
class Transition:
    index: int  # index of the *following* track
    key_score: float
    label: str
    bpm_delta: float


@dataclass
class BuildResult:
    order: list[str]
    transitions: list[Transition]
    cost: float
    energy_curve: list[float]
    targets: list[float]
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------- curves


def curve_from_points(points: list[tuple[float, float]], length: int) -> list[float]:
    """Piecewise-linear interpolation of a target curve for ``length`` slots."""
    if length <= 0:
        return []
    ordered = sorted((max(0.0, min(1.0, float(p))), float(e)) for p, e in points)
    if not ordered:
        return [5.0] * length
    if len(ordered) == 1:
        return [ordered[0][1]] * length

    values: list[float] = []
    for index in range(length):
        position = index / (length - 1) if length > 1 else 0.0
        values.append(_interpolate(ordered, position))
    return values


def _interpolate(points: list[tuple[float, float]], position: float) -> float:
    if position <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x1 <= position:
            continue
        if x1 == x0:
            return y1
        ratio = (position - x0) / (x1 - x0)
        return y0 + (y1 - y0) * ratio
    return points[-1][1]


def template_points(name: str) -> list[tuple[float, float]]:
    return list(TEMPLATES.get(name) or TEMPLATES["journey"])


# ---------------------------------------------------------------- costs


def bpm_delta(a: float, b: float) -> float:
    """Tempo change between two tracks, accepting half/double-time pairs."""
    if a <= 0 or b <= 0:
        return 0.0
    candidates = [abs(a - b)]
    if b > 0:
        candidates.append(abs(a - b * 2))
        candidates.append(abs(a - b / 2))
    return min(candidates)


def _bpm_penalty(a: Candidate, b: Candidate, config: BuildConfig) -> float:
    if not a.bpm or not b.bpm:
        return 0.0
    delta = bpm_delta(a.bpm, b.bpm)
    tolerance = max(0.5, config.bpm_tolerance)
    if delta <= tolerance:
        return 0.0
    excess = (delta - tolerance) / tolerance
    penalty = min(4.0, excess ** 1.5)
    if config.bpm_hard_limit and delta > config.bpm_hard_limit:
        penalty += 6.0
    return penalty


def _transition_penalty(a: Candidate, b: Candidate, config: BuildConfig) -> float:
    score = camelot.compat_score(a.camelot, b.camelot)
    key_penalty = (1.0 - score) * config.key_weight_effective()
    if config.strictness == "strict" and score == 0.0 and a.camelot and b.camelot:
        # Strict means strict: a clash costs more than any curve deviation, and
        # the constant follows the curve's own weighting so that stays true
        # whatever the energy term is set to.
        key_penalty += STRICT_CLASH_BONUS * ENERGY_SCALE
    return key_penalty + config.bpm_weight * _bpm_penalty(a, b, config)


def energy_cost(track: Candidate, target: float, config: BuildConfig) -> float:
    """What one track's distance from its target energy costs.

    Squared error on its own tolerates a single visible outlier: with the rest
    of the set sitting on the curve, trading the opening track for a better mix
    is cheap, and the first slot ends up a whole energy point out -- which is
    the one thing the chart makes obvious.  Deviations past a point are charged
    again, so the shape survives while ordinary misses are still traded freely.
    """
    deviation = abs(track.energy - target)
    outlier = max(0.0, deviation - ENERGY_OUTLIER_AT) ** 2 * ENERGY_OUTLIER_SCALE
    return config.energy_weight * (ENERGY_SCALE * deviation**2 + outlier)


def total_cost(order: list[Candidate], targets: list[float], config: BuildConfig) -> float:
    cost = 0.0
    for index, track in enumerate(order):
        target = targets[index] if index < len(targets) else 5.0
        cost += energy_cost(track, target, config)
        if index:
            cost += _transition_penalty(order[index - 1], track, config)

    window = max(0, config.artist_repeat_window)
    if window:
        for i in range(len(order)):
            for j in range(i + 1, min(len(order), i + window + 1)):
                if order[i].artist and order[i].artist == order[j].artist:
                    cost += 1.6 * (window + 1 - (j - i)) / (window + 1)
    return cost


# ---------------------------------------------------------------- building


# ------------------------------------------------------------ curve fitting


def fit_curve(
    pool: list[Candidate],
    targets: list[float],
    positions: list[int],
    rng: random.Random | None = None,
) -> dict[int, Candidate]:
    """The tracks that follow ``targets`` best, one per position, by rank.

    Matching the k-th quietest track of a group to the k-th quietest target is
    the pairing that minimises the total squared deviation -- rearranging equal
    numbers never beats sorting them -- so the only real question is *which*
    tracks to take.  That is what the dynamic program answers: it can see that a
    track is a better match for a target two slots along, and it will not spend
    the pool's only quiet track on a slot that could have used anything.

    ``positions`` are the slots still to fill (locks are already placed), and
    the return value maps position to track.
    """
    if not positions:
        return {}
    slots = sorted(positions, key=lambda position: (targets[position], position))
    wanted = [targets[position] for position in slots]
    tracks = sorted(pool, key=lambda track: (track.energy, track.key))

    if len(wanted) >= len(tracks):
        chosen = list(tracks)
    elif len(wanted) * len(tracks) <= MAX_FIT_CELLS:
        chosen = _fit_dp(tracks, wanted)
    else:
        chosen = _fit_walk(tracks, wanted)

    if rng is not None:
        chosen = _vary_equal_energy(chosen, tracks, rng)
    # Intentionally not zip(..., strict=True): a pool smaller than the set has
    # fewer tracks than slots, so the extra slots stay empty and the caller
    # builds the short set it asked for.
    return {position: track for position, track in zip(slots, chosen)}


def _fit_dp(tracks: list[Candidate], wanted: list[float]) -> list[Candidate]:
    """Fewest total squared deviation over every monotone choice of tracks.

    ``row[i]`` is the best cost of filling the targets seen so far with the
    first ``i + 1`` tracks *taking track i for the last of them*; the walk back
    through ``back`` rebuilds the picks.
    """
    count, available = len(wanted), len(tracks)
    energies = [track.energy for track in tracks]
    infinity = float("inf")
    row = [(energy - wanted[0]) ** 2 for energy in energies]
    back: list[list[int]] = []

    for index in range(1, count):
        current = [infinity] * available
        step = [-1] * available
        best, best_index = infinity, -1
        for i in range(available):
            if i and row[i - 1] < best:
                best, best_index = row[i - 1], i - 1
            if best_index >= 0:
                current[i] = best + (energies[i] - wanted[index]) ** 2
                step[i] = best_index
        back.append(step)
        row = current

    end = min(range(available), key=lambda i: (row[i], i))
    picks = [end]
    for step in reversed(back):
        end = step[end]
        if end < 0:
            break
        picks.append(end)
    picks.reverse()
    return [tracks[i] for i in picks]


def _fit_walk(tracks: list[Candidate], wanted: list[float]) -> list[Candidate]:
    """Monotone two-pointer pass, for pools too big to tabulate."""
    count, available = len(wanted), len(tracks)
    picks: list[int] = []
    start = 0
    for index, target in enumerate(wanted):
        last = available - (count - index)
        if last < start:
            last = start
        best_index, best_deviation = start, abs(tracks[start].energy - target)
        for i in range(start, last + 1):
            deviation = abs(tracks[i].energy - target)
            if deviation < best_deviation or (
                deviation == best_deviation and tracks[i].energy >= target
            ):
                best_index, best_deviation = i, deviation
        picks.append(best_index)
        start = best_index + 1
    return [tracks[i] for i in picks]


#: How close two energies have to be before swapping the tracks between them is
#: free as far as the curve is concerned (energy points).  A quarter is well
#: under the noise of any estimate Parity makes, and real libraries clump far
#: tighter than that.
TIE_TOLERANCE = 0.25


def _vary_equal_energy(
    chosen: list[Candidate], pool: list[Candidate], rng: random.Random
) -> list[Candidate]:
    """Draw each group of near-identical energies afresh, keeping the counts.

    Real libraries clump: rekordbox exports no energy column, so every track
    with no genre, no stars and no energy tag lands on the same BPM-derived
    number -- this library has thirty tracks on E4.2.  Two of them a tenth of an
    energy point apart are indistinguishable to any of this, so which one fills
    a slot changes nothing about how the set follows the curve, only about what
    the set *is*.  Drawing afresh each build is why pressing Build twice is
    worth doing instead of getting the same twelve tracks back, and the counts
    stay exactly as the fit chose them, so the curve does not move.
    """
    buckets: dict[int, list[Candidate]] = {}
    for track in pool:
        buckets.setdefault(int(round(track.energy / TIE_TOLERANCE)), []).append(track)

    wanted: dict[int, int] = {}
    for track in chosen:
        bucket = int(round(track.energy / TIE_TOLERANCE))
        wanted[bucket] = wanted.get(bucket, 0) + 1

    varied: list[Candidate] = []
    for bucket in sorted(wanted):
        candidates = list(buckets.get(bucket, []))
        rng.shuffle(candidates)
        varied.extend(candidates[: wanted[bucket]])
    return varied


#: How far from a slot's target a replacement track may sit before it stops
#: being worth considering (energy points), and how many of them to try.
REFINE_WINDOW = 1.5
REFINE_CANDIDATES = 60


def _slot_cost(
    order: list[Candidate], index: int, track: Candidate, targets: list[float], config: BuildConfig
) -> float:
    """What one slot costs: its own distance from the curve plus its mixes."""
    target = targets[index] if index < len(targets) else 5.0
    cost = energy_cost(track, target, config)
    if index:
        cost += _transition_penalty(order[index - 1], track, config)
    if index + 1 < len(order):
        cost += _transition_penalty(track, order[index + 1], config)
    return cost


def refine_selection(
    order: list[Candidate],
    spare: list[Candidate],
    targets: list[float],
    config: BuildConfig,
    locked: set[int] | None = None,
    rounds: int = 2,
) -> list[Candidate]:
    """Swap in pool tracks the curve fit left out, when the whole set gains.

    :func:`fit_curve` picks the group by energy alone, so it can choose a track
    no rearrangement will sequence -- the pool's only 120 BPM cut next to two
    140s, or one that clashes with everything around it.  This pass puts each
    slot up against the near-miss tracks the fit rejected and keeps a swap only
    when the total cost (curve, key, tempo) improves, so the set stays as close
    to the curve as the pool allows while becoming one you can actually mix.
    """
    if not spare or len(order) < 2:
        return order

    current = list(order)
    pinned = set(locked or ())
    spares = sorted(spare, key=lambda track: (track.energy, track.key))
    energies = [track.energy for track in spares]

    for _ in range(max(1, rounds)):
        improved = False
        for index in range(len(current)):
            if index in pinned:
                continue  # a pinned track is not the search's to move
            target = targets[index] if index < len(targets) else 5.0
            low = bisect.bisect_left(energies, target - REFINE_WINDOW)
            high = bisect.bisect_right(energies, target + REFINE_WINDOW)
            window = spares[low:high]
            if len(window) > REFINE_CANDIDATES:
                window = sorted(
                    window, key=lambda track: (abs(track.energy - target), track.key)
                )[:REFINE_CANDIDATES]
            baseline = _slot_cost(current, index, current[index], targets, config)
            best_cost, best_track = baseline, None
            for candidate in window:
                cost = _slot_cost(current, index, candidate, targets, config)
                if cost < best_cost - 1e-9:
                    best_cost, best_track = cost, candidate
            if best_track is None:
                continue
            displaced = current[index]
            current[index] = best_track
            spares = [track for track in spares if track.key != best_track.key]
            spares.append(displaced)
            spares.sort(key=lambda track: (track.energy, track.key))
            energies = [track.energy for track in spares]
            improved = True
        if not improved:
            break
    return current


def _nearby(
    spares: list[Candidate],
    energies: list[float],
    target: float,
    radius: float = REFINE_WINDOW,
) -> list[Candidate]:
    """The spares close enough to a slot's target to be worth trying there.

    ``radius`` is how far away still counts as close; a Strict repair clearing a
    clash passes an unlimited one, because a track in the right key is worth
    having even at an energy the curve would rather not pay.
    """
    if not spares:
        return []
    low = 0 if radius == float("inf") else bisect.bisect_left(energies, target - radius)
    high = len(spares) if radius == float("inf") else bisect.bisect_right(energies, target + radius)
    window = spares[low:high]
    if len(window) > REFINE_CANDIDATES:
        window = sorted(window, key=lambda track: (abs(track.energy - target), track.key))[
            :REFINE_CANDIDATES
        ]
    return window


#: The score at which a transition counts as harmonically fine (Camelot: a
#: perfect, relative, adjacent or diagonal pair).  Below it the pair is a
#: two-step, a tritone or unknown, and Strict mode treats it as a fault.
HARMONIC_SCORE = 0.75

#: How much curve fit a Strict build may spend per weak transition it removes,
#: in units of the key weight -- a fault carries ``(1 - HARMONIC_SCORE)`` of key
#: penalty, so this lets the fix cost up to that again in energy and tempo.  A
#: build that has to choose between the curve and Strict was asked for Strict.
STRICT_REPAIR_BUDGET = 1.5

#: How many slots a Strict repair may refill at once, and how many tracks each
#: of them may be offered.  Some faults cannot be fixed by swapping any two
#: tracks: 4A followed by 6A is two steps of the wheel, and if the tracks either
#: side are 2A and 7A there is nowhere to put either of them.  Fixing that means
#: changing two or three tracks together and bringing in tracks the curve fit
#: rejected, which is what this window search does.
STRICT_REPAIR_WINDOW = 5
STRICT_REPAIR_CANDIDATES = 8

#: How many times a Strict repair may swing its window across the whole set.
#: Each swing takes the first improvement and starts again, so this bounds the
#: search (it is the only part of a build that is not linear in the set length)
#: while still leaving room for the two or three moves a stubborn fault needs.
STRICT_REPAIR_SWEEPS = 6


def _weak_positions(order: list[Candidate]) -> list[int]:
    """Indices of transitions whose Camelot score is below the bar."""
    return [
        index
        for index in range(1, len(order))
        if order[index - 1].camelot
        and order[index].camelot
        and camelot.compat_score(order[index - 1].camelot, order[index].camelot)
        < HARMONIC_SCORE
    ]


def _weak_pair(a: Candidate, b: Candidate) -> bool:
    """Whether the transition A -> B scores below the Strict bar."""
    return bool(a.camelot and b.camelot and camelot.compat_score(a.camelot, b.camelot) < HARMONIC_SCORE)


def _is_clash(a: Candidate, b: Candidate) -> bool:
    """A tritone or otherwise unmusical pair -- what Strict promises never to keep."""
    return bool(a.camelot and b.camelot and camelot.compat_score(a.camelot, b.camelot) == 0.0)


def _clash_count(order: list[Candidate]) -> int:
    """How many transitions in ``order`` are clashes."""
    return sum(1 for index in range(1, len(order)) if _is_clash(order[index - 1], order[index]))


def _repair_window(
    order: list[Candidate],
    targets: list[float],
    config: BuildConfig,
    spare: list[Candidate],
    start: int,
    end: int,
    pinned: set[int],
    allowance: float,
    wide: bool = False,
) -> list[Candidate] | None:
    """Best way to refill slots ``start..end`` from the pool, or ``None``.

    Every free slot in the window is offered the tracks already in the window
    plus the near-miss pool tracks for its own target, and the search walks the
    assignments in slot order so a transition is priced as soon as both its
    tracks are known -- which prunes any branch already carrying more faults
    than the best complete refill.  Faults outside the window cannot change, so
    only the cost of a leaf is left to compute.

    A refill is kept when it removes a fault within the Strict budget -- or, if
    it clears a clash, whatever it costs and whatever the track's energy
    (``wide``) -- or when it costs less at the same fault count.  ``None`` means
    the window is as good as it gets.
    """
    positions = list(range(start, end + 1))
    free = [position for position in positions if position not in pinned]
    if not free:
        return None

    elsewhere = {track.key for index, track in enumerate(order) if index not in positions}
    window_tracks = [order[position] for position in free]
    options: dict[int, list[Candidate]] = {}
    for position in free:
        target = targets[position] if position < len(targets) else 5.0
        choices: list[Candidate] = []
        seen: set[str] = set()
        pool_here = _nearby(
            spare,
            [t.energy for t in spare],
            target,
            radius=float("inf") if wide else REFINE_WINDOW,
        )[:STRICT_REPAIR_CANDIDATES]
        for candidate in window_tracks + pool_here:
            if candidate.key in seen or candidate.key in elsewhere:
                continue
            seen.add(candidate.key)
            choices.append(candidate)
        choices.sort(key=lambda track: (abs(track.energy - target), track.key))
        options[position] = choices[:STRICT_REPAIR_CANDIDATES]

    earlier_faults = _weak_positions(order)
    # Faults away from the window cannot change; faults inside it between two
    # tracks that both stay put are a floor.  Everything else is priced as the
    # walk assigns it, and anything it misses only makes the running count a
    # lower bound, which is what the prune needs.
    untouched = sum(1 for index in earlier_faults if index < start or index > end + 1)
    fixed_faults = sum(
        1
        for index in earlier_faults
        if start <= index <= end + 1 and index not in free and (index - 1) not in free
    )
    clashes_now = _clash_count(order)
    best_key = (len(earlier_faults), total_cost(order, targets, config))
    best: list[Candidate] | None = None

    trial = list(order)
    taken: set[str] = set()

    def walk(depth: int, inside: int) -> None:
        nonlocal best, best_key
        if untouched + fixed_faults + inside > best_key[0]:
            return
        if depth == len(free):
            count = len(_weak_positions(trial))
            if count > best_key[0]:
                return
            cost = total_cost(trial, targets, config)
            if (
                count < best_key[0]
                and cost > allowance
                and _clash_count(trial) >= clashes_now
            ):
                return  # a fix the curve cannot afford, and no clash cleared
            if (count, cost) < best_key:
                best, best_key = list(trial), (count, cost)
            return
        position = free[depth]
        for candidate in options[position]:
            if candidate.key in taken:
                continue
            trial[position] = candidate
            taken.add(candidate.key)
            added = 1 if position and _weak_pair(trial[position - 1], candidate) else 0
            walk(depth + 1, inside + added)
            taken.discard(candidate.key)
            trial[position] = order[position]

    walk(0, 0)
    return best


def resolve_clashes(
    order: list[Candidate],
    targets: list[float],
    config: BuildConfig,
    spare: list[Candidate] | None = None,
    locked: set[int] | None = None,
) -> list[Candidate]:
    """Trade positions until a Strict set has no weak transitions left.

    The annealer proposes moves at random, so it can simply never try the one
    pair that removes the last fault -- and in Strict mode a weak transition (a
    clash, or the two-step below :data:`HARMONIC_SCORE`) is what the user asked
    not to have.  This walks the faults it can see, over every swap and every
    near-miss replacement for either side of the pair.

    It is allowed to spend a little of the curve to do it
    (:data:`STRICT_REPAIR_BUDGET` per fault removed), because on a real pool the
    keys in the chosen group sometimes cannot be chained harmonically at all --
    there is no 5A between the 4A and the 6A -- and then no rearrangement fixes
    the pair: a *different track* has to be chosen.  Cost-lowering moves are
    taken first and for free, so this only spends when Strict would otherwise
    go unmet.
    """
    current = list(order)
    if len(current) < 2 or config.strictness != "strict":
        return current

    pinned = set(locked or ())
    spares = sorted(spare or [], key=lambda track: (track.energy, track.key))
    energies = [track.energy for track in spares]
    budget_per_fault = STRICT_REPAIR_BUDGET * config.key_weight_effective()
    sweeps = 0

    def moves(faults: list[int]):
        """Every single swap or replacement around the weak pairs."""
        for index in faults:
            if index in pinned:
                continue  # a pinned track is not this pass's to move
            for swap_with in range(len(current)):
                if swap_with in (index, index - 1) or swap_with in pinned:
                    continue
                trial = list(current)
                trial[index], trial[swap_with] = trial[swap_with], trial[index]
                yield trial
            # Either side of the weak pair can be the one that moves; a swap
            # covers both, a replacement has to be tried for each.
            for position in (index - 1, index):
                if position in pinned or position < 0:
                    continue
                target = targets[position] if position < len(targets) else 5.0
                for candidate in _nearby(spares, energies, target):
                    trial = list(current)
                    trial[position] = candidate
                    yield trial

    # Each pass takes the move that removes a fault, or -- when none can -- the
    # one that lowers the cost outright.  Fault count comes first, so the pass
    # cannot undo a fix it has just paid for by finding the old order marginally
    # cheaper, which is exactly the trade this is meant to make.  Faults fall
    # every pass that spends, and cost falls otherwise, so the loop terminates;
    # the cap is only there to bound the polish.
    for _ in range(len(current) * 3):
        faults = _weak_positions(current)
        if not faults:
            break
        current_cost = total_cost(current, targets, config)
        # A two-step costs curve fit to fix and Strict is allowed only so much of
        # it; a clash is the one thing Strict exists to prevent, so it is fixed
        # at any price.
        allowance = current_cost + budget_per_fault
        clashes = _clash_count(current)
        best: list[Candidate] | None = None
        best_key = (len(faults), current_cost)
        for trial in moves(faults):
            cost = total_cost(trial, targets, config)
            count = len(_weak_positions(trial))
            if (count, cost) >= best_key:
                continue
            if (
                count < len(faults)
                and cost > allowance
                and _clash_count(trial) >= clashes
            ):
                continue  # a fix, but one the curve cannot afford
            best, best_key = trial, (count, cost)
        if best is None:
            # No single move helps, so widen it.  A fault can need two or three
            # tracks changed together, and the track that has to move is often
            # not one of the pair -- it is a quiet polish elsewhere that brings
            # the right key into reach -- so the window is swung across the whole
            # set and the first improvement is taken before looking again.
            if sweeps < STRICT_REPAIR_SWEEPS:
                sweeps += 1
                for start in range(0, len(current)):
                    end = min(len(current) - 1, start + STRICT_REPAIR_WINDOW - 1)
                    if end == start:
                        continue  # one free slot is a replacement, tried above
                    trial = _repair_window(
                        current, targets, config, spares, start, end, pinned, allowance,
                        wide=bool(clashes),
                    )
                    if trial is None:
                        continue
                    key = (len(_weak_positions(trial)), total_cost(trial, targets, config))
                    if key < best_key:
                        best, best_key = trial, key
                        break
        if best is None:
            break  # this pool cannot do better; the stats report what is left
        current = best
    return current


def _eligible(pool: list[Candidate], config: BuildConfig) -> list[Candidate]:
    result = []
    for track in pool:
        if track.key in config.excluded:
            continue
        if not (config.min_energy <= track.energy <= config.max_energy):
            continue
        if config.min_bpm and track.bpm and track.bpm < config.min_bpm:
            continue
        if config.max_bpm and track.bpm and track.bpm > config.max_bpm:
            continue
        result.append(track)
    return result


def build_set(pool: list[Candidate], config: BuildConfig) -> BuildResult:
    """Choose and order tracks from ``pool`` to match the configured curve."""
    warnings: list[str] = []
    available = _eligible(pool, config)
    by_key = {track.key: track for track in available}

    if not available:
        return BuildResult([], [], 0.0, [], [], ["No tracks left after filtering."])

    length = max(1, min(int(config.length_tracks), len(available)))
    if length < config.length_tracks:
        warnings.append(
            f"Only {length} tracks available after filtering (asked for {config.length_tracks})."
        )

    rng = random.Random(config.seed)
    config = _copy_with_length(config, length)
    targets = curve_from_points(config.curve_points or template_points(config.template), length)

    order: list[Candidate | None] = [None] * length
    used: set[str] = set()
    anchored: dict[int, str] = {}  # position -> track key that must stay put

    # Place pinned tracks first so the greedy fill has anchors to work around.
    for position, track_key in sorted(config.locks.items()):
        if 0 <= position < length and track_key in by_key and track_key not in used:
            order[position] = by_key[track_key]
            anchored[position] = track_key
            used.add(track_key)
        elif track_key not in by_key:
            warnings.append("A locked track was filtered out of the pool.")
    if config.start_key and config.start_key in by_key and order[0] is None:
        order[0] = by_key[config.start_key]
        anchored[0] = config.start_key
        used.add(config.start_key)
    if config.end_key and config.end_key in by_key and order[length - 1] is None:
        order[length - 1] = by_key[config.end_key]
        anchored[length - 1] = config.end_key
        used.add(config.end_key)

    remaining = [track for track in available if track.key not in used]
    free = [position for position in range(length) if order[position] is None]
    placed = fit_curve(remaining, targets, free, rng)
    chosen = {track.key for track in placed.values()}
    spare = [track for track in remaining if track.key not in chosen]
    for position, track in placed.items():
        order[position] = track
        used.add(track.key)

    filled = [track for track in order if track is not None]
    if len(filled) < length:
        warnings.append(f"Filled {len(filled)} of {length} slots.")

    # The curve fit chose the group; this lets near-miss tracks back in when
    # they make the set mixable.  Pinned slots stay exactly where they are.
    if spare and len(filled) > 1:
        filled = refine_selection(filled, spare, targets, config, locked=set(anchored))

    # Pinned tracks (locks and the chosen start/end) must survive optimisation.
    anchored = {position: key for position, key in anchored.items() if position < len(filled)}
    # Pinned tracks are never in ``spare``: they were placed before the fit, so
    # the fit never saw them and cannot hand one back as a replacement.
    current = _anneal(filled, targets, config, rng, locked=anchored, spare=spare)
    before_repair = _weak_positions(current)
    current = resolve_clashes(current, targets, config, spare, locked=set(anchored))
    resolved = len(before_repair) - len(_weak_positions(current))
    if resolved:
        warnings.append(
            f"Strict rebuilt {resolved} transition{'s' if resolved > 1 else ''} that "
            f"clashed or missed the Camelot bar into a harmonic mix."
        )
    warnings.extend(strict_notes(current, config))
    warnings.extend(curve_notes(available, current, targets))
    return _result(current, targets, config, warnings)


def strict_notes(order: list[Candidate], config: BuildConfig) -> list[str]:
    """Say so when a Strict set could not be made harmonic all the way through.

    Strict spends a bounded amount of the curve to fix a weak transition, and a
    pool can run out of ways to bridge a gap in the Camelot wheel -- there is no
    legal key between a 4A and a 6A if the only tracks with one cost more energy
    than Strict will pay -- so the honest thing is to name the pair rather than
    leave the user staring at a percentage.
    """
    if config.strictness != "strict":
        return []
    weak = _weak_positions(order)
    if not weak:
        return []
    index = weak[0]
    a, b = order[index - 1], order[index]
    return [
        f"Strict could not make every mix harmonic: {len(weak)} of {len(order) - 1} "
        f"transition(s) are two-steps or clashes, the first being {a.label()} "
        f"({a.camelot}) \u2192 {b.label()} ({b.camelot}). The pool has nothing that "
        f"bridges them within the fit Strict will spend -- add a track in a key "
        f"between the two, or switch to Normal."
    ]


def optimize_set(
    tracks: list[Candidate],
    config: BuildConfig,
    targets: list[float] | None = None,
) -> BuildResult:
    """Improve an existing order, keeping locked positions exactly in place."""
    warnings: list[str] = []
    if not tracks:
        return BuildResult([], [], 0.0, [], [], ["Nothing to optimize."])

    by_key = {track.key: track for track in tracks}
    length = len(tracks)
    ordered: list[Candidate | None] = list(tracks)

    for position, track_key in sorted(config.locks.items()):
        track = by_key.get(track_key)
        if track is None or not (0 <= position < length):
            continue
        current_index = next(
            (i for i, item in enumerate(ordered) if item is not None and item.key == track_key), None
        )
        if current_index is None:
            continue
        if current_index != position:
            # Lift the pinned track into its slot; everything else shifts along so
            # nothing is lost or duplicated.
            ordered.insert(position, ordered.pop(current_index))

    locked_positions = {
        position: track_key
        for position, track_key in config.locks.items()
        if 0 <= position < length and track_key in by_key
    }
    rng = random.Random(config.seed if config.seed is not None else 0)

    if targets is None:
        targets = curve_from_points(config.curve_points or template_points(config.template), length)

    compact = [track for track in ordered if track is not None]
    if locked_positions:
        warnings.append(f"{len(locked_positions)} locked position(s) preserved.")
    improved = _anneal(compact, targets, config, rng, locked=locked_positions)
    return _result(improved, targets, config, warnings)


def _copy_with_length(config: BuildConfig, length: int) -> BuildConfig:
    clone = BuildConfig.from_dict(config.to_dict())
    clone.length_tracks = length
    return clone


def _anneal(
    order: list[Candidate],
    targets: list[float],
    config: BuildConfig,
    rng: random.Random,
    locked: dict[int, str] | None = None,
    spare: list[Candidate] | None = None,
) -> list[Candidate]:
    """Simulated annealing over swap / move / reverse / swap-in moves.

    Swap-in is the one that lets the search change *which* tracks are in the set:
    a slot trades its track for one the curve fit rejected, if the whole set
    gains.  Reordering alone cannot fix a group no arrangement will sequence,
    and that is exactly the case the fit leaves behind -- it picks by energy, so
    it can hand back fourteen tracks whose keys cannot be chained.
    """
    size = len(order)
    if size < 3:
        return order

    locked = locked or {}
    locked_positions = set(locked)
    current = list(order)
    current_cost = total_cost(current, targets, config)
    best, best_cost = list(current), current_cost

    spares = sorted(spare or [], key=lambda track: (track.energy, track.key))
    spare_energies = [track.energy for track in spares]

    iterations = max(0, int(config.iterations))
    if not iterations:
        return best

    start_temp = max(0.4, 0.35 * current_cost / max(1, size))
    for step in range(iterations):
        temperature = start_temp * (1.0 - step / iterations) + 1e-4
        move = rng.random()
        snapshot = list(current)
        applied = False
        swapped: tuple[int, Candidate] | None = None

        if move < 0.40:
            i, j = rng.randrange(size), rng.randrange(size)
            if i != j and i not in locked_positions and j not in locked_positions:
                current[i], current[j] = current[j], current[i]
                applied = True
        elif move < 0.78:
            i, j = rng.randrange(size), rng.randrange(size)
            # A move shifts everything between i and j along, so it may not cross
            # a pinned slot even when neither end of it is pinned -- otherwise
            # the pin slides to a new position and the track it was holding
            # drops out of the set entirely.
            if (
                i != j
                and i not in locked_positions
                and j not in locked_positions
                and not any(
                    position in locked_positions
                    for position in range(min(i, j), max(i, j) + 1)
                )
            ):
                current.insert(j, current.pop(i))
                applied = True
        elif move < 0.88 or not spares:
            i, j = rng.randrange(size), rng.randrange(size)
            if i < j and not any(position in locked_positions for position in range(i, j + 1)):
                current[i : j + 1] = reversed(current[i : j + 1])
                applied = True
        else:
            position = rng.randrange(size)
            if position not in locked_positions:
                target = targets[position] if position < len(targets) else 5.0
                low = bisect.bisect_left(spare_energies, target - REFINE_WINDOW)
                high = bisect.bisect_right(spare_energies, target + REFINE_WINDOW)
                if high > low:
                    candidate = spares[rng.randrange(low, high)]
                    swapped = (position, current[position])
                    current[position] = candidate
                    applied = True

        if not applied:
            continue

        new_cost = total_cost(current, targets, config)
        delta = new_cost - current_cost
        if delta <= 0 or rng.random() < math.exp(-delta / max(temperature, 1e-6)):
            current_cost = new_cost
            if swapped is not None:
                # The track that just left the set takes its place in the pool
                # of tracks the search may bring back in.
                position, displaced = swapped
                spares = [track for track in spares if track.key != current[position].key]
                bisect.insort(spares, displaced, key=lambda track: (track.energy, track.key))
                spare_energies = [track.energy for track in spares]
            if new_cost < best_cost:
                best, best_cost = list(current), new_cost
        else:
            current = snapshot

    # Locks are authoritative: make sure the result honours them exactly.
    for position, track_key in locked.items():
        if position >= len(best) or best[position].key == track_key:
            continue
        source = next((i for i, track in enumerate(best) if track.key == track_key), None)
        if source is not None:
            best[position], best[source] = best[source], best[position]
    return best


def curve_notes(pool: list[Candidate], order: list[Candidate], targets: list[float]) -> list[str]:
    """Why the set did not follow the curve, in plain numbers.

    The chart shows a target line and the line the set actually makes.  When
    those two do not line up the cause is nearly always the pool rather than the
    search -- a target no track in it can reach, or a library where most tracks
    share one estimated energy because rekordbox exports no energy column -- so
    the build says which of those it is instead of quietly flattening out.
    """
    if not order or not pool or not targets:
        return []

    notes: list[str] = []
    quiet = min(pool, key=lambda track: (track.energy, track.key))
    loud = max(pool, key=lambda track: (track.energy, track.key))
    target_low, target_high = min(targets), max(targets)

    if target_low < quiet.energy - 0.25:
        notes.append(
            f"The curve opens at E{target_low:.1f} but the quietest track in the pool is "
            f"E{quiet.energy:.1f} ({quiet.label()}), so the set cannot start that low."
        )
    if target_high > loud.energy + 0.25:
        notes.append(
            f"The curve peaks at E{target_high:.1f} but the loudest track in the pool is "
            f"E{loud.energy:.1f} ({loud.label()}), so the peak comes out short."
        )

    actual = [track.energy for track in order]
    wanted_span = target_high - target_low
    actual_span = max(actual) - min(actual)
    pool_span = loud.energy - quiet.energy
    mean_miss = sum(
        abs(track.energy - (targets[index] if index < len(targets) else 5.0))
        for index, track in enumerate(order)
    ) / len(order)
    # The pool can be wide overall and still have nothing where the curve lives:
    # a library of mastered club tracks has a handful of quiet records and a
    # hundred loud ones, so a Chill curve picks up the handful and then has to
    # fill the rest of the set from far above the line.  That is the failure the
    # other notes miss -- the pool spans E2-E10 and the target is reachable, just
    # not twenty times over -- and it is worth counting out loud, but only when
    # the set actually came out far from the curve, or every narrow flat curve
    # would collect a note about how few tracks sit on it.
    in_band = [
        track for track in pool if target_low - 0.5 <= track.energy <= target_high + 0.5
    ]
    if mean_miss >= 0.5 and 0 < len(in_band) < len(order):
        notes.append(
            f"The curve asks for E{target_low:.1f}–E{target_high:.1f} and only {len(in_band)} of the "
            f"pool's {len(pool)} tracks measure inside that band, against a set of {len(order)} "
            f"tracks, so the rest of the set has to come from outside it."
        )
    elif wanted_span >= 0.75 and pool_span < wanted_span:
        # The set follows the curve as well as the pool allows and still comes
        # out flat, because the pool has less range in it than the curve asks
        # for.  Saying so beats leaving the user to wonder why the line is
        # straight -- which is what happens once energies are measured rather
        # than guessed, because a library of mastered club tracks really does sit
        # in a narrow band.
        notes.append(
            f"The pool's energies span E{quiet.energy:.1f}–E{loud.energy:.1f} against a curve of "
            f"E{target_low:.1f}–E{target_high:.1f}, so there is not enough range in it to build "
            f"that shape whatever the order."
        )
    elif wanted_span >= 0.75 and actual_span < 0.6 * wanted_span:
        notes.append(
            f"The set only spans E{min(actual):.1f}–E{max(actual):.1f} against a curve of "
            f"E{target_low:.1f}–E{target_high:.1f}: the pool's estimates sit in that band, so the "
            f"shape flattens out however the tracks are ordered."
        )

    low = sum(1 for track in order if track.confidence == "low")
    if low >= max(2, len(order) // 2):
        notes.append(
            f"{low} of {len(order)} tracks take their energy from BPM alone (no genre, no stars, no "
            f"energy tag). Type real values into the Energy column and rebuild to make the curve hold."
        )
    return notes


def _result(
    order: list[Candidate],
    targets: list[float],
    config: BuildConfig,
    warnings: list[str],
) -> BuildResult:
    transitions: list[Transition] = []
    for index in range(1, len(order)):
        a, b = order[index - 1], order[index]
        transitions.append(
            Transition(
                index=index,
                key_score=camelot.compat_score(a.camelot, b.camelot),
                label=camelot.compat_label(a.camelot, b.camelot),
                bpm_delta=round(bpm_delta(a.bpm, b.bpm), 1),
            )
        )

    bpm_values = [t.bpm for t in order if t.bpm]
    energies = [t.energy for t in order]
    clashes = sum(1 for t in transitions if t.label == "clash")
    deviations = [
        abs(track.energy - (targets[index] if index < len(targets) else 5.0))
        for index, track in enumerate(order)
    ]
    stats = {
        "count": len(order),
        "duration": round(sum(t.duration for t in order), 1),
        "avg_energy": round(sum(energies) / len(energies), 2) if energies else 0.0,
        "peak_energy": round(max(energies), 2) if energies else 0.0,
        # How closely the set followed the curve: the average distance of a
        # track from its target, and the worst single miss.
        "curve_fit": round(sum(deviations) / len(deviations), 2) if deviations else 0.0,
        "curve_worst": round(max(deviations), 2) if deviations else 0.0,
        "curve_target_range": (
            [round(min(targets), 2), round(max(targets), 2)] if targets else [0.0, 0.0]
        ),
        "curve_actual_range": (
            [round(min(energies), 2), round(max(energies), 2)] if energies else [0.0, 0.0]
        ),
        "low_confidence": sum(1 for track in order if track.confidence == "low"),
        "bpm_min": round(min(bpm_values), 1) if bpm_values else 0.0,
        "bpm_max": round(max(bpm_values), 1) if bpm_values else 0.0,
        "harmonic_ok": round(
            100.0 * sum(1 for t in transitions if t.key_score >= 0.75) / len(transitions), 1
        )
        if transitions
        else 100.0,
        "clashes": clashes,
        "beatmatched": round(
            100.0
            * sum(1 for t in transitions if t.bpm_delta <= max(config.bpm_tolerance, 0.5))
            / len(transitions),
            1,
        )
        if transitions
        else 100.0,
    }
    return BuildResult(
        order=[track.key for track in order],
        transitions=transitions,
        cost=round(total_cost(order, targets, config), 3),
        energy_curve=[round(track.energy, 2) for track in order],
        targets=[round(value, 2) for value in targets[: len(order)]],
        warnings=warnings,
        stats=stats,
    )
