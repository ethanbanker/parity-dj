"""Getting a folder of downloads into a library rekordbox will read.

Two problems, one screen:

* **The names.**  A download arrives named for the page it came from --
  ``01_Ada_Kaleh_-_Night_Ferry_🎵_[Official_Video]_[FREE_DL]_320kbps.mp3`` -- and
  a CDJ shows that in a one-line display, or refuses the characters outright on
  the exFAT stick the DJ actually carries.  :func:`sanitize` turns it into
  ``01 Ada Kaleh - Night Ferry.mp3`` and records every change with its reason, so
  the dry run can show *why* a name moved instead of just a new name.
* **The format.**  Converting MP3, M4A and the rest to AIFF or WAV, with the tags
  carried across -- the embedded cover art included, where the container can
  hold it: AIFF can (as an APIC frame), WAV cannot.  The point is not that MP3
  is unplayable, it is that an MP3
  carries encoder delay and end padding, so a cue point set against the MP3 and
  the same track as WAV do not sit at the same timecode.  Every file therefore
  reports how much silence it starts and ends with, so the offset is a number on
  the screen rather than a surprise in the booth.

Nothing here is destructive: files are read from the staging folder and written
to a destination folder, and the originals are left exactly as they were.  The
naming and the planning are pure standard library; only the conversion needs
ffmpeg, and a machine without it still gets the full name report and the plan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import audio

log = logging.getLogger("parity.ingest")

#: Both tools come from one ffmpeg install; ``PARITY_FFMPEG`` moves the pair.
ENV_FFMPEG = audio.ENV_FFMPEG
ENV_FFPROBE = "PARITY_FFPROBE"

#: What counts as a track when a folder is scanned.
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".mp2", ".mpga", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aif", ".aiff", ".aifc", ".wav", ".alac", ".wv", ".ape",
}


class IngestError(ValueError):
    """The caller asked for something impossible (a folder that is not there)."""


class IngestUnavailable(RuntimeError):
    """ffmpeg or ffprobe is missing, so the audio cannot be read or written."""


# ------------------------------------------------------------------- formats


@dataclass(frozen=True)
class TargetFormat:
    """One uncompressed format a set can be written in."""

    key: str
    label: str
    extension: str
    codec: str
    container: str
    #: Extra ffmpeg arguments this container needs to keep its tags.
    args: tuple[str, ...] = ()
    #: Whether the container can hold the cover art.  Artwork is an attached
    #: picture *stream*, not a tag, so a format that cannot take one loses it no
    #: matter what is mapped; WAV is such a format.
    carries_cover: bool = False

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "extension": self.extension}


TARGET_FORMATS: dict[str, TargetFormat] = {
    # The AIFF muxer writes a LIST/NAME chunk by default and drops artist, album
    # and genre; -write_id3v2 1 puts a proper ID3 chunk in, which is what
    # rekordbox reads back.  That same chunk is where the cover art lives, as an
    # APIC frame.  Measured, not assumed -- see tests/test_ingest.py.
    "aiff": TargetFormat(
        "aiff", "AIFF", ".aiff", "pcm_s16be", "aiff",
        ("-write_id3v2", "1", "-id3v2_version", "3"),
        carries_cover=True,
    ),
    "wav": TargetFormat("wav", "WAV", ".wav", "pcm_s16le", "wav"),
}
DEFAULT_FORMAT = "aiff"

#: Files in the target format do not need converting, only copying.
FORMAT_EXTENSIONS: dict[str, set[str]] = {
    "aiff": {".aif", ".aiff", ".aifc"},
    "wav": {".wav"},
}

#: Container bookkeeping that ffmpeg exposes as if it were a tag -- MP4's brand
#: fields, above all.  ``-map_metadata 0`` copies them, and they would land in
#: the new file as custom ID3 frames that say nothing about the music.
CONTAINER_TAGS = ("major_brand", "minor_version", "compatible_brands", "handler_name", "vendor_id")

#: Tags worth carrying and worth reporting on.  ffprobe reports far more; these
#: are the ones a DJ library is organised by.
KEPT_TAGS = (
    "artist", "album_artist", "title", "album", "genre", "track", "disc",
    "date", "comment", "composer", "bpm", "key", "label",
)


# ---------------------------------------------------------------------- tools


def ffmpeg_path() -> str:
    """The ffmpeg binary, or raise :class:`IngestUnavailable`."""
    try:
        return audio.ffmpeg_path()
    except audio.AudioUnavailable as exc:
        raise IngestUnavailable(str(exc)) from exc


def ffprobe_path() -> str:
    """The ffprobe binary -- it ships with ffmpeg, so look beside it too.

    Four places, as for ffmpeg: the environment variable, ``$PATH``, beside the
    ffmpeg we found (the same install, so the same version), then the standard
    install locations -- a Finder-launched app cannot see Homebrew's ``$PATH``,
    and that is exactly the case this is here for.
    """
    override = os.environ.get(ENV_FFPROBE)
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(override)
        if resolved:
            return resolved
        raise IngestUnavailable(
            f"{ENV_FFPROBE} is set to {override!r}, which is not a runnable file. "
            f"Unset it to let Parity search, or point it at the real one: "
            f"launchctl setenv {ENV_FFPROBE} /opt/homebrew/bin/ffprobe"
        )
    found = shutil.which("ffprobe")
    if found:
        return found
    try:
        beside = Path(ffmpeg_path()).with_name("ffprobe")
        if beside.is_file():
            return str(beside)
    except IngestUnavailable:
        # No ffmpeg either; the standard locations below are the last chance for
        # ffprobe on its own, and the error names both tools' fix.
        pass
    guessed = audio.find_tool("ffprobe")
    if guessed:
        log.info("ffprobe found at %s, which is not on this process's $PATH", guessed)
        return guessed
    raise IngestUnavailable(
        "ffprobe is not available, so a file's tags and length cannot be read. "
        + audio.tool_help("ffprobe", ENV_FFPROBE)
    )


def status() -> dict:
    """What this machine can do, in the shape the other status calls use."""
    ffmpeg = ffprobe = None
    error = None
    try:
        ffmpeg = ffmpeg_path()
    except IngestUnavailable as exc:
        error = str(exc)
    try:
        ffprobe = ffprobe_path()
    except IngestUnavailable as exc:
        error = error or str(exc)
    return {
        # Converting needs ffmpeg; reading tags needs ffprobe.  The tidy-up of
        # names needs neither, which is why this is not simply all-or-nothing.
        "available": bool(ffmpeg),
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "error": error,
        "formats": [fmt.to_dict() for fmt in TARGET_FORMATS.values()],
        "default_format": DEFAULT_FORMAT,
        "extensions": sorted(AUDIO_EXTENSIONS),
        "install_hint": audio.tool_help("ffmpeg", ENV_FFMPEG),
        "searched": list(audio.CANDIDATE_DIRS),
    }


def format_for(key: str | None) -> TargetFormat:
    wanted = (key or DEFAULT_FORMAT).strip().lower()
    if wanted not in TARGET_FORMATS:
        raise IngestError(
            f"Unknown format {key!r}. Choose one of: {', '.join(TARGET_FORMATS)}."
        )
    return TARGET_FORMATS[wanted]


# ---------------------------------------------------------------- sanitising

#: Characters no external drive should hold.  The Windows set is the one that
#: matters: the stick a DJ carries is exFAT or FAT32, whatever the laptop is.
ILLEGAL_TO_DASH = "/\\|"
ILLEGAL_TO_DROP = '*?"<>'
ILLEGAL_TO_SPACE = ":"

#: Windows device names, which are refused with any extension: ``CON.wav``.
RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

#: The whole filename (stem + extension) is capped so it fits every filesystem
#: a stick might use, with room to spare for the `` (2)`` a collision adds.
MAX_NAME_BYTES = 200

#: Bracketed groups are dropped when they say nothing about the music.  Every
#: pattern here was seen in a real downloads folder.
JUNK_IN_BRACKETS = re.compile(
    "|".join(
        (
            r"official\s*(?:video|audio|music\s+video|lyric\s+video|visuali[sz]er)",
            r"music\s+video", r"lyric\s+video", r"visuali[sz]er",
            r"free\s*(?:download|dl)", r"free\s*dl", r"download\s+link",
            r"out\s+now", r"no\s+copyright(?:\s+music)?", r"copyright\s+free",
            r"royalty\s+free", r"bass\s+boosted", r"full\s+hd",
            r"\b(?:hd|hq|4k|1080p|720p|480p)\b",
            r"\d{2,4}\s*kbps", r"\d{2,4}\s*kbit/s?", r"\d{2,4}\s*k(?![a-z0-9])",
            r"\b(?:mp3|wav|flac|m4a|aif{1,2})\b",
            r"promo(?:\s+copy)?", r"preview(?:\s+only)?",
            r"www\.\S+", r"https?://\S+", r"\.com|\.net|\.org|\.io\b",
            r"t\.me/\S+", r"telegram",
        )
    ),
    re.IGNORECASE,
)

#: Any bracketed group; whether it goes is decided by :data:`JUNK_IN_BRACKETS`.
_BRACKETS = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_EMPTY_BRACKETS = re.compile(r"[\(\[]\s*[\)\]]")

#: ``320kbps`` and friends outside brackets, where they are pure noise.
BITRATE = re.compile(r"\b\d{2,4}\s*(?:kbps|kbit/s|kbits?)\b|\b\d{2,4}\s*k(?![a-z0-9])", re.IGNORECASE)
#: A quality claim outside brackets (``Night Ferry HQ``).  A word boundary keeps
#: it off real titles: ``HDMI`` has a word character after ``HD``.
QUALITY = re.compile(r"\b(?:hd|hq|4k)\b", re.IGNORECASE)
LINKS = re.compile(
    r"https?://\S+|www\.\S+|[\w.-]+\.(?:com|net|org|io|co|link)/\S*"
    r"|\bt\.me/\S+|@[A-Za-z0-9_]{3,}",
    re.IGNORECASE,
)

#: ``%20`` and the other URL escapes a downloader leaves behind.
ESCAPES = re.compile(r"%[0-9a-fA-F]{2}")

#: A hyphen with space on either side is a separator: normalise it to `` - ``.
#: A hyphen with no space (``AC-DC``, ``lo-fi``) is part of the name, so it stays.
SEPARATOR = re.compile(r"\s+-\s*|\s*-\s+")
DASH_RUN = re.compile(r"\s*[-–—]{2,}\s*")
WHITESPACE = re.compile(r"[\s\u00a0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000]+")

#: Combining marks that only decorate symbols (keycaps, variation selectors).
VARIATION_MARKS = ((0x20D0, 0x20FF), (0xFE00, 0xFE0F), (0xE0100, 0xE01EF))


@dataclass
class Change:
    """One edit to a filename, and why it was made."""

    kind: str   # "illegal" | "emoji" | "format" | "reserved" | "length" | "collision"
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail}


def _is_variation_mark(char: str) -> bool:
    point = ord(char)
    return any(low <= point <= high for low, high in VARIATION_MARKS)


def strip_symbols(text: str) -> tuple[str, int]:
    """Drop emoji, dingbats, private-use and invisible characters.

    Letters are kept whatever their alphabet -- a Greek or Korean title is a
    title -- but the Unicode categories that carry no name to read (``So`` for
    emoji and dingbats, ``Co``/``Cn`` for private and unassigned, ``Cc``/``Cf``
    for control and invisible formatting) are removed, because a CDJ shows them
    as an empty box or drops the whole name.
    """
    kept: list[str] = []
    removed = 0
    for char in text:
        if _is_variation_mark(char) or unicodedata.category(char) in {"So", "Co", "Cn", "Cs", "Cc", "Cf"}:
            removed += 1
            continue
        kept.append(char)
    return "".join(kept), removed


def _drop_junk_brackets(text: str, changes: list[Change]) -> str:
    dropped: list[str] = []

    def replace(match: re.Match) -> str:
        inner = match.group(1).strip()
        if not inner:
            # Left empty by an earlier step ("[320kbps]"); the empty-bracket
            # pass tidies it up without a second note.
            return " "
        if JUNK_IN_BRACKETS.search(inner) or not any(char.isalnum() for char in inner):
            dropped.append(match.group(0))
            return " "
        return match.group(0)

    text = _BRACKETS.sub(replace, text)
    text = _EMPTY_BRACKETS.sub(" ", text)
    if dropped:
        shown = ", ".join(f"“{item.strip()}”" for item in dropped[:4])
        more = f" and {len(dropped) - 4} more" if len(dropped) > 4 else ""
        changes.append(Change("format", f"Removed the tag{'' if len(dropped) == 1 else 's'} {shown}{more}"))
    return text


def _fit(stem: str, extension: str, changes: list[Change]) -> str:
    """Trim the stem so the whole filename fits :data:`MAX_NAME_BYTES`."""
    budget = MAX_NAME_BYTES - len(extension.encode("utf-8"))
    if len(stem.encode("utf-8")) <= budget:
        return stem
    kept: list[str] = []
    used = 0
    for char in stem:
        width = len(char.encode("utf-8"))
        if used + width > budget:
            break
        kept.append(char)
        used += width
    trimmed = "".join(kept).rstrip(" .-_")
    if not trimmed:
        trimmed = "untitled"
    changes.append(
        Change("length", f"Shortened to {len(trimmed)} characters so the name fits a 255-byte limit")
    )
    return trimmed


def sanitize(stem: str, extension: str = "") -> tuple[str, list[Change]]:
    """Tidy one filename stem for a CDJ and a USB stick.

    Returns the new stem and every change made.  ``extension`` is only used to
    keep the finished filename inside :data:`MAX_NAME_BYTES`.  The function never
    returns an empty name and never raises: an unreadable download still has to
    become *something* the DJ can see and rename by hand.
    """
    changes: list[Change] = []
    original = stem

    text = unicodedata.normalize("NFKC", stem)
    if ESCAPES.search(text):
        decoded = re.sub(
            r"%([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text
        )
        decoded, _ = strip_symbols(decoded)
        if decoded != text:
            changes.append(Change("format", "Decoded %20-style web escapes"))
            text = decoded

    # Separators come out first: "Night_Ferry_[FREE_DL]_320kbps" only reads as
    # junk once the underscores are out of the way.
    if "_" in text:
        text = text.replace("_", " ")
        changes.append(Change("format", "Underscores became spaces"))
    text = WHITESPACE.sub(" ", text)

    found_links = LINKS.findall(text)
    if found_links:
        text = LINKS.sub(" ", text)
        changes.append(Change("format", f"Removed the web address “{found_links[0][:40]}”"))
    if BITRATE.search(text):
        text = BITRATE.sub(" ", text)
        changes.append(Change("format", "Removed the bitrate suffix"))
    quality = QUALITY.search(text)
    if quality:
        text = QUALITY.sub(" ", text)
        changes.append(Change("format", f"Removed the quality tag “{quality.group(0).strip()}”"))

    text = _drop_junk_brackets(text, changes)

    if ILLEGAL_TO_SPACE in text:
        text = text.replace(ILLEGAL_TO_SPACE, "-")
        changes.append(Change("illegal", "“:” is not allowed on exFAT or Windows and became “-”"))
    hits = [char for char in ILLEGAL_TO_DASH if char in text]
    if hits:
        for char in hits:
            text = text.replace(char, "-")
        shown = "”, “".join(sorted(set(hits)))
        verb = "is a path separator" if len(set(hits)) == 1 else "are path separators"
        changes.append(Change("illegal", f"“{shown}” {verb} and became “-”"))
    dropped_illegal = sorted({char for char in ILLEGAL_TO_DROP if char in text})
    if dropped_illegal:
        for char in dropped_illegal:
            text = text.replace(char, "")
        changes.append(
            Change("illegal", f"Removed “{'”, “'.join(dropped_illegal)}”, which no filesystem will take")
        )

    stripped, removed = strip_symbols(text)
    if removed:
        text = stripped
        changes.append(
            Change("emoji", f"Removed {removed} emoji or symbol character{'' if removed == 1 else 's'}")
        )

    text = WHITESPACE.sub(" ", text).strip()

    tightened = DASH_RUN.sub(" - ", text)
    tightened = SEPARATOR.sub(" - ", tightened)
    tightened = re.sub(r"(?:\s*-\s*){2,}", " - ", tightened).strip(" -")
    if tightened != text:
        changes.append(Change("format", "Tightened the separators"))
        text = tightened

    tidy = re.sub(r"\.{2,}", ".", text).strip(" .-")
    if tidy != text:
        changes.append(Change("format", "Removed a stray dot or trailing separator"))
        text = tidy

    if not text:
        changes.append(Change("format", "Nothing readable was left in the name"))
        return "untitled", changes

    head = text.split(".")[0].lower()
    if head in RESERVED_NAMES:
        text = f"{text}_"
        changes.append(
            Change("reserved", f"“{head.upper()}” is a reserved device name on Windows and exFAT")
        )

    text = _fit(text, extension, changes)
    if text == original:
        # Every step ran clean, or a later step put back what an earlier one
        # took out; either way the name did not move, so there is nothing to
        # explain to the user.
        return text, []
    return text, changes


def unique_name(name: str, taken: set[str]) -> tuple[str, Change | None]:
    """``name``, or the same name with `` (2)`` when it is already spoken for."""
    if name.lower() not in taken:
        taken.add(name.lower())
        return name, None
    stem = Path(name).stem
    extension = Path(name).suffix
    index = 2
    while f"{stem} ({index}){extension}".lower() in taken:
        index += 1
    candidate = f"{stem} ({index}){extension}"
    taken.add(candidate.lower())
    return candidate, Change("collision", f"{name} is already used here, so this one became {candidate}")


# ------------------------------------------------------------- reading files


def probe(path: Path) -> dict:
    """ffprobe one file: codec, length, channels and the tags it carries."""
    command = [
        ffprobe_path(),
        "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name,sample_rate,channels:format=duration:format_tags",
        "-of", "json",
        str(path),
    ]
    process = subprocess.run(command, capture_output=True, timeout=120)
    if process.returncode != 0:
        detail = (process.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise IngestUnavailable(
            f"ffprobe could not read {path.name}: " + (detail[-1] if detail else f"exit {process.returncode}")
        )
    try:
        data = json.loads(process.stdout or b"{}")
    except json.JSONDecodeError as exc:
        raise IngestUnavailable(f"ffprobe returned something unreadable for {path.name}") from exc

    streams = data.get("streams") or []
    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), {})
    tags = {
        key.lower(): value
        for key, value in ((data.get("format") or {}).get("tags") or {}).items()
        if key.lower() in KEPT_TAGS and value
    }
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration": round(duration, 3),
        "sample_rate": int(audio_stream.get("sample_rate") or 0),
        "channels": int(audio_stream.get("channels") or 0),
        "codec": audio_stream.get("codec_name") or "",
        "tags": tags,
        # An attached picture is a video stream in the container, so this is how
        # the run knows artwork was there to lose -- and, read back off the
        # written file, whether it made it across.
        "cover": any(item.get("codec_type") == "video" for item in streams),
    }


#: A file is read as "silent" below this, for at least this long.
SILENCE_THRESHOLD_DB = -50.0
MIN_SILENCE_SECONDS = 0.15
#: How far into the file a scan listens for the leading silence it reports.
SCAN_WINDOW_SECONDS = 20.0

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def parse_silence(stderr: str, duration: float) -> dict:
    """Turn silencedetect's log lines into leading and trailing offsets.

    Separate from the ffmpeg call so the arithmetic is testable on its own: the
    decoder is the one part of this that cannot be relied on to be installed.
    """
    starts = [float(value) for value in _SILENCE_START.findall(stderr or "")]
    ends = [float(value) for value in _SILENCE_END.findall(stderr or "")]

    leading = 0.0
    if starts and ends and starts[0] <= 0.05:
        leading = min(ends[0], duration) if duration else ends[0]

    trailing = 0.0
    if starts and len(starts) > len(ends):
        # An unclosed silence at the end runs to the last sample.
        trailing = max(0.0, (duration or starts[-1]) - starts[-1])
    elif starts and ends and duration and ends[-1] >= duration - 0.05:
        trailing = max(0.0, duration - starts[-1])

    # A file that is silent throughout reports one pair covering all of it; that
    # is leading silence, not leading plus trailing.
    if leading + trailing > duration > 0:
        trailing = max(0.0, duration - leading)
    return {
        "leading_ms": round(leading * 1000, 1),
        "trailing_ms": round(trailing * 1000, 1),
        "threshold_db": SILENCE_THRESHOLD_DB,
        "min_silence_s": MIN_SILENCE_SECONDS,
    }


def padding(path: Path, *, window: float | None = SCAN_WINDOW_SECONDS) -> dict:
    """How much silence a file starts (and, when the whole file is read, ends) with.

    ``window`` bounds the decode: a scan of a folder only needs the first seconds
    to find the leading offset, while a conversion is reading the whole file
    anyway and gets both ends for free.
    """
    command = [
        ffmpeg_path(),
        "-nostdin", "-hide_banner", "-v", "info",
    ]
    if window:
        command += ["-t", f"{max(1.0, float(window)):.3f}"]
    command += [
        "-i", str(path),
        "-af", f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={MIN_SILENCE_SECONDS}",
        "-f", "null", "-",
    ]
    process = subprocess.run(command, capture_output=True, timeout=300)
    if process.returncode != 0:
        detail = (process.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise IngestUnavailable(
            f"ffmpeg could not read {path.name}: " + (detail[-1] if detail else f"exit {process.returncode}")
        )
    stderr = (process.stderr or b"").decode("utf-8", "replace")
    duration = _stderr_duration(stderr, window)
    result = parse_silence(stderr, duration)
    # A file shorter than the window was read from end to end after all, so its
    # trailing silence is a measurement rather than a guess.
    result["complete"] = window is None or (duration > 0 and duration <= float(window) + 0.05)
    result["window_s"] = None if result["complete"] else round(float(window), 1)
    return result


def _stderr_duration(stderr: str, window: float | None) -> float:
    """The file's own length, from the input dump ffmpeg prints."""
    match = _DURATION.search(stderr or "")
    seconds = 0.0
    if match:
        seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    if window:
        # Only the window was decoded, so no silence can be measured past it.
        return min(seconds, float(window)) if seconds else float(window)
    return seconds


