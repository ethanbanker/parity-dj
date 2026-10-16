#!/usr/bin/env python3
"""Install Parity's optional extras (currently just pyrekordbox).

    python3 tools/install_extras.py                 # into your user site
    python3 tools/install_extras.py --venv /tmp/x   # somewhere else
    python3 tools/install_extras.py --current       # into this interpreter
    python3 tools/install_extras.py --check         # report, install nothing

The core of Parity needs nothing but the standard library.  pyrekordbox is
only required for the direct rekordbox database features -- reading
``master.db`` and the ANLZ analysis files beside it.

Homebrew's Python (and python.org's) refuses a plain ``pip install`` outside a
virtualenv.  The default here is therefore the **user site** of the interpreter
running this script (``~/.local/lib/...``), installed with
``--break-system-packages`` -- exactly what PEP 668's own message recommends.  It
is per-user, needs no sudo, and ``pip uninstall`` reverses it.

That matters for the macOS app: the app launches whatever ``python3`` it finds,
and a virtualenv inside a protected folder such as ``~/Downloads`` cannot be used
by a Finder-launched app (macOS blocks the read, and the interpreter hangs during
startup).  The user site is never blocked.  ``--venv DIR`` is still available for
full isolation, e.g. under ``~/Library/Application Support``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT_ROOT / "requirements-extras.txt"
DEFAULT_VENV = PROJECT_ROOT / ".venv"


def pip(python: Path, extra_args: list[str], upgrade: bool) -> int:
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    if upgrade:
        command.append("--upgrade")
    command += extra_args + ["-r", str(REQUIREMENTS)]
    print("$ " + " ".join(command), flush=True)
    return subprocess.call(command)


def ensure_venv(directory: Path) -> Path:
    python = directory / "bin" / "python3"
    if not python.exists():
        print(f"creating virtualenv at {directory}", flush=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(directory)])
    return python


def verify(python: Path) -> bool:
    code = (
        "import sys;"
        "from importlib.metadata import version;"
        "import pyrekordbox;"
        "print('pyrekordbox', version('pyrekordbox'), 'on', sys.executable)"
    )
    result = subprocess.run([str(python), "-c", code], capture_output=True, text=True)
    if result.returncode == 0:
        print(result.stdout.strip())
        return True
    print(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "not importable")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Parity's optional extras.")
    parser.add_argument(
        "--venv",
        metavar="DIR",
        nargs="?",
        const=str(DEFAULT_VENV),
        help="create/use a virtualenv instead of the user site (a target path is optional)",
    )
    parser.add_argument("--check", action="store_true", help="only report whether the extras are importable")
    parser.add_argument("--no-upgrade", action="store_true", help="do not pass --upgrade to pip")
    args = parser.parse_args()

    if args.check:
        python = ensure_venv(Path(args.venv)) if args.venv else Path(sys.executable)
        return 0 if verify(Path(python)) else 1

    if not REQUIREMENTS.is_file():
        print(f"missing {REQUIREMENTS}", file=sys.stderr)
        return 2

    upgrade = not args.no_upgrade
    if args.venv:
        python = ensure_venv(Path(args.venv).expanduser())
        status = pip(python, [], upgrade)
    else:
        python = Path(sys.executable)
        target = Path(
            subprocess.run(
                [str(python), "-c", "import site; print(site.getusersitepackages())"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "the user site"
        )
        print(f"installing into the user site for {python}\n  -> {target}")
        status = pip(python, ["--user", "--break-system-packages"], upgrade)
        if status != 0:
            print("retrying without --break-system-packages...", flush=True)
            status = pip(python, ["--user"], upgrade)

    if status != 0:
        print(
            "\npip failed. Parity's core still works (XML import, building, exporting);"
            " only master.db support is unavailable.",
            file=sys.stderr,
        )
        return status

    if not verify(python):
        return 1

    if python != Path(sys.executable):
        print(
            "\nUse that interpreter for the database features:\n"
            f"    {python} parity.py\n"
        )
    print("Parity can now read master.db and the ANLZ analysis files directly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
