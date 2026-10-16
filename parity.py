#!/usr/bin/env python3
"""Parity -- build DJ sets from a rekordbox playlist, locally.

    python3 parity.py                            # start the UI
    python3 parity.py --no-connect               # start without reading it
    python3 parity.py --import ~/rekordbox.xml   # preload a collection XML
    python3 parity.py --port 9000 --no-browser

The live rekordbox library is read at startup, so there is nothing to press
before you can build a set.  Writing back into a library happens on demand, and
never by accident:

    python3 parity.py --write-status                     # can a write run?
    python3 parity.py --write-rekordbox "Friday Set"     # dry run
    python3 parity.py --write-rekordbox "Friday Set" --commit
    python3 parity.py --write-rekordbox "Friday Set" --commit --tags
    python3 parity.py --preflight "Friday Set"           # missing files, duplicates

AUTO/PILOT is not written to directly: a row inserted that way is never
analysed, so Parity exports rekordbox XML (cues included) for you to import
there instead.

Reading and writing master.db needs the optional pyrekordbox extra:

    python3 tools/install_extras.py
"""

from __future__ import annotations

import argparse
import socket
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parity.server import serve, serve_forever  # noqa: E402
from parity.store import Store, default_data_dir  # noqa: E402


def free_port(host: str, preferred: int, attempts: int = 12) -> int:
    """Return the first usable port at or after ``preferred``."""
    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise SystemExit(f"No free port found between {preferred} and {preferred + attempts}.")


def extras_hint() -> str:
    """A one-line, actionable note when this interpreter lacks the extra."""
    from parity import rxdb

    if rxdb.extras_status()["available"]:
        return ""
    return (
        f"master.db support is unavailable in this interpreter ({sys.executable}).\n"
        "           Install the optional extra with: python3 tools/install_extras.py"
    )


def connect_rekordbox(
    data_dir: Path | None,
    db_path: Path | None = None,
    fallback_xml: Path | None = None,
    *,
    required: bool = True,
) -> bool:
    """Import the live rekordbox library, falling back to an XML export.

    Called at startup this is *not* fatal: an unreadable database is a note in
    the log, and the UI still opens with whatever library is already stored.
    """
    from parity import rxdb

    try:
        library, origin, detail = rxdb.read_library_or_xml(fallback_xml, db_path)
    except rxdb.MasterDbUnavailable as exc:
        message = f"Could not read the rekordbox library: {exc}"
        if required:
            raise SystemExit(f"{message}\n{extras_hint()}") from exc
        print(f"[parity] {message}", flush=True)
        return False

    store = Store(data_dir)
    if library.source_kind == "xml" and fallback_xml is not None:
        store.set_library(
            library,
            fallback_xml.read_text(encoding="utf-8", errors="replace"),
            source=str(fallback_xml),
            source_kind="xml",
        )
    else:
        store.set_library(library, None, source=library.source, source_kind="db")

    streaming = sum(1 for track in library.tracks.values() if track.is_streaming)
    print(
        f"[parity] {origin}: {len(library.tracks)} tracks "
        f"({len(library.tracks) - streaming} local, {streaming} streaming) "
        f"from {library.source}" + (f" — {detail}" if detail else ""),
        flush=True,
    )
    return True


def saved_set(store: Store, wanted: str) -> dict:
    """One saved set, by id or name, or a message listing what there is."""
    wanted = str(wanted or "").strip().lower()
    for item in store.list_sets():
        if wanted in {str(item.get("id", "")).lower(), str(item.get("name", "")).lower()}:
            return item
    names = ", ".join(f'"{item.get("name")}"' for item in store.list_sets()) or "none saved yet"
    raise SystemExit(f"No saved set called {wanted!r} ({names}). Save one in the UI first.")


def preflight_saved_set(args) -> int:
    """Check a saved set for missing files and duplicate entries.

    Exit status is 1 when something is wrong, so the check can be a step in a
    script -- the report itself is printed either way, because "why" is what the
    user came for.
    """
    from parity.server import Api

    store = Store(Path(args.data_dir).expanduser() if args.data_dir else None)
    record = saved_set(store, args.preflight)
    report = Api(store).preflight(
        {
            "order": record.get("order") or [],
            "cues": not args.no_cues,
            "name": record.get("name") or "",
        }
    )
    print(report["text"], end="")
    return 0 if report["ok"] else 1


