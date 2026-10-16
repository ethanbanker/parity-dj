"""Reader for rekordbox ``collection.xml`` exports.

Rekordbox writes this file from *File > Export Collection in xml format*.  It
contains both the track metadata we need (BPM, key, rating, comments, genre)
and the playlist tree.

The parser is deliberately read-only and defensive: rekordbox files vary
between versions and contain plenty of empty or unexpected attributes -- and a
file handed to the app by someone else gets checked before it is parsed at all
(see :func:`check_xml_safe`).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

# Attributes copied straight through when we read a <TRACK> element.
def _int(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def location_to_path(location: str) -> str:
    """Turn ``file://localhost/Users/me/a%20b.mp3`` into a real filesystem path."""
    if not location:
        return ""
    if location.startswith("file://"):
        parsed = urlparse(location)
        path = unquote(parsed.path)
        # Windows style URLs arrive as file://localhost/C:/... -- drop the slash.
        if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    return location


def is_local_path(path: str) -> bool:
    r"""True for real filesystem paths (``/Users/...`` or ``C:\...``)."""
    if not path:
        return False
    if path.startswith("/"):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", path))


@dataclass
class Track:
    """One row of the rekordbox collection."""

    track_id: str
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    label: str = ""
    mix: str = ""
    comments: str = ""
    tonality: str = ""
    kind: str = ""
    location: str = ""
    bpm: float = 0.0
    duration: float = 0.0  # seconds
    rating: int = 0  # raw rekordbox value: 0..255 in steps of 51
    play_count: int = 0
    year: int = 0
    colour: str = ""
    track_number: int = 0
    date_added: str = ""
    # Only filled in for direct master.db imports (see :mod:`parity.rxdb`).
    analysed: bool = False
    analysis_path: str = ""  # absolute path of the ANLZ file if it exists
    cues: list[dict] = field(default_factory=list)  # existing cue/memory points

    @property
    def key(self) -> str:
        """Stable identity for persisting per-track user edits.

        The file path is the only field that survives a re-import, so it is
        preferred over rekordbox's ``TrackID``.
        """
        basis = self.path.lower() or f"trackid:{self.track_id}"
        return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:16]

    @property
    def path(self) -> str:
        return location_to_path(self.location)

    @property
    def folder(self) -> str:
        """Directory the track lives in, used for folder-scoped filtering."""
        path = self.path
        return os.path.dirname(path) if path else ""

    @property
    def extension(self) -> str:
        suffix = os.path.splitext(self.path)[1].lstrip(".")
        return suffix.upper()

    @property
    def is_streaming(self) -> bool:
        """True for tracks that live on a streaming service, not on disk.

        rekordbox writes streaming entries as a file URL whose host *is* the
        service, e.g. ``file://localhostsoundcloud:tracks:862894612``, so they
        have a location but no filesystem path.  Those tracks are in the
        collection export even though no file exists for them.
        """
        if not (self.location or "").strip():
            return False
        return not is_local_path(self.path)

    @property
    def stars(self) -> float:
        """Rekordbox rating as 0..5 stars."""
        return round(min(self.rating, 255) / 51.0, 2)

    @property
    def display(self) -> str:
        if self.artist:
            return f"{self.artist} - {self.title}"
        return self.title or self.path or f"Track {self.track_id}"

    def to_dict(self) -> dict:
        return {
            "id": self.track_id,
            "key": self.key,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "genre": self.genre,
            "label": self.label,
            "mix": self.mix,
            "comments": self.comments,
            "tonality": self.tonality,
            "kind": self.kind,
            "bpm": self.bpm,
            "duration": self.duration,
            "rating": self.rating,
            "stars": self.stars,
            "play_count": self.play_count,
            "year": self.year,
            "colour": self.colour,
            "path": self.path,
            "location": self.location,
            "streaming": self.is_streaming,
            "folder": self.folder,
            "analysed": self.analysed,
            "analysis_path": self.analysis_path,
            "cues": [dict(cue) for cue in self.cues],
        }


@dataclass
class Playlist:
    """A rekordbox playlist node. Folders are kept for the UI tree."""

    playlist_id: str
    name: str
    track_ids: list[str] = field(default_factory=list)
    children: list["Playlist"] = field(default_factory=list)

    @property
    def is_folder(self) -> bool:
        return not self.track_ids and bool(self.children)

    def total_tracks(self) -> int:
        """Track count including everything nested underneath."""
        return len(self.track_ids) + sum(child.total_tracks() for child in self.children)

    def to_dict(self) -> dict:
        return {
            "id": self.playlist_id,
            "name": self.name,
            "count": len(self.track_ids),
            "total": self.total_tracks(),
            "is_folder": self.is_folder,
            "track_ids": list(self.track_ids),
            "children": [child.to_dict() for child in self.children],
        }


@dataclass
class Library:
    tracks: dict[str, Track] = field(default_factory=dict)  # TrackID -> Track
    playlists: list[Playlist] = field(default_factory=list)
    product: dict = field(default_factory=dict)
    source: str = ""
    source_kind: str = "xml"  # "xml" or "db"

    def playlist(self, playlist_id: str) -> Playlist | None:
        def walk(nodes):
            for node in nodes:
                if node.playlist_id == playlist_id:
                    return node
                found = walk(node.children)
                if found:
                    return found
            return None

        return walk(self.playlists)

    def track_keys(self, playlist_id: str) -> list[str]:
        """Stable per-track keys for a playlist, folders included, deduplicated."""
        seen: set[str] = set()
        keys: list[str] = []
        for track_id in self.playlist_track_ids(playlist_id):
            track = self.tracks.get(track_id)
            if track is None or track.key in seen:
                continue
            seen.add(track.key)
            keys.append(track.key)
        return keys

    def playlist_track_ids(self, playlist_id: str) -> list[str]:
        """Track ids of a playlist, including its folder children (depth first)."""
        node = self.playlist(playlist_id)
        if node is None:
            return []
        collected: list[str] = []

        def walk(item: Playlist):
            collected.extend(item.track_ids)
            for child in item.children:
                walk(child)

        walk(node)
        return collected


