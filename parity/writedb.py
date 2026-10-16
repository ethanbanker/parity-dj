"""Write cues and playlist order straight into rekordbox's ``master.db``.

This is the counterpart of :mod:`parity.rxdb`: reading a library is safe at
any time because it works on a snapshot copy, but *writing* means touching the
file rekordbox itself has open, so the rules are stricter:

* **rekordbox must be closed.**  pyrekordbox refuses to commit while the app
  runs (its USN bookkeeping and ``masterPlaylists6.xml`` sync assume exclusive
  access), and SQLCipher writing into a database another process is using can
  corrupt it.  :func:`assert_closed` runs before anything is opened, so the
  check happens even when the caller would have been happy with a partial run.
* **a backup is taken first**, always, next to the database.  ``master.db``
  plus its ``-wal``/``-shm`` siblings and ``masterPlaylists6.xml`` are copied
  into a timestamped folder before the first write.
* **nothing is written without an explicit call.**  Parity's export path
  stays read-only; this module is only reached from the write endpoints, the
  ``--write-*`` CLI flags, and their tests.
* **dry runs never open the live file.**  ``dry_run=True`` resolves everything
  against a snapshot copy and rolls back, so a preview cannot alter a byte of
  the real library.

Every row that is touched is logged through ``parity.writedb`` and returned in
the report, so the user can see exactly which cues were replaced and which
playlist entries moved.
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from . import rxdb, structure as structure_mod, tags as tags_mod

log = logging.getLogger("parity.writedb")

#: Command-line fragments that mean "the app is open".  Matching on the full
#: executable path rather than the bare name keeps a process scan from hitting
#: Parity's own command line (``python3 parity.py --from-rekordbox``), and
#: from matching editor windows that merely mention the name.
REKORDBOX_PATTERNS = (
    "rekordbox.app/Contents/MacOS/rekordbox",
    "rekordboxAgent.app/Contents/MacOS/rekordboxAgent",
)
APP_PATTERNS = {"rekordbox": REKORDBOX_PATTERNS}

#: ``DjmdContent.CueUpdated``: rekordbox's own marker for "this track's cues
#: changed".  It is the string ``"1"`` on every cued track in the collection and
#: ``NULL`` on every track without cues, so that is what we write.
CUES_UPDATED_FLAG = "1"

#: rekordbox's hot-cue colour palette (``djmdColor.Commnt`` names, ids 1..8).
#: Parity names its eight cues by colour, so the mapping is by name and never
#: by guessed RGB values.
PALETTE_IDS = {
    "pink": 1,
    "red": 2,
    "orange": 3,
    "yellow": 4,
    "green": 5,
    "aqua": 6,
    "blue": 7,
    "purple": 8,
}
PALETTE_ALIASES = {"cyan": "aqua", "teal": "aqua", "magenta": "pink", "purple": "purple"}

#: ``djmdCue.Color`` is 255 ("no explicit colour") on every cue rekordbox wrote
#: for a coloured cue point; the palette index lives in ``ColorTableIndex``.
COLOUR_UNSET = 255

#: ``djmdCue.InFrame`` in the user's library is ``InMsec * 150 / 1000`` (rekordbox
#: writes frames at 150 fps for uncompressed audio), ``OutMsec`` is -1 for a cue
#: that is not a loop.  Mirroring the values rekordbox itself writes keeps the
#: rows byte-compatible with the ones it produces in the GUI.
FRAMES_PER_SECOND = 150
OUT_MSEC_NONE = -1

#: How many backups to keep beside the database before the oldest is dropped.
BACKUP_KEEP = 5

#: ``DjmdContent.Commnt`` is declared ``VARCHAR(255)``.  SQLite does not enforce
#: it, so a longer comment is stored rather than rejected -- but rekordbox wrote
#: the schema for a reason, and a comment that overflows in another tool is a
#: problem the user cannot see coming.  Past the limit the write still happens
#: (refusing one because a comment was long would be worse) and the report says
#: which tracks went over.
COMMENT_LIMIT = 255


class WriteBlocked(RuntimeError):
    """The write cannot run (rekordbox is open, database missing, ...)."""


class WriteUnavailable(RuntimeError):
    """pyrekordbox is missing, so ``master.db`` cannot be opened at all."""


# ------------------------------------------------------------------ processes


def _process_list() -> list[tuple[int, str]]:
    """``(pid, command line)`` for every process we can see."""
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - exotic hosts
        return []
    rows: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            rows.append((int(pid_text), command.strip()))
        except ValueError:
            continue
    return rows


def _own_pids() -> set[int]:
    """Our pid plus every ancestor, so we never trip over our own command line."""
    pids = {os.getpid()}
    pid = os.getpid()
    for _ in range(12):
        try:
            parent = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            break
        if not parent or not parent.isdigit():
            break
        pid = int(parent)
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
    return pids


def running_processes(patterns: tuple[str, ...] | list[str]) -> list[dict]:
    """Processes matching any command-line fragment, excluding ourselves."""
    patterns = tuple(patterns or ())
    if not patterns:
        return []
    skip = _own_pids()
    found: list[dict] = []
    for pid, command in _process_list():
        if pid in skip:
            continue
        if any(pattern in command for pattern in patterns):
            found.append({"pid": pid, "command": command.split(" --")[0][:160]})
    return found


def blocked_by(app: str = "rekordbox") -> list[dict]:
    """Processes that must be closed before writing, for a known app name."""
    return running_processes(APP_PATTERNS.get(app, ()))


def assert_closed(name: str, patterns: tuple[str, ...] | list[str] | None = None) -> None:
    """Raise :class:`WriteBlocked` while the app is running."""
    running = blocked_by(name) if patterns is None else running_processes(patterns)
    if running:
        names = ", ".join(str(process["pid"]) for process in running[:3])
        more = "" if len(running) <= 3 else f" (+{len(running) - 3} more)"
        raise WriteBlocked(
            f"{name} is running (pid {names}{more}). Quit {name} completely, then "
            f"run the write again - Parity will not touch the database while "
            f"it is open."
        )


# --------------------------------------------------------------------- paths


def master_db_path(db_path: Path | None = None) -> Path:
    return rxdb.master_db_path() if db_path is None else Path(db_path).expanduser()


def backup_dir_for(db_path: Path, backup_root: Path | None = None) -> Path:
    if backup_root is not None:
        return Path(backup_root)
    return db_path.parent / "parity-writes"


def backup(db_path: Path, backup_root: Path | None = None, keep: int = BACKUP_KEEP) -> Path:
    """Copy the database and the sidecars rekordbox keeps next to it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = backup_dir_for(db_path, backup_root)
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
    for name in ("masterPlaylists6.xml", "automixPlaylist6.xml"):
        source = db_path.parent / name
        if source.exists():
            shutil.copy2(source, target / name)
    _prune_backups(root, keep)
    log.info("backed up %s -> %s", db_path, target)
    return target