# -------------------------------------------------------------- the pipeline


@dataclass
class Item:
    """One file's place in the plan."""

    source: Path
    relative: Path          # where it sits under the staging folder
    name: str               # the name it should have
    target: Path | None     # where it will land
    changes: list[Change] = field(default_factory=list)
    size: int = 0
    converts: bool = False
    status: str = "ready"   # ready | exists | skipped | in place
    reason: str = ""
    note: str = ""
    probe: dict | None = None
    padding: dict | None = None

    @property
    def name_changes(self) -> bool:
        """Whether the written file is named anything other than the original.

        A conversion always changes the extension, which is not a rename the user
        has to read about -- it is what they asked for.  :attr:`renames` is the
        narrower question, and is what the report counts.
        """
        return self.name != self.source.name

    @property
    def renames(self) -> bool:
        return Path(self.name).stem != self.source.stem

    @property
    def action(self) -> str:
        if self.status != "ready":
            return self.status
        if self.converts and self.renames:
            return "convert and rename"
        if self.converts:
            return "convert"
        if self.renames:
            return "rename and copy"
        return "copy"

    def to_dict(self) -> dict:
        return {
            "source": str(self.source),
            "relative": str(self.relative),
            "name": self.name,
            "target": str(self.target) if self.target else None,
            "action": self.action,
            "status": self.status,
            "reason": self.reason,
            "note": self.note,
            "size": self.size,
            "renames": self.renames,
            "name_changes": self.name_changes,
            "converts": self.converts,
            "changes": [change.to_dict() for change in self.changes],
            "probe": self.probe,
            "padding": self.padding,
        }


