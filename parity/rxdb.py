"""Direct access to the rekordbox library database.

Rekordbox 6/7 keeps everything in an SQLCipher-encrypted SQLite file at
``~/Library/Pioneer/rekordbox/master.db`` instead of the XML export, so reading
it gives us three things the XML cannot:

* playlists and folders, live, without the user exporting anything;
* which entries are streaming stubs rather than files;
* the ANLZ analysis files (beat grid, phrase structure) rekordbox wrote for
  every track -- see :mod:`parity.structure`.

Reading the database needs `pyrekordbox <https://github.com/dylanljones/pyrekordbox>`_,
which is the single optional dependency of Parity.  Everything here degrades
to "unavailable" instead of raising when it is missing, so the XML import path
keeps working exactly as before.

The database is **never opened in place**: opening it lets SQLCipher/SQLAlchemy
replay and checkpoint the write-ahead log, which rewrites the file.  A snapshot
copy (``master.db`` plus its ``-wal``/``-shm`` siblings and
``masterPlaylists6.xml``) is read instead and thrown away afterwards, so
Parity cannot damage a live library.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from .rekordbox import Library, Playlist, Track

log = logging.getLogger("parity.rxdb")

#: Environment overrides, handy for tests and for exotic installs.
ENV_MASTER_DB = "PARITY_MASTER_DB"
ENV_REKORDBOX_DIR = "PARITY_REKORDBOX_DIR"

#: rekordbox's ``DjmdContent.FileType`` for a streaming-only entry (file type
#: 11 is any local audio file).  Anything without a filesystem path is treated
#: as streaming too, so this is only a cross-check.
FILE_TYPE_STREAMING = 19

#: ``DjmdCue.Kind`` is the hot-cue slot plus two: 1 is a memory cue (XML
#: ``Num="-1"``) and 2..9 are hot cues A..H (XML ``Num="0"``..``"7"``).  Verified
#: against a library where the only exported cue had ``Kind=6`` and appeared as
#: ``Num="4"`` in rekordbox's own XML export.
CUE_KIND_OFFSET = 2
MAX_HOT_CUE = 7

INSTALL_HINT = (
    "install the optional extra with:  python3 tools/install_extras.py"
)
LOCAL_USER_HINT = "or:  python3 -m pip install --user pyrekordbox"


class MasterDbUnavailable(RuntimeError):
    """master.db cannot be read; the caller should fall back to an XML export."""


# --------------------------------------------------------------------- paths


def rekordbox_dir() -> Path:
    """The rekordbox application data folder."""
    override = os.environ.get(ENV_REKORDBOX_DIR)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Pioneer" / "rekordbox"


def master_db_path() -> Path:
    override = os.environ.get(ENV_MASTER_DB)
    if override:
        return Path(override).expanduser()
    return rekordbox_dir() / "master.db"


def share_dir() -> Path:
    """Where rekordbox keeps ANLZ analysis data for the local collection."""
    override = os.environ.get("PARITY_REKORDBOX_SHARE")
    if override:
        return Path(override).expanduser()
    return rekordbox_dir() / "share"


# ------------------------------------------------------------ availability


def _import_database_class():
    """Return (class, error) for pyrekordbox's database entry point.

    pyrekordbox renamed ``Rekordbox6Database`` to ``MasterDatabase`` in 0.5, so
    both are accepted.
    """
    try:
        from pyrekordbox.masterdb import MasterDatabase  # type: ignore
    except Exception:  # noqa: BLE001 - any import problem means "unavailable"
        try:
            from pyrekordbox import Rekordbox6Database as MasterDatabase  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"
    return MasterDatabase, None


def extras_status() -> dict:
    """Whether the optional pyrekordbox extra can be used from this process."""
    module, error = _import_database_class()
    version = None
    if module is not None:
        try:
            from importlib.metadata import version as _version

            version = _version("pyrekordbox")
        except Exception:  # noqa: BLE001
            version = "unknown"
    return {
        "available": module is not None,
        "version": version,
        "error": error,
        "interpreter": sys.executable,
        "hint": INSTALL_HINT,
        "fallback_hint": LOCAL_USER_HINT,
    }


def rekordbox_running() -> bool | None:
    """Whether the rekordbox app is open, or ``None`` if we cannot tell.

    Only informational: Parity reads a snapshot, so a running rekordbox is
    fine, but the library on disk may be a few seconds behind.
    """
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        for process in psutil.process_iter(["name"]):
            if "rekordbox" in (process.info.get("name") or "").lower():
                return True
    except Exception:  # noqa: BLE001
        return None
    return False


def db_status(db_path: Path | None = None) -> dict:
    """Everything the UI needs to describe the database before connecting."""
    path = master_db_path() if db_path is None else Path(db_path).expanduser()
    share = share_dir()
    status = {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "modified": None,
        "share_dir": str(share),
        "share_exists": share.is_dir(),
        "running": rekordbox_running(),
        "pyrekordbox": extras_status(),
        "readable": False,
        "error": None,
    }
    if path.is_file():
        import time

        status["modified"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    if not status["exists"]:
        status["error"] = f"No rekordbox database at {path}"
    elif not status["pyrekordbox"]["available"]:
        status["error"] = f"pyrekordbox is not installed. {INSTALL_HINT}"
    else:
        status["readable"] = True
    return status


# ----------------------------------------------------------------- snapshot


def snapshot(db_path: Path, dest_dir: Path) -> Path:
    """Copy the database and its sidecars into ``dest_dir`` and return the copy.

    A consistent snapshot needs the write-ahead log (``-wal``) and shared-memory
    file alongside the main database; SQLite replays the log when the copy is
    opened.  ``masterPlaylists6.xml`` carries the playlist ordering rekordbox
    keeps outside SQLite, so it is copied too.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / db_path.name
    shutil.copy2(db_path, target)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, dest_dir / sidecar.name)
    for name in ("masterPlaylists6.xml", "automixPlaylist6.xml"):
        source = db_path.parent / name
        if source.exists():
            shutil.copy2(source, dest_dir / name)
    return target