def _prune_backups(root: Path, keep: int) -> None:
    if keep <= 0:
        return
    folders = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda p: p.name)
    for stale in folders[:-keep]:
        shutil.rmtree(stale, ignore_errors=True)
        log.info("pruned old backup %s", stale)


# ----------------------------------------------------------------- palette


def palette_index(colour: str) -> int:
    """``djmdColor`` id for a Parity cue colour name, or 0 when unknown."""
    key = (colour or "").strip().lower()
    key = PALETTE_ALIASES.get(key, key)
    return PALETTE_IDS.get(key, 0)


# ----------------------------------------------------------------- adapter


def _load_pyrekordbox():
    """Return ``(MasterDatabase, models, error)``."""
    try:
        from pyrekordbox import masterdb  # type: ignore
        from pyrekordbox.masterdb import models  # type: ignore
    except Exception as exc:  # noqa: BLE001 - surfaced as a hint
        return None, None, f"{type(exc).__name__}: {exc}"
    database = getattr(masterdb, "MasterDatabase", None)
    if database is None:  # pragma: no cover - older layout
        try:
            from pyrekordbox import Rekordbox6Database as database  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return None, None, f"{type(exc).__name__}: {exc}"
    return database, models, None


class RekordboxDb:
    """The handful of database operations the writer needs.

    Everything the writer does goes through this class, so tests can drive the
    same logic with a fake that records calls instead of opening SQLCipher.
    """

    def __init__(self, path: Path):
        database_class, models, error = _load_pyrekordbox()
        if database_class is None:
            raise WriteUnavailable(
                f"pyrekordbox is not available ({error}). {rxdb.INSTALL_HINT}"
            )
        self.path = Path(path)
        self.models = models
        self.db = database_class(path=str(self.path), db_dir=self.path.parent)

    # -- reading ---------------------------------------------------------

    def contents(self) -> list:
        return list(self.db.get_content())

    def playlists(self) -> list:
        """Every playlist and folder, in the order rekordbox shows them."""
        rows = list(self.db.get_playlist())
        rows.sort(key=lambda row: (str(getattr(row, "ParentID", "") or ""), int(getattr(row, "Seq", 0) or 0)))
        return rows

    def playlist_by(self, value: str, include_folders: bool = False):
        """A playlist row by id (exact) or by name (case-insensitive)."""
        wanted = str(value or "").strip()
        if not wanted:
            return None
        for row in self.playlists():
            if str(getattr(row, "ID", "")) == wanted:
                return row
        return self.playlist_by_name(wanted, include_folders=include_folders)

    def playlist_by_name(self, name: str, include_folders: bool = False):
        rows = [
            row
            for row in self.db.get_playlist()
            if (getattr(row, "Name", "") or "").strip().lower() == name.strip().lower()
        ]
        rows.sort(key=lambda row: str(getattr(row, "ID", "")))
        if include_folders:
            return rows[0] if rows else None
        for row in rows:
            if not getattr(row, "Attribute", 0):
                return row
        return rows[0] if rows else None

    def playlist_songs(self, playlist) -> list:
        rows = [
            song
            for song in self.db.get_playlist_songs()
            if str(getattr(song, "PlaylistID", "")) == str(getattr(playlist, "ID", ""))
        ]
        rows.sort(key=lambda song: int(getattr(song, "TrackNo", 0) or 0))
        return rows

    def cues_for(self, content_id: str) -> list:
        return [
            cue
            for cue in self.db.get_cue()
            if str(getattr(cue, "ContentID", "")) == str(content_id)
        ]

    def songs_for_content(self, content_id: str) -> list:
        """Every playlist entry pointing at a track, across all playlists."""
        return [
            song
            for song in self.db.get_playlist_songs()
            if str(getattr(song, "ContentID", "")) == str(content_id)
        ]

    # -- writing ---------------------------------------------------------

    def create_playlist(self, name: str):
        return self.db.create_playlist(name)

    def add_playlist_song(self, playlist, content_id: str, track_no: int):
        """Create one ``djmdSongPlaylist`` row, without committing.

        pyrekordbox's own ``remove_from_playlist`` calls ``commit()`` once per
        row, which would split the reorder across N transactions and -- because
        that ``commit`` refuses to run while rekordbox is open -- make even a dry
        run depend on a write.  Going through the session directly keeps the
        whole reorder inside the caller's single transaction.
        """
        now = datetime.datetime.now()
        row = self.models.DjmdSongPlaylist.create(
            ID=str(uuid4()),
            PlaylistID=str(getattr(playlist, "ID", "")),
            ContentID=str(content_id),
            TrackNo=int(track_no),
            UUID=str(uuid4()),
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        return row

    def delete_playlist_song(self, song) -> None:
        """Drop one ``djmdSongPlaylist`` row, without committing (see above)."""
        self.db.delete(song)

    def next_cue_id(self) -> str:
        return str(self.db.generate_unused_id(self.models.DjmdCue))

    def add_cue(self, content, *, kind: int, msec: int, comment: str, colour_index: int):
        models = self.models
        cue = models.DjmdCue()
        cue.ID = self.next_cue_id()
        cue.ContentID = str(content.ID)
        cue.ContentUUID = getattr(content, "UUID", None)
        cue.InMsec = int(msec)
        cue.InFrame = int(round(int(msec) * FRAMES_PER_SECOND / 1000.0))
        cue.InMpegFrame = 0
        cue.InMpegAbs = 0
        cue.OutMsec = OUT_MSEC_NONE
        cue.OutFrame = 0
        cue.OutMpegFrame = 0
        cue.OutMpegAbs = 0
        cue.Kind = int(kind)
        cue.Color = COLOUR_UNSET
        cue.ColorTableIndex = int(colour_index)
        cue.ActiveLoop = 0
        cue.Comment = comment
        cue.BeatLoopSize = 0
        cue.CueMicrosec = 0
        self.db.add(cue)
        return cue

    def delete_cue(self, cue) -> None:
        self.db.delete(cue)

    def delete_playlist(self, playlist) -> None:
        """Delete a playlist or folder, with pyrekordbox's own bookkeeping.

        ``MasterDatabase.delete_playlist`` renumbers the ``Seq`` values of its
        siblings and drops it from ``masterPlaylists6.xml``; children of a folder
        go with it.  It never commits, so this stays inside our transaction.
        """
        self.db.delete_playlist(playlist)

    def rename_playlist(self, playlist, name: str) -> None:
        self.db.rename_playlist(playlist, name)

    def delete_content(self, content) -> dict:
        """Drop a track row and everything that hangs off it.

        ``djmdContent`` is referenced by cues, playlist entries, my tags, active
        censors and mixer parameters; a row removed on its own would leave the
        rest behind, so they go first.  Nothing on disk is touched -- the audio
        file and its ANLZ analysis stay exactly where they are.
        """
        content_id = str(getattr(content, "ID", ""))
        removed = 0
        for name in (
            "DjmdCue",
            "DjmdSongPlaylist",
            "DjmdSongMyTag",
            "DjmdActiveCensor",
            "DjmdMixerParam",
        ):
            model = getattr(self.models, name, None)
            column = getattr(model, "ContentID", None) if model is not None else None
            if column is None:  # pragma: no cover - model set varies by version
                continue
            for row in list(self.db.query(model).filter(column == content_id)):
                self.db.delete(row)
                removed += 1
        self.db.delete(content)
        return {"content_id": content_id, "dependents": removed}

    def set_comment(self, content, comment: str) -> None:
        """Set ``DjmdContent.Commnt``, without committing (see the cue writer).

        The comment is the one field a DJ already uses to say something about a
        track, and rekordbox searches it directly -- ``DjmdContent.SearchStr`` is
        ``NULL`` on every row in a real library, so there is no index to update
        alongside it and no second place for the tag to go stale.
        """
        content.Commnt = str(comment or "")

    def mark_cues_updated(self, content) -> None:
        """Flag the content as having new cues.

        rekordbox keeps ``DjmdContent.CueUpdated`` at ``"1"`` on every track
        that has cues and ``NULL`` on every track that has none (checked across
        the whole collection), so a cue change has to set it for rekordbox to
        pick the edit up rather than trust its cached view.
        """
        content.CueUpdated = CUES_UPDATED_FLAG

    def commit(self) -> None:
        # pyrekordbox's commit bumps the local USN and rewrites
        # masterPlaylists6.xml, which is what makes rekordbox accept the change.
        self.db.commit()

    def rollback(self) -> None:
        try:
            self.db.session.rollback()
        except Exception:  # noqa: BLE001 - best effort
            pass

    def close(self) -> None:
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------- writing


class _Session:
    '''An open database, plus the housekeeping that came with it.'''

    def __init__(self, handle, backup_path: str | None, workdir: Path | None):
        self.handle = handle
        self.backup = backup_path
        self._workdir = workdir

    def close(self) -> None:
        self.handle.close()
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)


