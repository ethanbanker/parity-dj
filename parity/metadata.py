"""Editing a track's metadata, and writing it to the file.

Parity's library is a copy of what rekordbox has, so editing a title here is
only useful if it reaches the places the DJ will see it: the library record in
Parity, and -- when asked -- the file's own tags, which is what rekordbox,
Serato, Traktor and every other app read when they reload or import the track.
The library record is the caller's business; this module owns the file.

Writing is done by ffmpeg instead of a tag library: ffmpeg is already the one
external tool the app uses, and ``-c copy`` re-muxes the audio without decoding
or re-encoding it, so the samples are byte-for-byte what they were.  The write
is atomic: ffmpeg writes a sibling temp file and it replaces the original only
when the command succeeded.  A container that cannot hold a field (WAV has no
standard comment in the INFO chunk, AIFF carries tags in an ID3 chunk) keeps
whatever it can rather than failing the whole edit.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from . import audio, camelot

log = logging.getLogger("parity.metadata")

#: The fields a track edit may carry, in the order the editor shows them.
#: The right-hand side is the key the stored library record uses.
FIELDS: dict[str, str] = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "genre": "genre",
    "comment": "comments",
    "year": "year",
    "track_number": "track_number",
    "bpm": "bpm",
    "key": "tonality",
}

#: Accepted spellings from the UI / API -> the canonical field name above.
ALIASES = {
    "comments": "comment",
    "tonality": "key",
    "track": "track_number",
}

#: The name ffmpeg's metadata muxer uses for each field.
TAG_NAMES = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "genre": "genre",
    "comment": "comment",
    "year": "date",
    "track_number": "track",
    "bpm": "bpm",
    "key": "key",
}

STRING_LIMITS = {
    "title": 512,
    "artist": 512,
    "album": 512,
    "genre": 255,
    "comment": 255,
}

#: The containers whose muxer only writes ID3 when it is asked to.
ID3_CONTAINERS = {".aiff", ".aif", ".aifc", ".wav"}


class MetadataError(ValueError):
    """The edit, or the file, cannot be written as asked."""


def _text(value: object, limit: int = 255) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) > limit:
        raise MetadataError(f"That value is longer than {limit} characters.")
    return text


def normalise(fields: dict | None) -> dict:
    """Validate an edit and return it keyed like a stored library record.

    Unknown fields are refused rather than silently ignored: a typo in a field
    name is better reported than accepted as a no-op.  A blank value clears the
    field, which is a real edit -- ``None``/missing means "leave it alone".
    """
    values = dict(fields or {})
    if not values:
        raise MetadataError("No fields to change.")
    cleaned: dict[str, object] = {}
    for raw_name, value in values.items():
        name = ALIASES.get(str(raw_name), str(raw_name))
        if name not in FIELDS:
            raise MetadataError(f"Unknown field: {raw_name}")
        record_field = FIELDS[name]
        if value is None:
            cleaned[record_field] = ""
        elif name in STRING_LIMITS:
            cleaned[record_field] = _text(value, STRING_LIMITS[name])
        elif name == "year":
            try:
                year = int(float(value or 0))
            except (TypeError, ValueError) as exc:
                raise MetadataError(f"Year must be a number, not {value!r}.") from exc
            if year < 0 or year > 9999:
                raise MetadataError("Year must be between 0 and 9999.")
            cleaned[record_field] = year
        elif name == "track_number":
            try:
                number = int(float(value or 0))
            except (TypeError, ValueError) as exc:
                raise MetadataError(f"Track number must be a number, not {value!r}.") from exc
            if number < 0:
                raise MetadataError("Track number cannot be negative.")
            cleaned[record_field] = number
        elif name == "bpm":
            try:
                bpm = float(value or 0)
            except (TypeError, ValueError) as exc:
                raise MetadataError(f"BPM must be a number, not {value!r}.") from exc
            if bpm < 0 or bpm > 400:
                raise MetadataError("BPM must be between 0 and 400.")
            cleaned[record_field] = round(bpm, 3)
        else:  # key
            text = _text(value, 32)
            code = camelot.parse_key(text)
            # Store the musical name when the edit arrived as Camelot, so the
            # library reads the same whichever notation the user typed; a key
            # the wheel cannot parse is still kept as text rather than lost.
            cleaned[record_field] = camelot.CAMELOT_TO_KEY.get(code, text) if code else text
    return cleaned


def tag_values(fields: dict) -> dict[str, str]:
    """The ffmpeg ``-metadata`` pairs for a normalised edit."""
    values: dict[str, str] = {}
    for name, record_field in FIELDS.items():
        if record_field not in fields:
            continue
        value = fields[record_field]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        text = str(value)
        if not text:
            continue
        tag = TAG_NAMES[name]
        if name == "key":
            # ID3's TKEY expects a musical key; a Camelot code is meaningless to
            # the other apps that read the field.
            code = camelot.parse_key(text)
            text = camelot.CAMELOT_TO_KEY.get(code, text) if code else text
        values[tag] = text
    return values


def command(input_path: Path, output_path: Path, values: dict[str, str]) -> list[str]:
    """The ffmpeg command that copies the audio and rewrites its tags."""
    args = [
        audio.ffmpeg_path(),
        "-nostdin",
        "-v", "error",
        "-y",
        "-i", str(input_path),
        "-map", "0",
        "-map_metadata", "0",
        "-c", "copy",
    ]
    if input_path.suffix.lower() in ID3_CONTAINERS:
        # The AIFF and WAV muxers write a bare LIST/NAME chunk by default and
        # would drop artist and genre; the ID3 chunk is what rekordbox reads.
        args += ["-write_id3v2", "1", "-id3v2_version", "3"]
    for tag, value in sorted(values.items()):
        args += ["-metadata", f"{tag}={value}"]
    args.append(str(output_path))
    return args


def write_tags(path: str | Path, fields: dict, *, keep_stat: bool = True) -> dict:
    """Write a normalised edit into the file's tags, atomically.

    Returns the tags that were written.  Nothing is written when the edit has no
    tag-worthy value; a file whose format ffmpeg cannot re-mux raises
    :class:`MetadataError` with ffmpeg's last line, before the original is
    touched.
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise MetadataError(f"No file at {target}")
    values = tag_values(fields)
    if not values:
        return {"written": {}, "file": str(target)}
    try:
        audio.ffmpeg_path()
    except audio.AudioUnavailable as exc:
        raise MetadataError(str(exc)) from exc

    temp = target.with_name(f".parity-tags-{os.getpid()}{target.suffix}")
    try:
        process = subprocess.run(command(target, temp, values), capture_output=True)
        if process.returncode != 0:
            detail = (process.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise MetadataError(
                f"ffmpeg could not write the tags of {target.name}: "
                + (detail[-1] if detail else f"exit {process.returncode}")
            )
        if keep_stat:
            try:
                shutil.copystat(target, temp)
            except OSError:
                pass
        temp.replace(target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:  # pragma: no cover - a temp file already gone
                pass
    # Read back what the container actually kept.  WAV's INFO chunk, for one,
    # has no slot for BPM or key, and saying "written" about a tag that did not
    # survive would send the user looking for it in rekordbox forever.
    landed: dict[str, str] = {}
    missing: list[str] = []
    try:
        from .ingest import probe  # noqa: PLC0415 - only needed on the write path

        landed = probe(target).get("tags") or {}
        missing = [tag for tag in values if tag not in landed]
    except Exception as exc:  # noqa: BLE001 - verification is a nicety, not the write
        log.debug("could not verify the tags of %s: %s", target.name, exc)
    log.info(
        "tags for %s: %s%s",
        target.name,
        ", ".join(f"{tag}={value!r}" for tag, value in sorted(values.items())),
        f" (container dropped {', '.join(missing)})" if missing else "",
    )
    return {"written": values, "missing": missing, "file": str(target)}