@dataclass
class Plan:
    """What a scan found, and what it would do about it."""

    root: Path
    destination: Path
    format: str
    items: list[Item] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    recursive: bool = True
    measured: bool = False

    @property
    def actionable(self) -> list[Item]:
        return [item for item in self.items if item.status in {"ready", "exists"}]

    def to_dict(self) -> dict:
        by_source: dict[str, int] = {}
        for item in self.items:
            suffix = item.source.suffix.lower().lstrip(".") or "other"
            if item.converts:
                by_source[suffix] = by_source.get(suffix, 0) + 1
        return {
            "root": str(self.root),
            "destination": str(self.destination),
            "format": self.format,
            "recursive": self.recursive,
            "measured": self.measured,
            "items": [item.to_dict() for item in self.items],
            "notes": self.notes,
            "summary": {
                "found": len(self.items),
                "renames": sum(1 for item in self.items if item.renames),
                "converts": sum(1 for item in self.items if item.converts),
                "copies": sum(1 for item in self.items if not item.converts),
                "ready": sum(1 for item in self.items if item.status == "ready"),
                "exists": sum(1 for item in self.items if item.status == "exists"),
                "skipped": sum(1 for item in self.items if item.status == "skipped"),
                "silence": sum(
                    1 for item in self.items if (item.padding or {}).get("leading_ms")
                ),
                "bytes": sum(item.size for item in self.items),
                "by_source_format": dict(sorted(by_source.items())),
            },
        }


