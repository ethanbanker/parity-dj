"""Read and manage AUTO/PILOT's own library.

AUTO/PILOT keeps a plain SQLite library at
``~/Library/Application Support/AutoPilot/Database/AutoPilot.db`` with three
tables that matter here:

``Tracks``
    one row per audio file (``FileLocation`` is unique), linked back to the
    rekordbox entry through ``RekordboxTrackID`` and to its own analysis through
    ``ApdId``.
``Playlists`` / ``PlaylistTracks``
    the playlists and their order (``TrackOrder``).

Parity used to write the built set straight into those tables.  It no longer
does: a row written that way appears in AUTO/PILOT's library but never gets
analysed, because only the app's own importer fills in the ``.apd`` sidecar that
holds the beat grid, waveform, phrases and cues.  Exporting the set as
rekordbox XML and importing that in AUTO/PILOT is the one route that produces a
fully analysed track, so that is the route the app offers.

What is left here is the management half -- listing what AUTO/PILOT holds, and
deleting or renaming a playlist in it -- plus the guards every write into
somebody else's database needs: AUTO/PILOT must be closed, and a backup is taken
first.

The ``.apd`` container itself is AUTO/PILOT's private format: ``APFILE01``, eight
version bytes, two little-endian u64s (the second is always the file size minus
72), 16 per-file random bytes, a payload, then 24 per-file random bytes -- with a
payload of 7.97 bits of entropy per byte, no readable runs, and nothing that
inflates as deflate, zlib, LZMA, bz2, LZFSE or LZ4.  It is encrypted or
obfuscated, not packed, and AUTO/PILOT's binary carries no spec for it.  Only
the app can author one, which is why nothing here tries.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from . import writedb

log = logging.getLogger("parity.autopilot")

APP = "AutoPilot"
ENV_AUTOPILOT_DIR = "PARITY_AUTOPILOT_DIR"

#: Command-line fragment that means the app is open (see writedb.REKORDBOX_PATTERNS).
AUTOPILOT_PATTERNS = ("AutoPilot.app/Contents/MacOS/AutoPilot",)

#: Tables Parity reads and edits; their absence means the app has never run.
REQUIRED_TABLES = ("Tracks", "Playlists", "PlaylistTracks")

BACKUP_KEEP = 5


class AutopilotUnavailable(writedb.WriteUnavailable):
    """AUTO/PILOT's library is missing or unreadable."""


class AutopilotBlocked(writedb.WriteBlocked):
    """AUTO/PILOT is open, so its database cannot be written."""


# --------------------------------------------------------------------- paths


def app_dir() -> Path:
    override = os.environ.get(ENV_AUTOPILOT_DIR)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / APP