def _session(db_path=None, *, dry_run=False, backup_root=None, database=None) -> _Session:
    '''Open master.db for a write, under the guards every write shares.

    A committing write refuses to run while rekordbox is open and takes a
    timestamped backup first; a dry run works on a private snapshot instead, so
    it cannot alter a byte of the live file.  ``database`` is the injectable
    seam the tests drive, and skips both.  The caller rolls back or commits and
    then calls ``close()``.
    '''
    path = master_db_path(db_path)
    backup_path: str | None = None
    workdir: Path | None = None
    if database is None:
        if not path.is_file():
            raise WriteBlocked(f'No rekordbox database at {path}')
        if dry_run:
            import tempfile

            workdir = Path(tempfile.mkdtemp(prefix='parity-dryrun-'))
            try:
                path = rxdb.snapshot(path, workdir)
            except OSError as exc:  # pragma: no cover - unreadable database
                shutil.rmtree(workdir, ignore_errors=True)
                raise WriteBlocked(f'Could not read {path}: {exc}') from exc
        else:
            assert_closed('rekordbox')
            backup_path = str(backup(path, backup_root))
    handle = database if database is not None else RekordboxDb(path)
    return _Session(handle, backup_path, workdir)


#: What to do with a track that already has cues.  ``overwrite`` is
#: Parity's own eight pads, as it has always been; ``keep`` leaves a track
#: alone when *the user* placed cues on it (cues carrying one of Parity's own
#: names are still replaced, so choosing "keep" does not freeze the library on
#: the first write -- see :mod:`parity.structure`).
CUE_POLICIES = ("overwrite", "keep")
DEFAULT_CUE_POLICY = "overwrite"