def _destination_for(root: Path, destination: str | Path | None) -> Path:
    """Where tidied files go: the chosen folder, or ``<root>/converted``."""
    if destination in (None, ""):
        return (root / "converted").resolve()
    return Path(str(destination)).expanduser().resolve()


def scan(
    root: str | Path,
    destination: str | Path | None = None,
    format: str = DEFAULT_FORMAT,
    *,
    recursive: bool = True,
    measure: bool = True,
    overwrite: bool = False,
) -> Plan:
    """Read a staging folder and work out what each file needs.

    Nothing is written and nothing is moved: the returned plan is what the UI
    shows as a dry run.  With ``measure`` the leading silence of each file is
    read too, which is the number that explains why a cue point moves when an
    MP3 becomes a WAV.
    """
    if not str(root or "").strip():
        raise IngestError("No folder chosen.")
    folder = Path(str(root)).expanduser()
    if not folder.is_dir():
        raise IngestError(f"There is no folder at {folder}")
    folder = folder.resolve()
    target_format = format_for(format)
    destination_path = _destination_for(folder, destination)

    plan = Plan(root=folder, destination=destination_path, format=target_format.key, recursive=recursive)

    try:
        walker = sorted(folder.rglob("*")) if recursive else sorted(folder.iterdir())
    except OSError as exc:
        raise IngestError(f"{folder} could not be read: {exc}") from exc

    taken: dict[Path, set[str]] = {}
    ignored = 0
    for path in walker:
        if not path.is_file():
            continue
        resolved = path.resolve()
        # Never treat our own output as something to ingest.
        if resolved == destination_path or destination_path in resolved.parents:
            continue
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            ignored += 1
            continue

        relative = path.relative_to(folder)
        suffix = path.suffix.lower()
        # A converted file takes the target format's extension: the data inside
        # is AIFF even if ``.mp3`` is what it arrived as, and a mismatched
        # extension is exactly what confuses a CDJ.
        converts = suffix not in FORMAT_EXTENSIONS[target_format.key]
        wanted = target_format.extension if converts else suffix
        stem, changes = sanitize(path.stem, wanted)
        candidate = f"{stem}{wanted}"
        seen = taken.setdefault(relative.parent, set())
        name, collision = unique_name(candidate, seen)
        if collision:
            changes.append(collision)

        try:
            size = path.stat().st_size
        except OSError:
            plan.notes.append(f"{relative} could not be read and was skipped")
            continue

        item = Item(
            source=path,
            relative=relative,
            name=name,
            target=destination_path / relative.parent / name,
            changes=changes,
            size=size,
            converts=converts,
        )
        if item.target.resolve() == resolved:
            item.status = "skipped"
            item.reason = "already in place"
        elif item.target.exists() and not overwrite:
            item.status = "exists"
            item.reason = "a file of this name is already in the destination"
        plan.items.append(item)

    if ignored:
        plan.notes.append(
            f"{ignored} other file{'' if ignored == 1 else 's'} in the folder "
            f"{'is' if ignored == 1 else 'are'} not audio and {'was' if ignored == 1 else 'were'} "
            "left alone"
        )
    if plan.items:
        plan.notes.append(
            f"Everything is written to {destination_path}; the originals are never changed."
        )

    if measure and plan.items:
        _measure(plan)
    return plan