#: A document type declaration is what a hostile XML file uses to make a parser
#: expand entities: a handful of nested definitions turn into gigabytes of text
#: ("billion laughs"), and the expansion happens inside the parser, so it cannot
#: be caught afterwards.  A rekordbox export has no DTD at all, so one is
#: refused rather than parsed.
XML_DECLARATION_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

XML_REFUSED = (
    "That XML declares entities (a DOCTYPE/ENTITY declaration), which a "
    "rekordbox export never does. Parity will not parse it: entity declarations "
    "are how a document makes a parser expand itself into gigabytes of text."
)


def check_xml_safe(xml_text: str) -> None:
    """Refuse an XML document that brings its own entity declarations.

    Raises :class:`ValueError`, which the API turns into a 400 with the message
    -- an import the user can read, rather than a parser that stops responding.
    """
    if XML_DECLARATION_RE.search(xml_text):
        raise ValueError(XML_REFUSED)


def parse_collection(xml_text: str | bytes, source: str = "") -> Library:
    """Parse rekordbox XML into a :class:`Library`."""
    if isinstance(xml_text, bytes):
        xml_text = xml_text.decode("utf-8", "replace")
    # Strip a UTF-8 BOM if present, ElementTree dislikes it in str input.
    xml_text = xml_text.lstrip("\ufeff")
    check_xml_safe(xml_text)
    root = ElementTree.fromstring(xml_text)

    library = Library(source=source)
    product = root.find("PRODUCT")
    if product is not None:
        library.product = dict(product.attrib)

    collection = root.find("COLLECTION")
    if collection is not None:
        for element in collection.findall("TRACK"):
            track = _read_track(element)
            if track.track_id:
                library.tracks[track.track_id] = track

    playlists = root.find("PLAYLISTS")
    if playlists is not None:
        for node in playlists.findall("NODE"):
            library.playlists.append(_read_node(node, ""))
        # Rekordbox wraps everything in a single "ROOT" node; show its children
        # at the top level instead of making the user expand a pointless folder.
        if len(library.playlists) == 1 and library.playlists[0].name.upper() == "ROOT":
            library.playlists = library.playlists[0].children

    return library


def _read_track(element: ElementTree.Element) -> Track:
    attrs = element.attrib
    track = Track(track_id=str(attrs.get("TrackID", "")))
    track.title = attrs.get("Name", "") or ""
    track.artist = attrs.get("Artist", "") or ""
    track.album = attrs.get("Album", "") or ""
    track.genre = attrs.get("Genre", "") or ""
    track.label = attrs.get("Label", "") or ""
    track.mix = attrs.get("Mix", "") or ""
    track.comments = attrs.get("Comments", "") or ""
    track.tonality = attrs.get("Tonality", "") or ""
    track.kind = attrs.get("Kind", "") or ""
    track.location = attrs.get("Location", "") or ""
    track.bpm = _float(attrs.get("AverageBpm"))
    track.duration = _float(attrs.get("TotalTime"))
    track.rating = _int(attrs.get("Rating"))
    track.play_count = _int(attrs.get("PlayCount"))
    track.year = _int(attrs.get("Year"))
    track.track_number = _int(attrs.get("TrackNumber"))
    track.date_added = attrs.get("DateAdded", "") or ""
    track.colour = attrs.get("Colour", "") or ""

    track.cues = _read_cues(element)

    if not track.bpm:
        # Fall back to the first tempo marker.
        tempo = element.find("TEMPO")
        if tempo is not None:
            track.bpm = _float(tempo.attrib.get("Bpm"))
    return track


def _read_cues(element: ElementTree.Element) -> list[dict]:
    """The ``<POSITION_MARK>`` cues a track already carries (XML ``Num`` >= 0
    is a hot cue, ``-1`` a memory cue).

    Shaped like :func:`parity.rxdb._cue_rows`, so the UI can report existing
    cues the same way whether the library came from an XML export or read
    straight out of master.db.
    """
    rows: list[dict] = []
    for mark in element.findall("POSITION_MARK"):
        attrs = mark.attrib
        num = _int(attrs.get("Num"))
        start = _float(attrs.get("Start"))
        end = _float(attrs.get("End"))
        rows.append(
            {
                "num": num,
                "name": attrs.get("Name", "") or "",
                "start": round(start, 3),
                "end": round(end, 3) if end > start else None,
                "kind": "memory" if num < 0 else "hot",
                "loop": end > start,
                "colour_index": 0,
                "source": "xml",
            }
        )
    rows.sort(key=lambda row: (row["num"], row["start"]))
    return rows


def _read_node(element: ElementTree.Element, prefix: str) -> Playlist:
    name = element.attrib.get("Name", "") or ""
    path = f"{prefix}/{name}" if prefix else name
    node = Playlist(playlist_id=f"pl:{hashlib.sha1(path.encode('utf-8')).hexdigest()[:12]}", name=name)
    for child in element:
        if child.tag == "TRACK":
            track_id = child.attrib.get("Key") or child.attrib.get("TrackID")
            if track_id:
                node.track_ids.append(str(track_id))
        elif child.tag == "NODE":
            node.children.append(_read_node(child, path))
    return node