def normalise_cue_policy(value: object) -> str:
    policy = str(value or "").strip().lower()
    return policy if policy in CUE_POLICIES else DEFAULT_CUE_POLICY


def _plan_hot_cues(plan: dict) -> list[dict]:
    hot = plan.get("hot_cues") or plan.get("hot") or []
    return sorted(hot, key=lambda cue: int(cue.get("num", 0) or 0))


def _hot_cue_rows(existing: list) -> list:
    """The hot cues (``Kind`` 2..9) a track already has in rekordbox."""
    offset = rxdb.CUE_KIND_OFFSET
    rows = []
    for cue in existing:
        kind = int(getattr(cue, "Kind", -1) or -1)
        if offset <= kind <= offset + rxdb.MAX_HOT_CUE:
            rows.append(cue)
    return rows


def user_hot_cues(existing: list) -> list:
    """Hot cues on a track that Parity did not write itself.

    Both kinds look the same in ``DjmdCue`` -- the only thing telling them apart
    is the comment Parity stamps onto its own cues.  Anything else (empty
    comment, or a name the DJ typed) was placed by hand, and a write under the
    ``keep`` policy leaves those tracks alone.
    """
    return [
        cue
        for cue in _hot_cue_rows(existing)
        if not structure_mod.is_parity_cue(str(getattr(cue, "Comment", "") or ""))
    ]


def _songs_by_content(songs: list) -> dict[str, object]:
    return {str(getattr(song, "ContentID", "")): song for song in songs}


def _current_order(songs: list) -> list[str]:
    return [str(getattr(song, "ContentID", "")) for song in songs]


def write_set(
    order: list[dict],
    cue_plans: dict[str, dict] | None = None,
    *,
    playlist: str = "",
    set_name: str = "",
    cues: bool = True,
    memory: bool = False,
    dry_run: bool = False,
    cue_policy: str = DEFAULT_CUE_POLICY,
    cue_policies: dict[str, str] | None = None,
    tags: dict[str, str] | None = None,
    db_path: Path | None = None,
    backup_root: Path | None = None,
    database=None,
) -> dict:
    """Write cues and/or playlist order into ``master.db``.

    ``order`` is the built set as the API hands it over (``id``, ``key``, ...),
    ``cue_plans`` maps a track key to its generated plan.  Returns a report that
    lists every row that was (or would be) touched.

    ``cue_policy`` says what to do with tracks that already have cues of their
    own: ``overwrite`` replaces the eight hot slots either way, ``keep`` skips
    those tracks and leaves the cues the DJ placed by hand untouched.
    ``cue_policies`` overrides that per track key, which is how the UI's
    per-track switch reaches the writer.

    ``tags`` maps a track key to its set tag (``[E7] [peak] 14/24``).  A tag is
    *appended* to the track's comment and an earlier tag of Parity's is
    replaced, so nothing the DJ typed is lost and a second write does not stack
    a second tag -- see :mod:`parity.tags`.
    """
    path = master_db_path(db_path)
    cue_plans = cue_plans or {}
    policies = {str(k): normalise_cue_policy(v) for k, v in (cue_policies or {}).items()}
    report: dict = {
        "database": str(path),
        "dry_run": bool(dry_run),
        "backup": None,
        "cue_policy": normalise_cue_policy(cue_policy),
        "cues": {
            "created": 0,
            "replaced": 0,
            "tracks": 0,
            "memory": 0,
            "removed_memory": 0,
            "rows": [],
            "duplicates": [],
            "skipped": [],
            "kept": [],
            "policy": normalise_cue_policy(cue_policy),
        },
        "tags": {
            "changed": 0,
            "unchanged": 0,
            "rows": [],
            "skipped": [],
            "over_limit": [],
        },
        "playlist": None,
        "warnings": [],
    }

    session = _session(db_path, dry_run=dry_run, backup_root=backup_root, database=database)
    report["backup"] = session.backup
    handle = session.handle
    try:
        contents = handle.contents()
        by_id = {str(getattr(row, "ID", "")): row for row in contents}
        by_path = {
            str(getattr(row, "FolderPath", "") or ""): row
            for row in contents
            if getattr(row, "FolderPath", "")
        }

        resolved: list[tuple[dict, object]] = []
        for track in order:
            content = by_id.get(str(track.get("id"))) or by_path.get(str(track.get("path") or ""))
            if content is None:
                report["cues"]["skipped"].append(
                    {"key": track.get("key", ""), "label": _label(track), "reason": "not in rekordbox"}
                )
                continue
            resolved.append((track, content))

        if cues:
            _write_cues(
                handle,
                resolved,
                cue_plans,
                memory=memory,
                report=report,
                policies=policies,
                default_policy=report["cue_policy"],
            )

        if tags:
            _write_tags(handle, resolved, tags, report=report)

        if playlist:
            report["playlist"] = _write_playlist(handle, playlist, set_name, resolved, report=report)

        kept = len(report["cues"]["kept"])
        if dry_run:
            handle.rollback()
            log.info(
                "dry run: %d cue rows, %d comment tag(s), %d track(s) kept as they are "
                "(%s), playlist=%s (nothing committed)",
                report["cues"]["created"],
                report["tags"]["changed"],
                kept,
                report["cues"]["policy"],
                report["playlist"]["name"] if report["playlist"] else "-",
            )
        else:
            handle.commit()
            log.info(
                "wrote %d cue rows, %d comment tag(s), kept %d track(s) as they are "
                "(%s), playlist %s to %s",
                report["cues"]["created"],
                report["tags"]["changed"],
                kept,
                report["cues"]["policy"],
                report["playlist"]["name"] if report["playlist"] else "-",
                path,
            )
        return report
    except Exception:
        handle.rollback()
        raise
    finally:
        session.close()