def _measure(plan: Plan) -> None:
    """Best-effort: read the length, tags and leading silence of each file.

    A missing ffprobe or an unreadable file leaves a note on that item rather
    than failing the whole scan -- the names are still worth having.
    """
    plan.measured = True
    for item in plan.items:
        try:
            item.probe = probe(item.source)
        except (IngestUnavailable, subprocess.TimeoutExpired, OSError) as exc:
            item.note = (item.note + " " + str(exc)).strip()
        try:
            item.padding = padding(item.source)
        except (IngestUnavailable, subprocess.TimeoutExpired, OSError) as exc:
            item.note = (item.note + " " + str(exc)).strip()


def convert(
    source: str | Path,
    target: str | Path,
    format: str = DEFAULT_FORMAT,
    *,
    overwrite: bool = False,
    trim_leading_ms: float = 0.0,
) -> dict:
    """Convert one file, measuring its silence on the way through.

    The output is written beside its final name and moved into place only once
    ffmpeg has succeeded, so an interrupted conversion cannot leave a half-file
    that looks like a finished one.
    """
    source = Path(source)
    target = Path(target)
    target_format = format_for(format)
    if target.exists() and not overwrite:
        return {"ok": False, "skipped": "exists", "target": str(target), "reason": "already there"}

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    command = [ffmpeg_path(), "-nostdin", "-hide_banner", "-v", "info", "-y"]
    trimmed = 0.0
    if trim_leading_ms and trim_leading_ms > 0:
        trimmed = float(trim_leading_ms) / 1000.0
        command += ["-ss", f"{trimmed:.3f}"]
    command += [
        "-i", str(source),
        "-map_metadata", "0",
        # Setting a container field to nothing is how ffmpeg drops a tag, and
        # these are the ones an MP4 hands over that no player wants to read.
        *[arg for key in CONTAINER_TAGS for arg in ("-metadata", f"{key}=")],
        "-map", "0:a:0",
    ]
    if target_format.carries_cover:
        # Cover art is a stream of its own, so mapping the audio alone is what
        # drops it -- -map_metadata cannot bring it back, it only moves tags.
        # The "?" makes the map optional, so a file with no artwork still
        # converts; the picture is copied rather than re-encoded, because it is
        # already a JPEG or a PNG and re-encoding it would only cost quality.
        command += ["-map", "0:v:0?", "-c:v", "copy", "-disposition:v", "attached_pic"]
    command += [
        "-c:a", target_format.codec,
        *target_format.args,
        "-af", f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={MIN_SILENCE_SECONDS}",
        "-f", target_format.container,
        str(temporary),
    ]

    started = time.perf_counter()
    try:
        process = subprocess.run(command, capture_output=True, timeout=3600)
    except subprocess.TimeoutExpired:
        temporary.unlink(missing_ok=True)
        return {"ok": False, "skipped": "timeout", "target": str(target), "reason": "ffmpeg did not finish"}
    elapsed = time.perf_counter() - started
    stderr = (process.stderr or b"").decode("utf-8", "replace")

    if process.returncode != 0 or not temporary.exists():
        temporary.unlink(missing_ok=True)
        detail = [line for line in stderr.strip().splitlines() if line.strip()]
        return {
            "ok": False,
            "skipped": "failed",
            "target": str(target),
            "reason": detail[-1] if detail else f"ffmpeg exited {process.returncode}",
        }

    duration = _stderr_duration(stderr, None)
    measured = parse_silence(stderr, duration)
    measured["complete"] = True
    measured["window_s"] = None
    measured["trimmed_ms"] = round(trimmed * 1000, 1)
    try:
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        return {"ok": False, "skipped": "failed", "target": str(target), "reason": f"could not be moved into place: {exc}"}

    result = {
        "ok": True,
        "target": str(target),
        "bytes": target.stat().st_size,
        "seconds": round(duration, 3),
        "elapsed": round(elapsed, 2),
        # The number the whole feature exists for: how far into the converted
        # file the music actually starts.
        "padding": measured,
        "trimmed_ms": measured["trimmed_ms"],
        "source": str(source),
    }
    try:
        # Read the tags back out rather than trusting that the copy worked: this
        # is the check that catches a container quietly dropping artist/album.
        read_back = probe(target)
        result["tags"] = sorted(read_back["tags"])
        # Same for the artwork: what matters is whether it landed in the written
        # file, not whether it was asked for.
        result["cover"] = bool(read_back["cover"])
    except (IngestUnavailable, subprocess.TimeoutExpired, OSError) as exc:
        result["tags"] = {"error": str(exc)}
    return result