def write_saved_set(args) -> int:
    """Write a saved set into rekordbox's library, or report why not."""
    from parity import writedb
    from parity.server import Api

    store = Store(Path(args.data_dir).expanduser() if args.data_dir else None)
    record = saved_set(store, args.write_rekordbox)

    payload = {
        "order": record.get("order") or [],
        "name": record.get("name") or "Parity Set",
        "playlist": args.playlist or record.get("name") or "Parity Set",
        "cues": not args.no_cues,
        "memory": bool(args.memory),
        "tags": bool(args.tags),
        "dry_run": not args.commit,
    }
    if args.master_db:
        payload["db_path"] = args.master_db

    api = Api(store)
    try:
        report = api.write_rekordbox(payload)
    except (writedb.WriteBlocked, writedb.WriteUnavailable) as exc:
        raise SystemExit(f"{exc}") from exc

    where = "dry run" if report["dry_run"] else "committed"
    print(f"[parity] {where}: {payload['playlist']!r} in {report['database']}")
    if report.get("backup"):
        print(f"[parity] backup: {report['backup']}")
    cues = report.get("cues")
    if cues:
        print(
            f"[parity] cues: {cues['created']} rows written, "
            f"{cues['replaced']} replaced, {len(cues['skipped'])} tracks skipped"
        )
        for row in cues["rows"][:10]:
            print(
                f"           {row['slot']} {row['seconds']:>8.3f}s  {row['comment']:<20} "
                f"colourIndex={row['colour_index']:<2} {row['label']}"
            )
        if len(cues["rows"]) > 10:
            print(f"           ... {len(cues['rows']) - 10} more cue rows")
    tags = report.get("tags")
    if tags and (tags["changed"] or tags["unchanged"]):
        print(
            f"[parity] comments: {tags['changed']} tag(s) written, "
            f"{tags['unchanged']} already tagged"
        )
        for row in tags["rows"][:5]:
            print(f"           {row['tag']:<24} {row['label']}")
        if len(tags["rows"]) > 5:
            print(f"           ... {len(tags['rows']) - 5} more comment rows")
    check = report.get("preflight") or {}
    if check:
        counts = check.get("counts") or {}
        print(
            f"[parity] pre-flight: {check.get('checked', 0)} tracks, "
            f"{counts.get('error', 0)} error(s), {counts.get('warning', 0)} warning(s)"
        )
        for issue in (check.get("issues") or [])[:5]:
            print(f"           {issue['detail']} — {issue['label']}")
    playlist = report.get("playlist")
    if playlist:
        print(
            f"[parity] playlist: id={playlist['id']} {playlist['tracks']} tracks, "
            f"{playlist['written']} rows written, {playlist['removed']} removed"
            + (" (created)" if playlist["created"] else "")
        )
    for warning in report.get("warnings") or []:
        print(f"[parity] note: {warning}")
    if not args.commit:
        print("[parity] nothing was written. Re-run with --commit to apply it.")
    return 0


def preload(xml_path: Path, data_dir: Path | None) -> None:
    """Import a rekordbox XML before the server starts."""
    from xml.etree import ElementTree

    from parity import rekordbox

    if not xml_path.is_file():
        raise SystemExit(f"No such file: {xml_path}")
    text = xml_path.read_text(encoding="utf-8", errors="replace")
    try:
        library = rekordbox.parse_collection(text, source=str(xml_path))
    except (ValueError, ElementTree.ParseError) as exc:
        # The API turns this into a 400 with the same words.  On the command
        # line it has to arrive as a message rather than as a parser traceback,
        # because pointing --import at the wrong file is easy to do.
        raise SystemExit(f"Could not read {xml_path.name} as rekordbox XML: {exc}") from exc
    if not library.tracks:
        raise SystemExit("That XML has no tracks. Use File > Export Collection in xml format in rekordbox.")
    store = Store(data_dir)
    store.set_library(library, text, source=str(xml_path))
    print(
        f"[parity] imported {len(library.tracks)} tracks and "
        f"{sum(1 for p in library.playlists if not p.is_folder)} playlists",
        flush=True,
    )