def _label(track: dict) -> str:
    artist = (track.get("artist") or "").strip()
    title = (track.get("title") or "").strip() or "Untitled"
    return f"{artist} - {title}" if artist else title


def _write_cues(
    handle,
    resolved,
    cue_plans,
    *,
    memory: bool,
    report: dict,
    policies: dict[str, str] | None = None,
    default_policy: str = DEFAULT_CUE_POLICY,
) -> None:
    """Replace Parity's own hot-cue slots (A-H) and add the new rows."""
    offset = rxdb.CUE_KIND_OFFSET  # Kind 2 == hot cue A (Num 0)
    policies = policies or {}
    slots = {}
    for index in range(rxdb.MAX_HOT_CUE + 1):  # A-H: eight slots, all Parity's
        slots[index] = offset + index
    memory_kind = offset - 1

    for track, content in resolved:
        key = track.get("key", "")
        existing = handle.cues_for(str(content.ID))

        # Under "keep", a track the DJ cued by hand is left exactly as it is --
        # no deletes, no inserts, not even the "cues were updated" flag.  The
        # check reads the database rather than the loaded library, so a cue
        # placed in rekordbox after the import is still respected.
        policy = policies.get(str(key), default_policy)
        if policy == "keep":
            theirs = user_hot_cues(existing)
            if theirs:
                entry = {
                    "key": key,
                    "label": _label(track),
                    "cues": len(theirs),
                    "names": [str(getattr(cue, "Comment", "") or "") for cue in theirs],
                    "seconds": [round(int(getattr(cue, "InMsec", 0) or 0) / 1000.0, 3) for cue in theirs],
                    "reason": "already has cues",
                }
                report["cues"]["kept"].append(entry)
                log.info(
                    "djmdCue keep content=%s (%s): %d hot cue(s) placed in rekordbox, "
                    "left untouched",
                    content.ID,
                    entry["label"],
                    entry["cues"],
                )
                continue

        plan = cue_plans.get(key)
        if not plan:
            report["cues"]["skipped"].append(
                {"key": key, "label": _label(track), "reason": "no cue plan"}
            )
            continue

        owned = set(slots.values()) | ({memory_kind} if memory else set())
        planned_ms = [int(cue.get("ms") or 0) for cue in _plan_hot_cues(plan)]
        for cue in existing:
            kind = int(getattr(cue, "Kind", -1) or -1)
            msec = int(getattr(cue, "InMsec", 0) or 0)
            # parity owns the hot slots outright; on top of that, a rekordbox
            # *memory* cue sitting on the same beat as one of ours is removed as
            # a duplicate.  That is almost always ``1.1Bars``, which rekordbox
            # puts on the first beat -- exactly where hot cue A goes.
            duplicate = kind == memory_kind and not memory and any(
                abs(msec - planned) <= structure_mod.DUPLICATE_CUE_MS for planned in planned_ms
            )
            if kind in owned or duplicate:
                log.info(
                    "djmdCue delete id=%s content=%s kind=%s msec=%s comment=%r%s",
                    getattr(cue, "ID", ""),
                    content.ID,
                    getattr(cue, "Kind", ""),
                    getattr(cue, "InMsec", ""),
                    getattr(cue, "Comment", ""),
                    " (duplicate of a hot cue)" if duplicate else "",
                )
                handle.delete_cue(cue)
                report["cues"]["replaced"] += 1
                if duplicate:
                    report["cues"]["removed_memory"] += 1
                    report["cues"]["duplicates"].append(
                        {
                            "content_id": str(content.ID),
                            "label": _label(track),
                            "msec": msec,
                            "seconds": round(msec / 1000.0, 3),
                            "comment": str(getattr(cue, "Comment", "") or ""),
                        }
                    )

        for cue in _plan_hot_cues(plan):
            number = int(cue.get("num", 0) or 0)
            kind = slots.get(number)
            if kind is None:
                continue  # only eight hot-cue slots exist; anything else is noise
            msec = int(cue.get("ms") or 0)
            colour_index = palette_index(cue.get("colour", ""))
            row = handle.add_cue(
                content,
                kind=kind,
                msec=msec,
                comment=str(cue.get("name") or ""),
                colour_index=colour_index,
            )
            entry = {
                "id": str(getattr(row, "ID", "")),
                "content_id": str(content.ID),
                "slot": cue.get("slot", ""),
                "kind": kind,
                "msec": msec,
                "seconds": round(msec / 1000.0, 3),
                "comment": str(cue.get("name") or ""),
                "colour_index": colour_index,
                "colour": cue.get("colour", ""),
                "label": _label(track),
            }
            report["cues"]["rows"].append(entry)
            report["cues"]["created"] += 1
            log.info(
                "djmdCue insert id=%s content=%s slot=%s kind=%d msec=%d inFrame=%d "
                "colorTableIndex=%d comment=%r (%s)",
                entry["id"],
                entry["content_id"],
                entry["slot"],
                kind,
                msec,
                int(round(msec * FRAMES_PER_SECOND / 1000.0)),
                colour_index,
                entry["comment"],
                entry["label"],
            )

        if memory:
            for cue in plan.get("memory_cues") or []:
                msec = int(cue.get("ms") or 0)
                row = handle.add_cue(
                    content,
                    kind=memory_kind,
                    msec=msec,
                    comment=str(cue.get("name") or ""),
                    colour_index=0,
                )
                report["cues"]["memory"] += 1
                log.info(
                    "djmdCue insert id=%s content=%s kind=%d (memory) msec=%d comment=%r",
                    getattr(row, "ID", ""),
                    content.ID,
                    memory_kind,
                    msec,
                    str(cue.get("name") or ""),
                )

        handle.mark_cues_updated(content)
        log.info("djmdContent update ID=%s CueUpdated=%r", content.ID, CUES_UPDATED_FLAG)
        report["cues"]["tracks"] += 1

    if memory:
        report["warnings"].append(
            "memory cues replace every Kind=1 cue on these tracks, including any "
            "placed by rekordbox's own cue analysis."
        )