def snapshot_dir(db_path: Path, dest_dir: Path) -> Path:
    """Copy the whole database *folder* sidecar set into ``dest_dir``."""
    snapshot(db_path, dest_dir)
    return dest_dir


# ------------------------------------------------------------ db -> Library


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _related_name(instance: object) -> str:
    """Name of an optional relationship (Artist, Genre, Key, ...)."""
    if instance is None:
        return ""
    return _text(getattr(instance, "Name", None) or getattr(instance, "ScaleName", None))


def _file_kind(path: str) -> str:
    suffix = os.path.splitext(path)[1].lstrip(".").upper()
    return f"{suffix} File" if suffix else ""


def _location(path: str) -> str:
    """rekordbox's file URL for a path, keeping streaming ids recognisable."""
    if not path:
        return ""
    if path.startswith("/"):
        return "file://localhost" + quote(path)
    return "file://localhost" + path


def _cue_rows(cues_by_content: dict[str, list], content_id: str) -> list[dict]:
    rows: list[dict] = []
    for cue in cues_by_content.get(content_id, []):
        kind = cue.Kind if cue.Kind is not None else 1
        num = kind - CUE_KIND_OFFSET
        if num > MAX_HOT_CUE:
            num = MAX_HOT_CUE
        start = (cue.InMsec or 0) / 1000.0
        end = cue.OutMsec if cue.OutMsec is not None else None
        rows.append(
            {
                "num": num,
                "name": _text(cue.Comment),
                "start": round(start, 3),
                "end": round(end / 1000.0, 3) if end and end > 0 else None,
                "kind": "memory" if num < 0 else "hot",
                "loop": bool(cue.ActiveLoop),
                "colour_index": cue.ColorTableIndex,
                "source": "rekordbox",
            }
        )
    rows.sort(key=lambda row: (row["num"], row["start"]))
    return rows


def _anlz_paths(db, content, share_root: Path) -> dict[str, str]:
    """Absolute ANLZ paths that exist on disk for ``content``.

    ``AnalysisDataPath`` is stored relative to rekordbox's ``share`` folder
    (``/PIONEER/USBANLZ/...``).  It has to be joined here rather than left to
    pyrekordbox, which resolves it relative to the *database file* -- and we
    read a snapshot copy in a temporary directory, where no analysis exists.
    """
    found: dict[str, str] = {}
    raw = _text(getattr(content, "AnalysisDataPath", ""))
    if raw:
        for root in (share_root, share_root.parent):
            candidate = root / raw.lstrip("/")
            if candidate.is_file():
                found["DAT"] = str(candidate)
                break
        else:
            if Path(raw).is_file():
                found["DAT"] = raw

    if "DAT" not in found:
        try:
            fallback = db.get_anlz_paths(content) or {}
        except Exception:  # noqa: BLE001 - analysis is optional detail
            fallback = {}
        found.update({key: str(value) for key, value in fallback.items() if value})

    dat = found.get("DAT")
    if dat:
        ext = Path(dat).with_suffix(".EXT")
        if ext.is_file():
            found.setdefault("EXT", str(ext))
    return found


def _read_product(db_path: Path) -> dict:
    """rekordbox version, read from the playlist XML snapshot when present."""
    import xml.etree.ElementTree as ET

    playlist_xml = db_path.parent / "masterPlaylists6.xml"
    if not playlist_xml.exists():
        return {"Name": "rekordbox"}
    try:
        root = ET.parse(playlist_xml).getroot()
        product = root.find("PRODUCT")
        if product is not None:
            return dict(product.attrib)
    except Exception:  # noqa: BLE001
        pass
    return {"Name": "rekordbox"}