def copy_file(source: str | Path, target: str | Path, *, overwrite: bool = False) -> dict:
    """A file already in the target format: same name tidied, same bytes."""
    source = Path(source)
    target = Path(target)
    if target.exists() and not overwrite:
        return {"ok": False, "skipped": "exists", "target": str(target), "reason": "already there"}
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        return {"ok": False, "skipped": "failed", "target": str(target), "reason": str(exc)}
    return {"ok": True, "target": str(target), "bytes": target.stat().st_size, "source": str(source)}


def run(
    plan: Plan,
    *,
    dry_run: bool = True,
    overwrite: bool = False,
    trim_silence: bool = False,
    limit: int = 0,
) -> dict:
    """Act on a plan: convert or copy each file, collecting a report.

    A dry run walks the same list and returns the same shape with nothing
    written, which is what lets the UI show the report before the commit and
    then re-render it afterwards without a second layout.
    """
    started = time.perf_counter()
    report: dict = {
        "dry_run": dry_run,
        "root": str(plan.root),
        "destination": str(plan.destination),
        "format": plan.format,
        "written": [],
        "copied": [],
        "skipped": [],
        "failed": [],
        "trimmed": [],
        "bytes": 0,
    }
    if dry_run:
        for item in plan.items:
            row = {
                "source": str(item.source),
                "target": str(item.target) if item.target else None,
                "name": item.name,
                "was": item.source.name,
                "action": item.action,
                "converts": item.converts,
                "reason": item.reason,
                "changes": [change.to_dict() for change in item.changes],
                "padding": item.padding,
            }
            if item.status in {"skipped", "exists"}:
                report["skipped"].append(row)
            elif item.converts:
                report["written"].append(row)
            else:
                report["copied"].append(row)
        report["elapsed"] = round(time.perf_counter() - started, 2)
        report["summary"] = _summary(report, plan)
        return report

    handled = 0
    for item in plan.items:
        if limit > 0 and handled >= limit:
            break
        if item.target is None or item.status == "skipped":
            report["skipped"].append(
                {"source": str(item.source), "target": None, "reason": item.reason or "nothing to do"}
            )
            continue
        if item.status == "exists" and not overwrite:
            report["skipped"].append(
                {"source": str(item.source), "target": str(item.target), "reason": item.reason}
            )
            continue

        try:
            if item.converts:
                leading = float((item.padding or {}).get("leading_ms") or 0.0)
                result = convert(
                    item.source,
                    item.target,
                    plan.format,
                    overwrite=overwrite,
                    trim_leading_ms=leading if trim_silence else 0.0,
                )
            else:
                result = copy_file(item.source, item.target, overwrite=overwrite)
        except (IngestUnavailable, OSError, subprocess.SubprocessError) as exc:
            # One unreadable file must not abandon the other fifty.
            result = {"ok": False, "skipped": "failed", "target": str(item.target), "reason": str(exc)}

        if result.get("ok"):
            row = {
                "source": str(item.source),
                "target": result["target"],
                "name": item.name,
                "was": item.source.name,
                "action": item.action,
                "bytes": result.get("bytes", 0),
                "elapsed": result.get("elapsed"),
                "padding": result.get("padding"),
                "trimmed_ms": result.get("trimmed_ms", 0.0),
                "changes": [change.to_dict() for change in item.changes],
                "tags": _tag_report(item, result),
                "cover_dropped": _cover_dropped(item, result, plan.format),
            }
            report["written" if item.converts else "copied"].append(row)
            report["bytes"] += row["bytes"]
            handled += 1
            if row["trimmed_ms"]:
                report["trimmed"].append({"target": row["target"], "ms": row["trimmed_ms"]})
            log.info(
                "%s -> %s (%s, %.1f MB)%s",
                item.source.name,
                item.name,
                item.action,
                row["bytes"] / 1_000_000,
                f", trimmed {row['trimmed_ms']:.0f} ms of leading silence" if row["trimmed_ms"] else "",
            )
        elif result.get("skipped") == "exists":
            report["skipped"].append(
                {"source": str(item.source), "target": result["target"], "reason": result.get("reason", "already there")}
            )
        else:
            report["failed"].append(
                {
                    "source": str(item.source),
                    "target": result.get("target"),
                    "reason": result.get("reason", "failed"),
                }
            )
            log.warning("%s could not be written: %s", item.source.name, result.get("reason"))

    report["elapsed"] = round(time.perf_counter() - started, 2)
    report["summary"] = _summary(report, plan)
    return report