def _write_tags(handle, resolved, tags: dict[str, str], *, report: dict) -> None:
    """Append the set tag to each track's comment in ``DjmdContent``.

    Nothing is assigned from scratch: the tag is merged with the comment the
    track already carries, and only Parity's own previous tag is dropped, so a
    comment the DJ typed survives and re-tagging the same track for a second set
    does not leave two tags behind.  A track whose comment already reads exactly
    like this is counted and left alone, which is what makes a second write of
    the same set cheap and honest in the report.
    """
    for track, content in resolved:
        key = str(track.get("key") or "")
        tag = str(tags.get(key) or "")
        if not tag:
            report["tags"]["skipped"].append(
                {"key": key, "label": _label(track), "reason": "no tag for this track"}
            )
            continue
        before = str(getattr(content, "Commnt", "") or "")
        after = tags_mod.merge_comment(before, tag)
        if after == before:
            report["tags"]["unchanged"] += 1
            continue
        handle.set_comment(content, after)
        entry = {
            "content_id": str(content.ID),
            "key": key,
            "label": _label(track),
            "tag": tag,
            "before": before,
            "after": after,
        }
        report["tags"]["rows"].append(entry)
        report["tags"]["changed"] += 1
        log.info(
            "djmdContent update ID=%s Commnt=%r (was %r) tag=%r (%s)",
            getattr(content, "ID", ""),
            after,
            before,
            tag,
            entry["label"],
        )
        if len(after) > COMMENT_LIMIT:
            report["tags"]["over_limit"].append(
                {"key": key, "label": entry["label"], "length": len(after)}
            )

    if report["tags"]["over_limit"]:
        report["warnings"].append(
            f"{len(report['tags']['over_limit'])} comment(s) are longer than the "
            f"{COMMENT_LIMIT} characters rekordbox declares for that field. The tag is "
            f"still written; a shorter comment leaves room for it."
        )