def _playlist_tree(db, track_ids_by_playlist: dict[str, list[str]]) -> list[Playlist]:
    """Build the playlist/folder tree from ``DjmdPlaylist.ParentID`` links."""
    rows = list(db.get_playlist())
    nodes: dict[str, Playlist] = {}
    parents: dict[str, str] = {}
    for row in rows:
        name = _text(row.Name) or "Untitled"
        nodes[str(row.ID)] = Playlist(
            playlist_id=f"pl:{row.ID}",
            name=name,
            track_ids=list(track_ids_by_playlist.get(str(row.ID), [])),
        )
        parents[str(row.ID)] = _text(row.ParentID) or "root"

    roots: list[Playlist] = []
    for row in rows:
        node = nodes[str(row.ID)]
        parent = parents[str(row.ID)]
        if parent and parent != "root" and parent in nodes:
            nodes[parent].children.append(node)
        else:
            roots.append(node)
    return roots


def _library_from_db(db, source: str, product: dict, share_root: Path | None = None) -> Library:
    library = Library(source=source, source_kind="db", product=product)

    cues_by_content: dict[str, list] = {}
    try:
        for cue in db.get_cue():
            cues_by_content.setdefault(str(cue.ContentID), []).append(cue)
    except Exception:  # noqa: BLE001 - cues are a bonus, never fatal
        cues_by_content = {}

    share_root = share_root or share_dir()
    for content in db.get_content():
        path = _text(content.FolderPath)
        anlz = _anlz_paths(db, content, share_root)
        track = Track(track_id=str(content.ID))
        track.title = _text(content.Title)
        track.artist = _related_name(getattr(content, "Artist", None))
        track.album = _related_name(getattr(content, "Album", None))
        track.genre = _related_name(getattr(content, "Genre", None))
        track.label = _related_name(getattr(content, "Label", None))
        track.tonality = _related_name(getattr(content, "Key", None))
        track.comments = _text(content.Commnt)
        track.location = _location(path)
        track.kind = _file_kind(path)
        track.bpm = round((content.BPM or 0) / 100.0, 2)
        track.duration = float(content.Length or 0)
        track.rating = int(content.Rating or 0)
        track.play_count = int(getattr(content, "DJPlayCount", 0) or 0)
        track.year = int(content.ReleaseYear or 0)
        track.track_number = int(content.TrackNo or 0)
        track.colour = _text(getattr(content, "ColorID", "") or "")
        created = getattr(content, "DateCreated", None)
        track.date_added = created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else ""
        track.analysed = bool(content.Analysed)
        track.analysis_path = anlz.get("DAT", "")
        track.cues = _cue_rows(cues_by_content, str(content.ID))
        library.tracks[track.track_id] = track

    ordered: dict[str, list[str]] = {}
    for row in db.get_playlist():
        playlist_id = str(row.ID)
        try:
            contents = list(db.get_playlist_contents(row))
        except Exception:  # noqa: BLE001 - empty or smart playlist
            contents = []
        ordered[playlist_id] = [str(item.ID) for item in contents]
    library.playlists = _playlist_tree(db, ordered)
    return library


# ------------------------------------------------------------------ reading


def read_library(db_path: Path | None = None, workdir: Path | None = None) -> Library:
    """Read the live rekordbox database into a :class:`~parity.rekordbox.Library`.

    Raises :class:`MasterDbUnavailable` with an actionable message when the
    database or the pyrekordbox extra is missing, so callers can fall back to
    the XML export path.
    """
    path = master_db_path() if db_path is None else Path(db_path).expanduser()
    if not path.is_file():
        raise MasterDbUnavailable(f"No rekordbox database at {path}")

    database_class, error = _import_database_class()
    if database_class is None:
        raise MasterDbUnavailable(
            f"pyrekordbox is not available ({error}). {INSTALL_HINT} {LOCAL_USER_HINT}"
        )

    work = Path(tempfile.mkdtemp(prefix="parity-db-", dir=str(workdir) if workdir else None))
    try:
        copy = snapshot(path, work)
        product = _read_product(copy)
        try:
            db = database_class(path=str(copy))
            library = _library_from_db(db, source=str(path), product=product, share_root=share_dir())
        except MasterDbUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            raise MasterDbUnavailable(
                f"pyrekordbox could not read {path}: {type(exc).__name__}: {exc}"
            ) from exc
        if not library.tracks:
            raise MasterDbUnavailable(f"{path} opened but contains no tracks.")
        return library
    finally:
        shutil.rmtree(work, ignore_errors=True)


def read_library_or_xml(
    xml_path: Path | None = None,
    db_path: Path | None = None,
    workdir: Path | None = None,
) -> tuple[Library, str, str]:
    """Try the database first, then an XML export.

    Returns ``(library, kind, detail)`` where ``kind`` is ``"db"`` or ``"xml"``
    and ``detail`` explains a fallback, mirroring the mechanism the product
    promises.  Raises :class:`MasterDbUnavailable` when neither works.
    """
    try:
        return read_library(db_path, workdir), "db", ""
    except MasterDbUnavailable as db_error:
        if xml_path is None or not xml_path.is_file():
            raise
        from . import rekordbox as rb

        text = xml_path.read_text(encoding="utf-8", errors="replace")
        library = rb.parse_collection(text, source=str(xml_path))
        library.source_kind = "xml"
        detail = f"master.db unavailable ({db_error}); used {xml_path}"
        log.warning("falling back to XML: %s", db_error)
        return library, "xml", detail