#: Addresses that mean "this machine only".  Anything else is a bind the LAN
#: can reach, which matters because Parity has no password by design.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"}


def is_loopback(host: str) -> bool:
    """Whether ``host`` keeps the server on this machine."""
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parity -- a local harmonic set builder for rekordbox playlists.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    parser.add_argument("--import", dest="import_xml", metavar="XML", help="rekordbox XML to import at startup")
    parser.add_argument(
        "--from-rekordbox",
        action="store_true",
        help="read ~/Library/Pioneer/rekordbox/master.db directly at startup",
    )
    parser.add_argument("--master-db", metavar="PATH", help="rekordbox master.db to read instead of the default")
    parser.add_argument("--data-dir", metavar="DIR", help=f"where to keep local data (default: {default_data_dir()})")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument(
        "--write-status",
        action="store_true",
        help="report whether rekordbox and AUTO/PILOT can be written to right now, then exit",
        # still the one place both libraries are described side by side
    )
    parser.add_argument(
        "--write-rekordbox",
        metavar="SET",
        help="write the saved set SET into master.db (dry run unless --commit)",
    )
    parser.add_argument(
        "--playlist",
        metavar="NAME",
        help="playlist to write or reorder (default: the set's own name)",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="apply a write instead of only reporting what would change",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="also write memory cues (replaces every memory cue on those tracks)",
    )
    parser.add_argument(
        "--tags",
        action="store_true",
        help="also write the set tags ([E7] [peak] 14/24) into each track's comment",
    )
    parser.add_argument(
        "--preflight",
        metavar="SET",
        help="check a saved set for missing audio files and duplicate entries, then exit",
    )
    parser.add_argument("--no-cues", action="store_true", help="write playlist order only, no cues")
    parser.add_argument(
        "--no-connect",
        action="store_true",
        help="do not read the rekordbox library at startup (use --import instead)",
    )
    args = parser.parse_args(argv)

    if args.write_status:
        import json

        from parity import autopilot, writedb

        print(json.dumps({"rekordbox": writedb.status(), "autopilot": autopilot.status()}, indent=2))
        return 0

    if args.write_rekordbox:
        return write_saved_set(args)

    if args.preflight:
        return preflight_saved_set(args)

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
    fallback_xml = Path(args.import_xml).expanduser() if args.import_xml else None
    explicit = args.from_rekordbox or args.master_db
    if fallback_xml and not explicit:
        preload(fallback_xml, data_dir)
    elif not args.no_connect:
        # Read the live library at startup, so the UI never opens behind a
        # button.  A database that cannot be read is a note, not a crash: the
        # stored library is still there to work from.
        connect_rekordbox(
            data_dir,
            Path(args.master_db).expanduser() if args.master_db else None,
            fallback_xml,
            required=explicit,
        )

    port = free_port(args.host, args.port)
    if port != args.port:
        print(f"[parity] port {args.port} is busy, using {port}")

    hint = extras_hint()
    if hint:
        print(f"[parity] note: {hint}", flush=True)

    # A 0.0.0.0 bind is not an address a browser can open, and it is worth
    # saying out loud that the server is now reachable from the network: there
    # is no auth, and the API can import a library, build a set and write into
    # master.db.
    openable = args.host if is_loopback(args.host) else "127.0.0.1"
    if not is_loopback(args.host):
        print(
            f"[parity] warning: binding to {args.host} lets any machine that can "
            "reach this one use Parity, and it has no password -- the API can "
            "import a library and write to your rekordbox database.  Use "
            "127.0.0.1 unless you meant this.",
            flush=True,
        )
    url = f"http://{openable}:{port}"
    if args.no_browser:
        serve_forever(args.host, port, data_dir)
        return 0

    httpd = serve(args.host, port, data_dir)
    print(f"[parity] running at {url} (Ctrl+C to stop)", flush=True)
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[parity] stopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