def _write_playlist(handle, name: str, set_name: str, resolved, *, report: dict) -> dict:
    """Point the playlist at the built set, in the built order."""
    playlist = handle.playlist_by_name(name)
    created = False
    if playlist is None:
        playlist = handle.create_playlist(name)
        created = True
        log.info("djmdPlaylist insert id=%s name=%r", getattr(playlist, "ID", ""), name)
    elif int(getattr(playlist, "Attribute", 0) or 0) != 0:
        raise WriteBlocked(
            f'"{name}" is a rekordbox folder or smart playlist, not a playlist that '
            f"Parity can reorder. Pick another name."
        )

    wanted = [str(getattr(content, "ID", "")) for _, content in resolved]
    songs = handle.playlist_songs(playlist)
    before = _current_order(songs)
    before_set = set(before)
    wanted_set = set(wanted)
    dropped = [content_id for content_id in before if content_id not in wanted_set]
    new_entries = [content_id for content_id in wanted if content_id not in before_set]

    removed = 0
    added = 0
    if before != wanted:
        # Rebuild the relationship rows in the built order, all inside the one
        # transaction the caller commits (or rolls back) at the end.  Removing
        # first keeps this a single well-defined operation: the playlist table
        # ends up exactly as ordered, with no dependency on how the row shuffling
        # around a partial reorder would behave.
        for song in songs:
            log.info(
                "djmdSongPlaylist delete id=%s playlist=%s content=%s trackNo=%s",
                getattr(song, "ID", ""),
                getattr(playlist, "ID", ""),
                getattr(song, "ContentID", ""),
                getattr(song, "TrackNo", ""),
            )
            handle.delete_playlist_song(song)
            removed += 1
        titles = {str(getattr(content, "ID", "")): _label(track) for track, content in resolved}
        for position, content_id in enumerate(wanted, start=1):
            handle.add_playlist_song(playlist, content_id, position)
            log.info(
                "djmdSongPlaylist insert playlist=%s content=%s trackNo=%d title=%r",
                getattr(playlist, "ID", ""),
                content_id,
                position,
                titles.get(content_id, ""),
            )
            added += 1

    written = len(wanted) if before != wanted else 0
    log.info(
        "playlist %r (id=%s): %d tracks, %d rows written, %d removed, %d new, %d dropped, %s%s",
        name,
        getattr(playlist, "ID", ""),
        len(wanted),
        written,
        removed,
        len(new_entries),
        len(dropped),
        "unchanged" if before == wanted else "rewritten",
        " (created)" if created else "",
    )
    return {
        "name": name,
        "id": str(getattr(playlist, "ID", "")),
        "created": created,
        "tracks": len(wanted),
        "written": written,
        "removed": removed,
        "added": added,
        "new": len(new_entries),
        "dropped": len(dropped),
        "dropped_ids": dropped,
        "rewritten": before != wanted,
        "before": before,
        "order": wanted,
        "set_name": set_name,
    }


# ------------------------------------------------------------ library edits
#
# Deleting a playlist or a track takes something out of the library, so these
# follow the same rules as writing cues: rekordbox has to be closed, a backup is
# taken first, and a dry run lists what would go without opening the live
# database at all.  Audio files are never touched -- only the library rows.


def _playlist_row(playlist) -> dict:
    return {
        'id': str(getattr(playlist, 'ID', '')),
        'name': getattr(playlist, 'Name', '') or '',
        'folder': bool(getattr(playlist, 'Attribute', 0) or 0),
        'parent': str(getattr(playlist, 'ParentID', '') or ''),
    }


def _content_label(content) -> str:
    artist = (getattr(content, 'ArtistName', '') or '').strip()
    title = (getattr(content, 'Title', '') or '').strip() or 'Untitled'
    return f'{artist} - {title}' if artist else title


def _edit_report(path: Path, action: str, dry_run: bool) -> dict:
    return {
        'database': str(path),
        'action': action,
        'dry_run': bool(dry_run),
        'backup': None,
        'playlist': None,
        'tracks': [],
        'removed': 0,
        'skipped': [],
        'warnings': [],
    }


def delete_playlist(
    value: str, *, dry_run: bool = False, db_path=None, backup_root=None, database=None
) -> dict:
    '''Delete a playlist -- or a folder and everything inside it.

    ``value`` is a playlist id or its name.  The playlist's entries go with it,
    but the tracks stay in the collection and on disk.
    '''
    path = master_db_path(db_path)
    report = _edit_report(path, 'delete_playlist', dry_run)
    session = _session(db_path, dry_run=dry_run, backup_root=backup_root, database=database)
    report['backup'] = session.backup
    handle = session.handle
    try:
        playlist = handle.playlist_by(value, include_folders=True)
        if playlist is None:
            raise ValueError(f'No playlist or folder called "{value}" in rekordbox.')
        songs = handle.playlist_songs(playlist)
        row = _playlist_row(playlist)
        row['tracks'] = len(songs)
        report['playlist'] = row
        for song in songs:
            log.info(
                'djmdSongPlaylist delete id=%s playlist=%s content=%s trackNo=%s',
                getattr(song, 'ID', ''),
                row['id'],
                getattr(song, 'ContentID', ''),
                getattr(song, 'TrackNo', ''),
            )
            handle.delete_playlist_song(song)
            report['removed'] += 1
        handle.delete_playlist(playlist)
        log.info(
            'djmdPlaylist delete id=%s name=%r folder=%s (%d entries)',
            row['id'],
            row['name'],
            row['folder'],
            report['removed'],
        )
        if row['folder']:
            report['warnings'].append('A folder takes the playlists inside it with it.')
        if dry_run:
            handle.rollback()
        else:
            handle.commit()
        return report
    except Exception:
        handle.rollback()
        raise
    finally:
        session.close()


def rename_playlist(
    value: str, name: str, *, dry_run: bool = False, db_path=None, backup_root=None, database=None
) -> dict:
    '''Rename a playlist or folder; ``value`` is its id or current name.'''
    new_name = str(name or '').strip()
    if not new_name:
        raise ValueError('A playlist needs a name.')
    path = master_db_path(db_path)
    report = _edit_report(path, 'rename_playlist', dry_run)
    session = _session(db_path, dry_run=dry_run, backup_root=backup_root, database=database)
    report['backup'] = session.backup
    handle = session.handle
    try:
        playlist = handle.playlist_by(value, include_folders=True)
        if playlist is None:
            raise ValueError(f'No playlist or folder called "{value}" in rekordbox.')
        row = _playlist_row(playlist)
        report['playlist'] = row
        report['name'] = new_name
        if row['name'].strip().lower() == new_name.lower():
            report['warnings'].append('That is already the playlist name; nothing to change.')
        handle.rename_playlist(playlist, new_name)
        log.info('djmdPlaylist update id=%s name=%r -> %r', row['id'], row['name'], new_name)
        if dry_run:
            handle.rollback()
        else:
            handle.commit()
        return report
    except Exception:
        handle.rollback()
        raise
    finally:
        session.close()


