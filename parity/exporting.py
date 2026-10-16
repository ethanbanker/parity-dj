"""Export a finished set.

Formats:

* ``rekordbox``  -- a re-importable rekordbox XML.  When the library came from
  an XML export the original ``<TRACK>`` elements are copied verbatim; when it
  came from ``master.db`` they are synthesised with the same attribute set, so
  either way rekordbox's XML browser and AUTO/PILOT see a normal collection.
  Injected ``<POSITION_MARK>`` cue points and a ``<TEMPO>`` beat grid are merged
  in per track.
* ``autopilot`` -- a zip with that same XML plus a JSON manifest and a text set
  list, so one download serves both importers.
* ``package``   -- the portable set package: that XML, the set list, a cue log,
  the pre-flight report and the manifest in one zip (:func:`package_readme`
  says what to do with each part), so a set can be handed over or put away
  whole.
* ``m3u8`` / ``csv`` / ``txt`` -- playlists and readable set lists.

The comment tags (``[E7] [peak] 14/24``) are written into the XML's
``Comments`` attribute when a tag plan is passed in, merged with whatever the
track already carried -- see :mod:`parity.tags`.
"""

from __future__ import annotations

import csv
import io
import json
import time
import xml.etree.ElementTree as ET
import zipfile

from . import tags as tags_mod

#: Attribute set rekordbox writes on a collection <TRACK>.  Used when a track
#: has no original XML element to copy (i.e. it came from master.db).
_TRACK_ATTRS = (
    ("Name", "title"),
    ("Artist", "artist"),
    ("Composer", ""),
    ("Album", "album"),
    ("Grouping", ""),
    ("Genre", "genre"),
    ("Kind", "kind"),
    ("Size", ""),
    ("TotalTime", "duration"),
    ("DiscNumber", "0"),
    ("TrackNumber", "track_number"),
    ("Year", "year"),
    ("AverageBpm", "bpm"),
    ("DateAdded", "date_added"),
    ("BitRate", "0"),
    ("SampleRate", "44100"),
    ("Comments", "comments"),
    ("PlayCount", "play_count"),
    ("Rating", "rating"),
    ("Location", "location"),
    ("Remixer", ""),
    ("Tonality", "tonality"),
    ("Label", "label"),
    ("Mix", "mix"),
)


