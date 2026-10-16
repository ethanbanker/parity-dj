"""Tests for the command line entry point, ``parity.py``.

The module is run rather than imported twice for the same reason a user runs it:
``import parity`` is the package, so the CLI is exercised the way it is used --
as a program, with a file it was handed and an exit status to look at.  Only the
address helper is imported directly, because a test that binds a real socket to
a LAN address to prove a warning is a test that opens a port.

Nothing here starts a server: every case stops at an error the CLI reports
before it would.
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "collection.xml"

#: The shortest "billion laughs": three entities that expand into millions of
#: characters when the last one is referenced.
ENTITY_XML = """<?xml version="1.0"?>
<!DOCTYPE DJ_PLAYLISTS [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]>
<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0">&c;</COLLECTION></DJ_PLAYLISTS>
"""


def load_cli():
    """The ``parity.py`` at the repository root, loaded under its own name.

    ``import parity`` finds the package next to it, so the CLI is loaded from
    its path instead of by name.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("parity_cli", ROOT / "parity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoopbackTest(unittest.TestCase):
    """``--host`` decides whether the server is reachable from the network."""

    def setUp(self):
        self.cli = load_cli()

    def test_the_local_addresses_are_recognised(self):
        for host in ("127.0.0.1", "localhost", "LOCALHOST", "::1", " 127.0.0.1 "):
            self.assertTrue(self.cli.is_loopback(host), host)

    def test_the_network_addresses_are_not(self):
        # 0.0.0.0 is every interface, and a LAN address is one of them: both
        # mean the server has no protection but the network it sits on.
        for host in ("0.0.0.0", "192.168.1.20", "10.0.0.5", "", "example.local"):
            self.assertFalse(self.cli.is_loopback(host), host)


class ImportErrorTest(unittest.TestCase):
    """A file that cannot be read fails with a message and a non-zero status."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "parity.py"), "--data-dir", str(self.dir / "data"), *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=60,
        )

    def test_entities_are_refused_with_a_message_not_a_traceback(self):
        evil = self.dir / "hostile.xml"
        evil.write_text(ENTITY_XML, encoding="utf-8")
        result = self.run_cli("--import", str(evil))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("entities", result.stdout + result.stderr)

    def test_a_file_that_is_not_xml_is_reported_the_same_way(self):
        junk = self.dir / "collection.xml"
        junk.write_text("this is not an export", encoding="utf-8")
        result = self.run_cli("--import", str(junk))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("Could not read collection.xml as rekordbox XML", result.stdout + result.stderr)

    def test_a_missing_file_is_reported_by_name(self):
        result = self.run_cli("--import", str(self.dir / "nope.xml"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("No such file", result.stdout + result.stderr)

    def test_a_real_export_is_imported(self):
        """The one case that gets past the parse, and then serves.

        ``--import`` writes the library and starts the server, so this waits for
        the store to appear and stops the process itself rather than waiting for
        a program that is meant to keep running.  ``--no-browser`` is not about
        the window: a test that opens one is a test that takes over the desktop.
        """
        store = self.dir / "data" / "store.json"
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "parity.py"),
                "--data-dir", str(self.dir / "data"),
                "--import", str(FIXTURE),
                "--no-browser",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=ROOT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not store.exists():
                time.sleep(0.1)
        finally:
            process.terminate()
            process.wait(timeout=15)

        self.assertTrue(store.exists(), "the CLI never wrote the imported library")
        data = json.loads(store.read_text(encoding="utf-8"))
        self.assertEqual(data["library"]["track_count"], 12)
        self.assertIn("Friday Set", store.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