def database_path(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return app_dir() / "Database" / "AutoPilot.db"


def sidecar_root(db_path: Path | None = None) -> Path:
    return database_path(db_path).parent / "TrackData"


def sidecar_path(apd_id: str, db_path: Path | None = None) -> Path | None:
    """Where an ``ApdId`` sidecar lives, if the id looks usable."""
    apd_id = (apd_id or "").strip()
    if len(apd_id) < 4 or not all(char in "0123456789abcdefABCDEF" for char in apd_id):
        return None
    return sidecar_root(db_path) / apd_id[:2].lower() / f"{apd_id}.apd"


def running_processes(patterns=None) -> list[dict]:
    """Processes that mean AUTO/PILOT is open (its own or an override list)."""
    return writedb.running_processes(AUTOPILOT_PATTERNS if patterns is None else patterns)


def running() -> list[dict]:
    return running_processes()


def assert_closed() -> None:
    running_rows = running()
    if running_rows:
        names = ", ".join(str(row["pid"]) for row in running_rows[:3])
        raise AutopilotBlocked(
            f"{APP} is running (pid {names}). Quit {APP} completely, then run the write "
            f"again - Parity will not touch its library while it is open."
        )


# -------------------------------------------------------------------- status


def _connect(path: Path, read_only: bool = True) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    return sqlite3.connect(path, timeout=10)


def _playlists(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.PlaylistID, p.Name, p.Source, p.ParentPlaylistID, p.IsFolder, "
        "  (SELECT COUNT(*) FROM PlaylistTracks t WHERE t.PlaylistID = p.PlaylistID) "
        "FROM Playlists p ORDER BY p.SortOrder, p.PlaylistID"
    )
    return [
        {
            "id": int(row[0]),
            "name": row[1] or "",
            "source": row[2] or "",
            "parent": int(row[3]) if row[3] is not None else None,
            "folder": bool(row[4]),
            "tracks": int(row[5] or 0),
        }
        for row in rows
    ]


def track_ids_for_paths(paths, db_path: Path | None = None) -> dict[str, int]:
    """AUTO/PILOT ``TrackID`` per file path, for the paths it already knows.

    The link is ``Tracks.FileLocation``, the same column the app's own importer
    keys on, so it resolves a Parity track to the row AUTO/PILOT holds without
    guessing at ids.
    """
    path = database_path(db_path)
    wanted = [str(item) for item in (paths or []) if item]
    found: dict[str, int] = {}
    if not wanted or not path.is_file():
        return found
    try:
        with closing(_connect(path)) as conn:
            for item in wanted:
                row = conn.execute(
                    "SELECT TrackID FROM Tracks WHERE FileLocation = ? LIMIT 1", (item,)
                ).fetchone()
                if row is not None:
                    found[item] = int(row[0])
    except sqlite3.Error as exc:  # pragma: no cover - unreadable library
        log.info("could not resolve AUTO/PILOT track ids: %s", exc)
    return found


def status(db_path: Path | None = None, backup_root: Path | None = None) -> dict:
    """Whether a write can run now, and what AUTO/PILOT already holds."""
    path = database_path(db_path)
    running_rows = running()
    root = writedb.backup_dir_for(path, backup_root)
    info = {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "running": running_rows,
        "blocked": bool(running_rows),
        "sidecar_dir": str(sidecar_root(db_path)),
        "backup_dir": str(root),
        "tracks": 0,
        "playlists": [],
        "sidecars": 0,
        "error": None,
        "writable": False,
    }
    if not path.is_file():
        info["error"] = f"No AUTO/PILOT library at {path}"
        return info
    try:
        with closing(_connect(path)) as conn:
            info["tracks"] = conn.execute("SELECT COUNT(*) FROM Tracks").fetchone()[0]
            info["playlists"] = [row for row in _playlists(conn) if not row["folder"]]
            info["folders"] = [row for row in _playlists(conn) if row["folder"]]
    except sqlite3.Error as exc:
        info["error"] = f"Could not read {path}: {exc}"
        return info
    root_side = sidecar_root(db_path)
    if root_side.is_dir():
        info["sidecars"] = sum(1 for _ in root_side.glob("*/*.apd"))
    info["writable"] = not running_rows
    return info


# ------------------------------------------------------------------- backup


def backup(db_path: Path, backup_root: Path | None = None, keep: int = BACKUP_KEEP) -> Path:
    """Copy the library and its write-ahead log into a timestamped folder."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = writedb.backup_dir_for(db_path, backup_root)
    target = root / stamp
    suffix = 1
    while target.exists():
        suffix += 1
        target = root / f"{stamp}-{suffix}"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, target / db_path.name)
    for name in (db_path.name + "-wal", db_path.name + "-shm"):
        sidecar = db_path.with_name(name)
        if sidecar.exists():
            shutil.copy2(sidecar, target / sidecar.name)
    writedb._prune_backups(root, keep)
    log.info("backed up %s -> %s", db_path, target)
    return target


# ------------------------------------------------------------------- writes


class _Session:
    """An open AUTO/PILOT database, plus the housekeeping that came with it."""

    def __init__(self, connection: sqlite3.Connection, backup_path: str | None, workdir: Path | None):
        self.connection = connection
        self.backup = backup_path
        self._workdir = workdir

    def close(self) -> None:
        self.connection.close()
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)


def _session(db_path=None, *, dry_run=False, backup_root=None, connection=None) -> _Session:
    """Open AUTO/PILOT's library under the guards every write shares.

    A committing write refuses to run while the app is open and takes a backup
    first; a dry run works on a copy, so the live library cannot change.
    """
    path = database_path(db_path)
    backup_path: str | None = None
    workdir: Path | None = None
    if connection is None:
        if not path.is_file():
            raise AutopilotUnavailable(
                f"No AUTO/PILOT library at {path}. Open AUTO/PILOT once so it creates one."
            )
        if dry_run:
            workdir = Path(tempfile.mkdtemp(prefix="parity-ap-dryrun-"))
            target = workdir / path.name
            shutil.copy2(path, target)
            for name in (path.name + "-wal", path.name + "-shm"):
                sidecar = path.with_name(name)
                if sidecar.exists():
                    shutil.copy2(sidecar, workdir / sidecar.name)
            connection = _connect(target, read_only=False)
        else:
            assert_closed()
            backup_path = str(backup(path, backup_root))
            connection = _connect(path, read_only=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        names = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing = [name for name in REQUIRED_TABLES if name not in names]
        if missing:
            raise AutopilotUnavailable(
                "AUTO/PILOT's library has no "
                + ", ".join(missing)
                + " table yet. Open AUTO/PILOT once so it creates its library, then try again."
            )
    except Exception:
        connection.close()
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        raise
    return _Session(connection, backup_path, workdir)


def _edit_report(path: Path, action: str, dry_run: bool) -> dict:
    return {
        "database": str(path),
        "action": action,
        "dry_run": bool(dry_run),
        "backup": None,
        "playlist": None,
        "tracks": [],
        "removed": 0,
        "skipped": [],
        "warnings": [],
        "sidecars": None,
    }


def _playlist_row(conn: sqlite3.Connection, playlist_id: int) -> dict | None:
    row = conn.execute(
        "SELECT PlaylistID, Name, IsFolder, ParentPlaylistID FROM Playlists WHERE PlaylistID = ?",
        (int(playlist_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "name": row[1] or "",
        "folder": bool(row[2]),
        "parent": int(row[3]) if row[3] is not None else None,
        "tracks": conn.execute(
            "SELECT COUNT(*) FROM PlaylistTracks WHERE PlaylistID = ?", (int(row[0]),)
        ).fetchone()[0],
    }


def _descendants(conn: sqlite3.Connection, playlist_id: int) -> list[int]:
    """A folder's children, depth first."""
    ids: list[int] = []
    pending = [int(playlist_id)]
    while pending:
        current = pending.pop()
        children = [
            int(row[0])
            for row in conn.execute(
                "SELECT PlaylistID FROM Playlists WHERE ParentPlaylistID = ?", (current,)
            )
        ]
        for child in children:
            ids.append(child)
            pending.append(child)
    return ids


def delete_playlist(
    playlist_id, *, dry_run: bool = False, db_path=None, backup_root=None, connection=None
) -> dict:
    """Delete a playlist from AUTO/PILOT, and the playlists inside a folder too."""
    path = database_path(db_path)
    report = _edit_report(path, "delete_playlist", dry_run)
    session = _session(db_path, dry_run=dry_run, backup_root=backup_root, connection=connection)
    report["backup"] = session.backup
    conn = session.connection
    try:
        row = _playlist_row(conn, int(playlist_id))
        if row is None:
            raise ValueError(f"No AUTO/PILOT playlist with id {playlist_id}.")
        report["playlist"] = row
        targets = [int(playlist_id)] + _descendants(conn, int(playlist_id))
        if len(targets) > 1:
            report["warnings"].append(
                f"That folder holds {len(targets) - 1} playlist(s); they go with it."
            )
        for target in targets:
            removed = conn.execute(
                "DELETE FROM PlaylistTracks WHERE PlaylistID = ?", (target,)
            ).rowcount
            report["removed"] += max(0, removed or 0)
            log.info("PlaylistTracks delete playlist=%d (%d rows)", target, removed or 0)
            conn.execute("DELETE FROM Playlists WHERE PlaylistID = ?", (target,))
            log.info("Playlists delete id=%d", target)
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
        return report
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        session.close()


def rename_playlist(
    playlist_id, name: str, *, dry_run: bool = False, db_path=None, backup_root=None, connection=None
) -> dict:
    """Rename a playlist in AUTO/PILOT's library."""
    new_name = str(name or "").strip()
    if not new_name:
        raise ValueError("A playlist needs a name.")
    path = database_path(db_path)
    report = _edit_report(path, "rename_playlist", dry_run)
    session = _session(db_path, dry_run=dry_run, backup_root=backup_root, connection=connection)
    report["backup"] = session.backup
    conn = session.connection
    try:
        row = _playlist_row(conn, int(playlist_id))
        if row is None:
            raise ValueError(f"No AUTO/PILOT playlist with id {playlist_id}.")
        report["playlist"] = row
        report["name"] = new_name
        conn.execute("UPDATE Playlists SET Name = ? WHERE PlaylistID = ?", (new_name, int(playlist_id)))
        log.info("Playlists update id=%s name=%r -> %r", playlist_id, row["name"], new_name)
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
        return report
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        session.close()


def remove_playlist_tracks(
    playlist_id, track_ids, *, dry_run: bool = False, db_path=None, backup_root=None, connection=None
) -> dict:
    """Take tracks out of an AUTO/PILOT playlist, leaving the tracks themselves."""
    wanted = {int(item) for item in (track_ids or [])}
    if not wanted:
        raise ValueError("No tracks selected to remove.")
    path = database_path(db_path)
    report = _edit_report(path, "remove_playlist_tracks", dry_run)
    session = _session(db_path, dry_run=dry_run, backup_root=backup_root, connection=connection)
    report["backup"] = session.backup
    conn = session.connection
    try:
        row = _playlist_row(conn, int(playlist_id))
        if row is None:
            raise ValueError(f"No AUTO/PILOT playlist with id {playlist_id}.")
        if row["folder"]:
            raise AutopilotBlocked(f'"{row["name"]}" is a folder, not a playlist.')
        current = [
            int(item[0])
            for item in conn.execute(
                "SELECT TrackID FROM PlaylistTracks WHERE PlaylistID = ? ORDER BY TrackOrder",
                (int(playlist_id),),
            )
        ]
        keep = [track_id for track_id in current if track_id not in wanted]
        report["removed"] = len(current) - len(keep)
        report["playlist"] = {**row, "tracks": len(keep)}
        report["skipped"] = [
            {"id": track_id, "reason": "not in that playlist"} for track_id in sorted(wanted - set(current))
        ]
        if report["removed"]:
            conn.execute("DELETE FROM PlaylistTracks WHERE PlaylistID = ?", (int(playlist_id),))
            for position, track_id in enumerate(keep, start=1):
                conn.execute(
                    "INSERT INTO PlaylistTracks(PlaylistID, TrackID, TrackOrder) VALUES(?,?,?)",
                    (int(playlist_id), track_id, position),
                )
                log.info(
                    "PlaylistTracks insert playlist=%d track=%d TrackOrder=%d",
                    int(playlist_id),
                    track_id,
                    position,
                )
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
        return report
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        session.close()


def delete_tracks(
    track_ids, *, dry_run: bool = False, db_path=None, backup_root=None, connection=None
) -> dict:
    """Delete tracks from AUTO/PILOT's library, leaving the audio files alone.

    AUTO/PILOT's ``.apd`` sidecar is deliberately left behind: it is the app's
    own file, and a later import of the same audio simply writes a new one.
    """
    wanted = [int(item) for item in (track_ids or [])]
    if not wanted:
        raise ValueError("No tracks selected to delete.")
    path = database_path(db_path)
    report = _edit_report(path, "delete_tracks", dry_run)
    session = _session(db_path, dry_run=dry_run, backup_root=backup_root, connection=connection)
    report["backup"] = session.backup
    conn = session.connection
    try:
        for track_id in wanted:
            row = conn.execute(
                "SELECT COALESCE(Title,''), COALESCE(Artist,''), COALESCE(ApdId,''), "
                "COALESCE(FileLocation,'') FROM Tracks WHERE TrackID = ?",
                (track_id,),
            ).fetchone()
            if row is None:
                report["skipped"].append({"id": track_id, "reason": "not in AUTO/PILOT"})
                continue
            title, artist, apd_id, _location = row
            entries = conn.execute(
                "DELETE FROM PlaylistTracks WHERE TrackID = ?", (track_id,)
            ).rowcount
            conn.execute("DELETE FROM Tracks WHERE TrackID = ?", (track_id,))
            sidecar = sidecar_path(apd_id, db_path)
            report["tracks"].append(
                {
                    "id": track_id,
                    "label": f"{artist} - {title}" if artist else title,
                    "playlists": max(0, entries or 0),
                    "sidecar": str(sidecar) if sidecar else "",
                }
            )
            report["removed"] += 1
            log.info(
                "Tracks delete id=%d title=%r playlist entries=%d apd=%s",
                track_id,
                title,
                entries or 0,
                apd_id or "-",
            )
        if report["removed"]:
            report["warnings"].append(
                "The audio files and any .apd sidecars were left on disk; only the "
                "library rows were removed. AUTO/PILOT analyses the track again if you "
                "re-import it."
            )
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
        return report
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        session.close()