def _cover_dropped(item: Item, result: dict, format: str) -> bool:
    """Whether artwork the source carried is not in the written file.

    Only a conversion can lose it -- a copy is the same bytes -- and a format
    that cannot hold a picture loses it whatever is mapped, so a covered MP3 sent
    to WAV says so while the same file sent to AIFF stays quiet.  A written file
    whose streams could not be read back makes no claim either way.
    """
    if not item.converts or not (item.probe or {}).get("cover"):
        return False
    if not format_for(format).carries_cover:
        return True
    return result.get("cover") is False


def _tag_report(item: Item, result: dict) -> dict:
    """What the source carried, what the written file carries, and what went missing."""
    written = result.get("tags")
    before = sorted((item.probe or {}).get("tags") or {})
    if not isinstance(written, list):
        return {"before": before, "after": [], "lost": before, "error": "the tags could not be read back"}
    return {"before": before, "after": written, "lost": sorted(set(before) - set(written))}


def _summary(report: dict, plan: Plan) -> dict:
    return {
        "converted": len(report["written"]),
        "copied": len(report["copied"]),
        "planned": len(report["written"]) + len(report["copied"]),
        "skipped": len(report["skipped"]),
        "failed": len(report["failed"]),
        "renamed": sum(1 for item in plan.items if item.renames),
        "bytes": report["bytes"],
        "folder": str(plan.destination),
        "format": plan.format,
    }
