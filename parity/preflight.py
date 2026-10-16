"""Pre-flight: what is wrong with a set before it leaves the app.

An export is a copy of the set, not the library, so a broken track in it is not
dangerous -- it is just useless: a row in rekordbox you cannot play, or the same
track twice in the list you are about to hand over.  This module answers the
questions worth answering before a file is written:

* **is there a file behind every track?**  An unplugged drive, a renamed folder
  or a track that was never downloaded leaves a path that does not resolve, and
  nothing downstream notices -- the XML still lists it and rekordbox still shows
  a row.  Streaming entries are reported too: they can travel in the XML, but
  they cannot be played from the package on the laptop you take out.
* **is the same track in the set twice?**  The same track added twice, two
  library entries pointing at one file, or one file imported under two names --
  all three look like a proper set and all three lose a slot.

Nothing here blocks an export.  The user decided what goes in the set; the job
of pre-flight is to say what will go wrong, in time to change it, and to write
that down in the package so the warning travels with the set.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

#: Ordered worst first: the report is read top to bottom.
SEVERITIES = ("error", "warning")

#: What each issue kind is called in the report, and how bad it is.
ISSUE_KINDS: dict[str, dict] = {
    "missing_file": {
        "severity": "error",
        "title": "Tracks with no file behind them",
        "summary": "nothing to play",
    },
    "duplicate_track": {
        "severity": "error",
        "title": "The same track listed twice",
        "summary": "the same track takes two slots",
    },
    "duplicate_file": {
        "severity": "error",
        "title": "Two tracks pointing at one file",
        "summary": "one file, listed as two tracks",
    },
    "duplicate_name": {
        "severity": "warning",
        "title": "Same artist and title, different tracks",
        "summary": "looks like a duplicate import",
    },
    "streaming": {
        "severity": "warning",
        "title": "Streaming-only tracks",
        "summary": "no local file to play",
    },
    "no_cues": {
        "severity": "warning",
        "title": "Tracks with no cues generated yet",
        "summary": "the export carries no cue points for them",
    },
}


def _label(track: dict) -> str:
    artist = str(track.get("artist") or "").strip()
    title = str(track.get("title") or "").strip() or "Untitled"
    return f"{artist} - {title}" if artist else title


def _row(kind: str, track: dict, *, detail: str = "", positions: list[int] | None = None, **extra) -> dict:
    info = ISSUE_KINDS[kind]
    return {
        "kind": kind,
        "severity": info["severity"],
        "key": str(track.get("key") or ""),
        "label": _label(track),
        "path": str(track.get("path") or ""),
        "detail": detail,
        "positions": list(positions or []),
        **extra,
    }


def inspect(tracks: list[dict], *, check_cues: bool = True, set_name: str = "") -> dict:
    """Check a built set, in order, and report what would go wrong.

    ``tracks`` is the set as the API hands it over (``key``, ``path``,
    ``streaming``, ``cues``, the names).  ``check_cues`` is off when the export
    is not carrying cues, so the report never complains about a step the user
    turned off.  ``set_name`` only titles the text report.  Every issue is a
    fact read from the set and the disk -- no guessing about the music.
    """
    rows = list(tracks)
    issues: list[dict] = []
    seen_paths: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    seen_keys: dict[str, list[int]] = defaultdict(list)
    seen_names: dict[str, list[int]] = defaultdict(list)

    for index, track in enumerate(rows, start=1):
        key = str(track.get("key") or "")
        if not key:
            # A track with no key is not a track listed twice, and grouping them
            # under "" would report every keyless row as a duplicate of the
            # first one.  It still gets the checks below, which work off the
            # path and the names.
            continue
        seen_keys[key].append(index)
        path = str(track.get("path") or "")
        name = f"{str(track.get('artist') or '').strip().lower()}\u0000{str(track.get('title') or '').strip().lower()}"
        seen_names[name].append(index)

        if track.get("streaming"):
            issues.append(
                _row(
                    "streaming",
                    track,
                    detail="only plays from a streaming service; the XML lists it but there is no file to carry",
                    positions=[index],
                )
            )
        elif not path:
            issues.append(
                _row("missing_file", track, detail="no file path recorded for this track", positions=[index])
            )
        elif Path(path).expanduser().is_file():
            seen_paths[path].append((index, track))
        else:
            # A file that is not there is reported once per track: "two tracks
            # share this missing file" says nothing the two rows above it did
            # not already say, and it would double the error count.
            issues.append(_row("missing_file", track, detail=f"no file at {path}", positions=[index]))

        if check_cues and not (track.get("cues") or {}).get("analysed"):
            issues.append(
                _row(
                    "no_cues",
                    track,
                    detail="no cue plan stored: press Analyse cues, or the XML reads as a track with no cues",
                    positions=[index],
                )
            )

    for positions in seen_keys.values():
        if len(positions) > 1:
            track = rows[positions[0] - 1]
            issues.append(
                _row(
                    "duplicate_track",
                    track,
                    detail=f"the same track at {_positions(positions)}",
                    positions=positions,
                )
            )

    for path, entries in seen_paths.items():
        if len(entries) > 1:
            entries.sort(key=lambda item: item[0])
            track = entries[0][1]
            issues.append(
                _row(
                    "duplicate_file",
                    track,
                    detail=f"one file at {_positions([i for i, _ in entries])}: {path}",
                    positions=[i for i, _ in entries],
                )
            )

    for positions in seen_names.values():
        if len(positions) < 2:
            continue
        paths = {str(rows[index - 1].get("path") or "") for index in positions}
        if len(paths) < 2:
            # Same file: already reported as a duplicate entry; saying it twice
            # would make the count mean nothing.
            continue
        issues.append(
            _row(
                "duplicate_name",
                rows[positions[0] - 1],
                detail=f"same artist and title at {_positions(positions)}, from different files",
                positions=positions,
            )
        )

    issues.sort(key=lambda issue: (SEVERITIES.index(issue["severity"]), issue["positions"] or [0]))

    summary: dict[str, int] = {kind: 0 for kind in ISSUE_KINDS}
    for issue in issues:
        summary[issue["kind"]] += 1
    counts = {severity: sum(1 for issue in issues if issue["severity"] == severity) for severity in SEVERITIES}

    report = {
        "ok": counts["error"] == 0,
        "checked": len(rows),
        "issues": issues,
        "summary": summary,
        "counts": counts,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    report["text"] = render_text(report, set_name)
    return report


def _positions(positions: list[int]) -> str:
    listed = [str(position) for position in positions]
    if len(listed) == 1:
        return f"track {listed[0]}"
    return "tracks " + ", ".join(listed[:-1]) + f" and {listed[-1]}"


def render_text(report: dict, set_name: str = "") -> str:
    """The report as plain text: what goes in the package and the log."""
    lines = ["Parity pre-flight check"]
    if set_name:
        lines.append(set_name)
    lines.append(f"{report.get('generated', '')}")
    checked = report.get("checked", 0)
    counts = report.get("counts") or {}
    errors = counts.get("error", 0)
    warnings = counts.get("warning", 0)
    lines.append("")
    if not report.get("issues"):
        lines.append(f"All clear: {checked} tracks checked, no missing files and no duplicates.")
        return "\n".join(lines) + "\n"

    lines.append(
        f"{checked} tracks checked, {errors} error{'' if errors == 1 else 's'}, "
        f"{warnings} warning{'' if warnings == 1 else 's'}."
    )
    if errors:
        lines.append("An export still runs: nothing here blocks it, but the tracks below are not usable as they stand.")
    else:
        lines.append("Nothing here blocks an export.")

    for severity in SEVERITIES:
        section = [issue for issue in report["issues"] if issue["severity"] == severity]
        if not section:
            continue
        lines.append("")
        lines.append(f"{severity.upper()}S")
        for kind, info in ISSUE_KINDS.items():
            rows = [issue for issue in section if issue["kind"] == kind]
            if not rows:
                continue
            lines.append(f"  {info['title']} ({len(rows)} - {info['summary']})")
            for issue in rows:
                place = _positions(issue["positions"]) if issue["positions"] else "-"
                lines.append(f"    {place:<12} {issue['label']}")
                if issue["detail"]:
                    lines.append(f"                 {issue['detail']}")
    lines.append("")
    return "\n".join(lines)