def remove_playlist_tracks(
    value: str, content_ids, *, dry_run: bool = False, db_path=None, backup_root=None, database=None
) -> dict:
    '''Take tracks out of a playlist, leaving them in the collection.

    The remaining entries are renumbered from one, the same way
    :func:`write_set` rewrites a playlist, so the order is exactly what the
    library ends up with.
    '''
    wanted = {str(item) for item in (content_ids or [])}
    if not wanted:
        raise ValueError('No tracks selected to remove.')
    path = master_db_path(db_path)
    report = _edit_report(path, 'remove_playlist_tracks', dry_run)
    session = _session(db_path, dry_run=dry_run, backup_root=backup_root, database=database)
    report['backup'] = session.backup
    handle = session.handle
    try:
        playlist = handle.playlist_by(value, include_folders=True)
        if playlist is None:
            raise ValueError(f'No playlist or folder called "{value}" in rekordbox.')
        if getattr(playlist, 'Attribute', 0) or 0:
            raise WriteBlocked(f'{getattr(playlist, "Name", value)!r} is a folder, not a playlist.')
        songs = handle.playlist_songs(playlist)
        keep = [song for song in songs if str(getattr(song, 'ContentID', '')) not in wanted]
        removed = len(songs) - len(keep)
        report['playlist'] = {**_playlist_row(playlist), 'tracks': len(keep)}
        report['removed'] = removed
        report['skipped'] = [
            {'id': content_id, 'reason': 'not in that playlist'} for content_id in sorted(wanted - {str(getattr(s, 'ContentID', '')) for s in songs})
        ]
        if removed:
            for song in songs:
                log.info(
                    'djmdSongPlaylist delete id=%s playlist=%s content=%s',
                    getattr(song, 'ID', ''),
                    report['playlist']['id'],
                    getattr(song, 'ContentID', ''),
                )
                handle.delete_playlist_song(song)
            for position, song in enumerate(keep, start=1):
                handle.add_playlist_song(playlist, str(getattr(song, 'ContentID', '')), position)
                log.info(
                    'djmdSongPlaylist insert playlist=%s content=%s trackNo=%d',
                    report['playlist']['id'],
                    getattr(song, 'ContentID', ''),
                    position,
                )
            log.info(
                'playlist %r: %d entries removed, %d left',
                report['playlist']['name'],
                removed,
                len(keep),
            )
        if dry_run:
            handle.rollback()
        else:
            handle.commit()
        return report
    except Exception:
        handle.rollback()
        raise
    finally:
        session.close()


def delete_tracks(
    content_ids, *, dry_run: bool = False, db_path=None, backup_root=None, database=None
) -> dict:
    '''Delete tracks from the collection, leaving the files on disk.

    Every cue, playlist entry, my tag, censor and mixer parameter pointing at the
    track goes with it, and the audio file plus its ANLZ analysis are left where
    they are -- re-importing the file puts the track back, analysed, with its
    cues gone.
    '''
    wanted = [str(item) for item in (content_ids or [])]
    if not wanted:
        raise ValueError('No tracks selected to delete.')
    path = master_db_path(db_path)
    report = _edit_report(path, 'delete_tracks', dry_run)
    session = _session(db_path, dry_run=dry_run, backup_root=backup_root, database=database)
    report['backup'] = session.backup
    handle = session.handle
    try:
        by_id = {str(getattr(row, 'ID', '')): row for row in handle.contents()}
        for content_id in wanted:
            content = by_id.get(content_id)
            if content is None:
                report['skipped'].append({'id': content_id, 'reason': 'not in rekordbox'})
                continue
            cues = handle.cues_for(content_id)
            songs = handle.songs_for_content(content_id)
            report['tracks'].append(
                {
                    'id': content_id,
                    'label': _content_label(content),
                    'cues': len(cues),
                    'playlists': len(songs),
                }
            )
            removed = handle.delete_content(content)
            report['removed'] += 1
            log.info(
                'djmdContent delete ID=%s title=%r cues=%d playlists=%d dependents=%d',
                content_id,
                getattr(content, 'Title', ''),
                len(cues),
                len(songs),
                removed.get('dependents', 0),
            )
        if report['removed']:
            report['warnings'].append('The audio files were left on disk; only the library rows were removed.')
        if dry_run:
            handle.rollback()
        else:
            handle.commit()
        return report
    except Exception:
        handle.rollback()
        raise
    finally:
        session.close()


# -------------------------------------------------------------------- status


def status(db_path: Path | None = None, backup_root: Path | None = None) -> dict:
    """Everything the UI needs to explain whether a write can run right now."""
    path = master_db_path(db_path)
    database_class, _models, error = _load_pyrekordbox()
    root = backup_dir_for(path, backup_root)
    backups = sorted((item.name for item in root.iterdir() if item.is_dir())) if root.is_dir() else []
    running = blocked_by("rekordbox")
    return {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "running": running,
        "blocked": bool(running),
        "writable": path.is_file() and database_class is not None and not running,
        "pyrekordbox": {"available": database_class is not None, "error": error},
        "backup_dir": str(root),
        "backups": backups[-5:],
        "backups_count": len(backups),
        "supported": True,
    }