def _xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _int(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _location_from_path(path: str) -> str:
    if not path:
        return ""
    from urllib.parse import quote

    return "file://localhost" + quote(path)


# ------------------------------------------------------------ cue injection


def _cue_element(cue: dict) -> ET.Element:
    """One ``<POSITION_MARK>``: hot cue when ``num`` >= 0, memory cue at -1."""
    num = int(cue.get("num", -1))
    ms = float(cue.get("ms") or 0)
    attrs = {
        "Name": str(cue.get("name") or ""),
        "Type": "4" if cue.get("loop") else "0",
        "Start": f"{ms / 1000.0:.3f}",
        "Num": str(num),
    }
    end = cue.get("end_ms") or cue.get("end")
    if cue.get("loop") and end:
        attrs["End"] = f"{float(end) / 1000.0:.3f}"
    rgb = cue.get("rgb")
    if rgb and num >= 0:
        attrs["Red"], attrs["Green"], attrs["Blue"] = (str(int(channel)) for channel in rgb)
    return ET.Element("POSITION_MARK", attrs)


def apply_cues(element: ET.Element, plan: dict, replace_existing: bool = True) -> None:
    """Merge a cue plan into a ``<TRACK>`` element, in place."""
    hot = list(plan.get("hot") or [])
    memory = list(plan.get("memory") or [])
    existing = list(plan.get("existing") or [])

    injected = {-1} if memory else set()
    injected.update(int(cue.get("num", -1)) for cue in hot)

    if replace_existing:
        for child in list(element.findall("POSITION_MARK")):
            num = _int(child.attrib.get("Num"), -1)
            if num in injected:
                element.remove(child)

    # Keep the cues rekordbox already had that are not in one of the eight hot
    # slots -- everything A-H is Parity's, and the memory cues that duplicate
    # one of them go with the write (see parity.writedb).
    kept: set[tuple[int, int]] = set()
    for child in element.findall("POSITION_MARK"):
        kept.add((_int(child.attrib.get("Num"), -1), int(float(child.attrib.get("Start", "0")) * 1000)))

    for cue in existing:
        num = int(cue.get("num", -1))
        if num in injected:
            continue
        marker = (num, int(float(cue.get("ms") or cue.get("start") or 0)))
        if marker in kept:
            continue
        element.append(_cue_element(cue))
        kept.add(marker)

    for cue in hot + memory:
        element.append(_cue_element(cue))

    if hot or memory:
        children = list(element)
        non_marks = [child for child in children if child.tag != "POSITION_MARK"]
        marks = [child for child in children if child.tag == "POSITION_MARK"]
        for child in children:
            element.remove(child)
        for child in non_marks:
            element.append(child)
        for child in marks:
            element.append(child)


def apply_tempo(element: ET.Element, beat_grid: dict | None) -> None:
    """Give the track a beat grid, replacing rekordbox's if we have better data."""
    if not beat_grid or not beat_grid.get("bpm"):
        return
    for child in list(element.findall("TEMPO")):
        element.remove(child)
    ET.SubElement(
        element,
        "TEMPO",
        {
            "Inizio": f"{float(beat_grid.get('inizio') or 0):.3f}",
            "Bpm": f"{float(beat_grid['bpm']):.2f}",
            "Metro": str(beat_grid.get("metro") or "4/4"),
            "Battito": str(beat_grid.get("battito") or 1),
        },
    )


def apply_comment(element: ET.Element, comment: str) -> None:
    """Set a ``<TRACK>``'s comment.

    rekordbox's XML carries the comment as the ``Comments`` *attribute* of
    ``<TRACK>`` -- there is no ``<COMMENT>`` element in the format -- and this
    is the field rekordbox's own comment search reads, so it is where a set tag
    has to live to be findable in the library the file is imported into.
    """
    value = str(comment or "")
    if value:
        element.set("Comments", value)
    elif "Comments" in element.attrib:
        element.set("Comments", "")


def build_track_element(track_id: str, data: dict) -> ET.Element:
    """Synthesise a collection ``<TRACK>`` for a track with no source XML."""
    attrs: dict[str, str] = {"TrackID": str(track_id)}
    for name, field in _TRACK_ATTRS:
        if not field:
            attrs[name] = ""
        elif name == "TotalTime":
            attrs[name] = str(_int(data.get("duration")))
        elif name == "AverageBpm":
            attrs[name] = f"{float(data.get('bpm') or 0):.2f}"
        elif name == "Location":
            attrs[name] = str(data.get("location") or _location_from_path(data.get("path", "")))
        elif name in {"PlayCount", "Year", "TrackNumber", "Rating", "Size", "DiscNumber"}:
            attrs[name] = str(_int(data.get(field)))
        else:
            attrs[name] = str(data.get(field) or "")
    return ET.Element("TRACK", attrs)


# ------------------------------------------------------------------- rekordbox


def _flatten_cues(plan: dict | None) -> dict:
    """Normalise a cue plan coming from the API or the structure module."""
    if not plan:
        return {}
    hot = []
    for cue in plan.get("hot_cues") or plan.get("hot") or []:
        hot.append(
            {
                "num": int(cue.get("num", 0)),
                "name": str(cue.get("name") or ""),
                "ms": int(cue.get("ms") or float(cue.get("seconds") or 0) * 1000),
                "rgb": list(cue.get("rgb")) if cue.get("rgb") else list(cue.get("colour_rgb") or []),
                "loop": bool(cue.get("loop")),
                "end_ms": cue.get("end_ms"),
            }
        )
    memory = [
        {
            "num": -1,
            "name": str(cue.get("name") or ""),
            "ms": int(cue.get("ms") or float(cue.get("seconds") or 0) * 1000),
            "loop": False,
        }
        for cue in (plan.get("memory_cues") or plan.get("memory") or [])
    ]
    existing = [
        {
            "num": int(cue.get("num", -1)),
            "name": str(cue.get("name") or ""),
            "ms": int(cue.get("ms") or float(cue.get("start") or 0) * 1000),
            "loop": bool(cue.get("loop")),
        }
        for cue in (plan.get("existing") or [])
    ]
    return {"hot": hot, "memory": memory, "existing": existing}


def set_to_rekordbox_xml(
    library_xml: str | None,
    track_ids: list[str],
    set_name: str = "Parity Set",
    fallback_tracks: dict[str, dict] | None = None,
    cue_plans: dict[str, dict] | None = None,
    tags: dict[str, str] | None = None,
) -> bytes:
    """Build a rekordbox XML containing only the set, in order, with cues.

    ``library_xml`` is the original export when there is one; ``<TRACK>``
    elements are reused so nothing already in them is lost.  Tracks with no
    source element are synthesised from ``fallback_tracks`` (keyed by TrackID).

    ``tags`` maps a TrackID to the set tag for that track (``[E7] [peak]
    14/24``).  The tag is merged into the comment the element already has, so a
    track that has been through Parity before does not collect a second tag
    and a comment the DJ typed is kept.
    """
    elements: dict[str, ET.Element] = {}
    if library_xml:
        root = ET.fromstring(library_xml.lstrip("\ufeff"))
        collection = root.find("COLLECTION")
        if collection is not None:
            for element in collection.findall("TRACK"):
                track_id = element.attrib.get("TrackID")
                if track_id:
                    elements[track_id] = element

    # Only ids we can actually describe get exported; an id with neither a source
    # element nor a fallback record would be an empty row in rekordbox.
    known = set(elements) | set(fallback_tracks or {})
    ordered_ids = [
        track_id
        for track_id in dict.fromkeys(str(track_id) for track_id in track_ids)
        if track_id in known
    ]
    cue_plans = cue_plans or {}

    product = ET.Element("PRODUCT", {"Name": "rekordbox", "Version": "7.0.0", "Company": "AlphaTheta"})
    root = ET.Element("DJ_PLAYLISTS", {"Version": "1.0.0"})
    root.append(product)
    collection = ET.SubElement(root, "COLLECTION", {"Entries": str(len(ordered_ids))})

    for track_id in ordered_ids:
        source = elements.get(track_id)
        element = ET.fromstring(ET.tostring(source)) if source is not None else None
        if element is None:
            data = (fallback_tracks or {}).get(track_id) or {}
            element = build_track_element(track_id, data)
        plan = cue_plans.get(track_id)
        if plan:
            flat = _flatten_cues(plan)
            if flat.get("hot") or flat.get("memory") or flat.get("existing"):
                apply_cues(element, flat)
            apply_tempo(element, plan.get("beat_grid"))
        tag = (tags or {}).get(track_id)
        if tag:
            apply_comment(element, tags_mod.merge_comment(element.attrib.get("Comments", ""), tag))
        collection.append(element)

    playlists = ET.SubElement(root, "PLAYLISTS")
    playlist_root = ET.SubElement(playlists, "NODE", {"Type": "0", "Name": "ROOT", "Count": "1"})
    node = ET.SubElement(
        playlist_root,
        "NODE",
        {
            "Name": set_name or "Parity Set",
            "Type": "1",
            "KeyType": "0",
            "Entries": str(len(ordered_ids)),
        },
    )
    for track_id in ordered_ids:
        ET.SubElement(node, "TRACK", {"Key": track_id})

    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


# ------------------------------------------------------------------- autopilot


def autopilot_manifest(
    tracks: list[dict],
    set_name: str = "Parity Set",
    transitions: list[dict] | None = None,
    stats: dict | None = None,
    source: str = "",
    xml_filename: str = "",
    cue_plans: dict[str, dict] | None = None,
) -> bytes:
    """The companion manifest for AUTO/PILOT and anything else that reads JSON.

    AUTO/PILOT imports rekordbox XML, so the XML is the import file and this is
    the machine-readable record of *why* the set is in this order: the Camelot
    path, the tempo ramp and every cue timestamp.
    """
    cue_plans = cue_plans or {}
    rows = []
    for position, track in enumerate(tracks, start=1):
        track_id = str(track.get("id") or "")
        # Plans are keyed by track key internally (it survives re-imports) and by
        # TrackID for the XML; accept either so callers cannot miss.
        plan = cue_plans.get(track.get("key") or "") or cue_plans.get(track_id) or {}
        rows.append(
            {
                "position": position,
                "track_id": track_id,
                "title": track.get("title", ""),
                "artist": track.get("artist", ""),
                "genre": track.get("genre", ""),
                "bpm": track.get("bpm") or 0,
                "key": track.get("tonality", ""),
                "camelot": track.get("camelot", ""),
                "energy": track.get("energy"),
                "duration": track.get("duration") or 0,
                "path": track.get("path", ""),
                "hot_cues": [
                    {
                        "slot": cue.get("slot", ""),
                        "num": cue.get("num"),
                        "role": cue.get("role", ""),
                        "name": cue.get("name", ""),
                        "seconds": round(int(cue.get("ms") or 0) / 1000.0, 3),
                        "colour": cue.get("colour", ""),
                        "confidence": cue.get("confidence", ""),
                        "reason": cue.get("reason", ""),
                    }
                    for cue in (plan.get("hot_cues") or [])
                ],
                "memory_cues": [
                    {
                        "name": cue.get("name", ""),
                        "seconds": round(int(cue.get("ms") or 0) / 1000.0, 3),
                    }
                    for cue in (plan.get("memory_cues") or [])
                ],
            }
        )

    payload = {
        "manifest": "parity.autopilot/1",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "set_name": set_name,
        "source": source,
        "import_file": xml_filename,
        "note": (
            "AUTO/PILOT and rekordbox both import the rekordbox XML next to this "
            "file; this manifest describes the ordering and cue timestamps."
        ),
        "track_count": len(rows),
        "transitions": transitions or [],
        "stats": stats or {},
        "tracks": rows,
    }
    return json.dumps(payload, indent=2).encode("utf-8")


def _plural(count: int, word: str) -> str:
    return word if count == 1 else word + "s"


def _clock(ms: object) -> str:
    """``m:ss.mmm`` -- how a cue timestamp reads in the log and the editor."""
    total = max(0, _int(ms))
    minutes, rest = divmod(total, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{minutes}:{seconds:02d}.{millis:03d}"


def cue_log(
    tracks: list[dict],
    cue_plans: dict[str, dict] | None = None,
    set_name: str = "Parity Set",
    tags: dict[str, str] | None = None,
) -> bytes:
    """Every cue in the set, track by track, as readable text.

    The XML and the manifest say *where* the cues are; this says what they are
    -- which pad, what colour, which phrase the timestamp came from and why it
    landed there -- so a cue plan can be read, checked or argued about without
    rekordbox and without Parity.  It is the log half of the set package.
    """
    cue_plans = cue_plans or {}
    tags = tags or {}
    lines: list[str] = [
        f"{set_name} - cue log",
        "=" * max(8, len(set_name) + 10),
        f"generated {time.strftime('%Y-%m-%dT%H:%M:%S')}",
    ]

    hot_total = 0
    memory_total = 0
    locked_total = 0
    blocks: list[str] = []
    for position, track in enumerate(tracks, start=1):
        key = str(track.get("key") or "")
        track_id = str(track.get("id") or "")
        plan = cue_plans.get(key) or cue_plans.get(track_id) or {}
        hot = list(plan.get("hot_cues") or [])
        memory = list(plan.get("memory_cues") or [])
        tag = tags.get(key) or tags.get(track_id) or ""
        hot_total += len(hot)
        memory_total += len(memory)
        locked_total += sum(1 for cue in hot if cue.get("locked"))

        artist = str(track.get("artist") or "").strip()
        title = str(track.get("title") or "").strip() or "Untitled"
        label = f"{artist} - {title}" if artist else title
        heading = f"{position:>3}. {label}"
        if tag:
            heading += f"   {tag}"
        key_name = track.get("camelot") or track.get("tonality") or "?"
        body = [
            heading,
            f"     {_clock(_int(track.get('duration')) * 1000)} long · key {key_name} · "
            f"{float(track.get('bpm') or 0):g} BPM · energy {float(track.get('energy') or 0):g}",
        ]
        if plan.get("kept"):
            body.append("     cues: left as they are (this track is cued by hand)")
        if not hot and not memory:
            body.append("     cues: none generated yet")
        for cue in hot:
            source = str(cue.get("source") or "").strip()
            confidence = str(cue.get("confidence") or "").strip()
            origin = ", ".join(part for part in (source, confidence) if part)
            locked = " (locked: moved by hand)" if cue.get("locked") else ""
            body.append(
                f"     {str(cue.get('slot') or '?'):<2} {_clock(cue.get('ms')):>10}  "
                f"{str(cue.get('colour') or '-'):<7} {str(cue.get('name') or ''):<16} "
                f"{origin}{locked}"
            )
            reason = str(cue.get("reason") or "").strip()
            if reason:
                body.append(f"         {reason}")
        for cue in memory:
            body.append(
                f"     mem {_clock(cue.get('ms')):>9}  {'':<7} {str(cue.get('name') or ''):<16} "
                f"{str(cue.get('source') or '').strip()}"
            )
        blocks.append("\n".join(body))

    lines.append(
        f"{len(tracks)} {_plural(len(tracks), 'track')}, "
        f"{hot_total} hot {_plural(hot_total, 'cue')}, "
        f"{memory_total} memory {_plural(memory_total, 'cue')}"
        + (f", {locked_total} locked where you put them" if locked_total else "")
    )
    lines.append("")
    lines.extend(blocks)
    return ("\n".join(lines) + "\n").encode("utf-8")


def package_readme(
    set_name: str,
    files: list[tuple[str, str]],
    *,
    source: str = "",
    preflight: dict | None = None,
    generated: str = "",
) -> bytes:
    """The note inside a set package, built from the entries actually in it.

    ``files`` is the same ``(name, description)`` list the zip is built from,
    so the note cannot drift from the archive: a part that is added to the
    package without a line here is a bug the tests catch, not a surprise for
    the person opening the zip a year later.
    """
    name = set_name or "Parity Set"
    lines = [
        "Parity set package",
        "=" * 21,
        name,
        f"generated {generated or time.strftime('%Y-%m-%dT%H:%M:%S')}",
    ]
    if source:
        lines.append(f"library source: {source}")
    lines.append("")
    lines.append("What is in here")
    width = max((len(entry) for entry, _ in files), default=0)
    for entry, description in files:
        lines.append(f"  {entry:<{width}}  {description}")
    lines.append("")
    lines.append("How to use it")
    lines.extend(
        [
            "  1. In rekordbox: File > Import Collection in xml format, and pick the .xml here.",
            "     The cues and the tag comments come with it. Import while rekordbox is closed",
            "     for the cleanest result, and read the set list next to it to know the order.",
            "  2. To bring the set back into Parity: drag the .xml onto the Parity window (or",
            "     start it with --import), load the set list, and press Analyse cues if you want",
            "     the cue points rebuilt from rekordbox's own phrase analysis.",
            "  3. The cue log is the record of where every cue was placed and why; the manifest",
            "     is the same set as JSON, for anything that reads files instead of screens.",
        ]
    )
    lines.append("")
    if preflight is None:
        lines.append("Pre-flight: not run when this package was written.")
    elif preflight.get("issues"):
        counts = preflight.get("counts") or {}
        lines.append(
            f"Pre-flight: {counts.get('error', 0)} error(s) and {counts.get('warning', 0)} "
            f"warning(s) - see the pre-flight report in this package."
        )
    else:
        lines.append(
            f"Pre-flight: all clear - {preflight.get('checked', 0)} tracks checked, every "
            "file present, no duplicates."
        )
    lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def set_to_bundle(
    stem: str,
    stems: dict[str, bytes] | None = None,
    manifest_bytes: bytes | None = None,
) -> bytes:
    """Zip the XML, the manifest and a readable set list into one download.

    ``stem`` is the sanitised filename without an extension, so every entry in
    the archive shares one name.  ``stems`` maps full entry names to content.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (stems or {}).items():
            archive.writestr(name, content)
        if manifest_bytes is not None:
            archive.writestr(f"{stem}.autopilot.json", manifest_bytes)
    return buffer.getvalue()


# ---------------------------------------------------------------------- other


def set_to_m3u8(tracks: list[dict], set_name: str = "Parity Set") -> bytes:
    lines = ["#EXTM3U", f"#PLAYLIST:{set_name}"]
    for track in tracks:
        seconds = int(track.get("duration") or 0)
        lines.append(f"#EXTINF:{seconds},{track.get('artist', '')} - {track.get('title', '')}")
        lines.append(track.get("path") or "")
    return ("\n".join(lines) + "\n").encode("utf-8")


_CSV_FIELDS = ("position", "title", "artist", "bpm", "key", "camelot", "energy", "genre", "duration", "path")


def _row(index: int, track: dict) -> dict:
    return {
        "position": index,
        "title": track.get("title", ""),
        "artist": track.get("artist", ""),
        "bpm": track.get("bpm") or "",
        "key": track.get("tonality", ""),
        "camelot": track.get("camelot", "") or "",
        "energy": track.get("energy", ""),
        "genre": track.get("genre", ""),
        "duration": f"{int(track.get('duration') or 0) // 60}:{int(track.get('duration') or 0) % 60:02d}",
        "path": track.get("path", ""),
    }


def set_to_csv(tracks: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for index, track in enumerate(tracks, start=1):
        writer.writerow(_row(index, track))
    return buffer.getvalue().encode("utf-8")


def set_to_txt(tracks: list[dict], set_name: str = "Parity Set") -> bytes:
    lines = [set_name, "=" * len(set_name), ""]
    for index, track in enumerate(tracks, start=1):
        row = _row(index, track)
        key = row["camelot"] or row["key"] or "??"
        bpm = f"{float(row['bpm']):g}" if row["bpm"] else "??"
        energy = f"{float(row['energy']):g}" if row["energy"] not in ("", None) else "?"
        lines.append(
            f"{index:>3}. {row['artist']} - {row['title']}  [{bpm} BPM | {key} | E{energy}]"
        )
    total = sum(int(t.get("duration") or 0) for t in tracks)
    lines.append("")
    lines.append(f"{len(tracks)} tracks | {total // 60} min")
    return ("\n".join(lines) + "\n").encode("utf-8")
