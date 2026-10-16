#!/usr/bin/env python3
"""Build ``Parity.app`` -- a native macOS app, no terminal needed.

    python3 tools/build_macos_app.py                  # build dist/Parity.app
    python3 tools/build_macos_app.py --install --run  # put it in ~/Applications and open it

The app is a small Swift/Cocoa shell (tools/macos/main.swift) around the same
Python server the CLI runs. Double-clicking it starts that server as a child
process and shows the UI in a real window: Dock icon, menu bar, Cmd-Q, and
quitting stops the server.

The Python code is copied into the bundle, so the app is self-contained and can
live in /Applications. Re-run this script after changing Parity's code.
"""

from __future__ import annotations

import argparse
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SWIFT_SOURCE = Path(__file__).resolve().parent / "macos" / "main.swift"
APP_NAME = "Parity"
BUNDLE_ID = "co.parity.launcher"
# Mirrors tools/macos/main.swift: where the app keeps its data and logs.
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
VERSION = "1.0.0"
MIN_MACOS = "13.0"


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(
            f"command failed: {' '.join(command)}\n{result.stdout.strip()}\n{result.stderr.strip()}"
        )
    return result


def python_candidates() -> list[str]:
    """Interpreters to try at runtime, most specific first.

    A Finder-launched app gets a minimal PATH, so absolute paths are baked into
    the app's Info.plist.  The supporting interpreter first, then the running one.

    Note: do not point this at a virtualenv under a protected folder such as
    ``~/Downloads`` -- a Finder-launched app cannot read it, and the interpreter
    hangs during startup.  ``tools/install_extras.py`` installs the optional
    pyrekordbox extra into the user site instead, which is never blocked.
    """
    found: list[str] = []
    for candidate in (
        str(SUPPORT_DIR / "venv" / "bin" / "python3"),
        sys.executable,
        shutil.which("python3"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
        "/usr/bin/python3",
    ):
        if candidate and candidate not in found:
            found.append(candidate)
    return found


def swift_toolchain() -> str:
    swiftc = shutil.which("swiftc")
    if not swiftc:
        result = run(["/usr/bin/xcrun", "--find", "swiftc"], check=False)
        swiftc = result.stdout.strip() if result.returncode == 0 else None
    if not swiftc:
        raise SystemExit(
            "swiftc not found. Install Apple's developer tools with:\n"
            "    xcode-select --install\n"
            "then run this script again."
        )
    return swiftc


def compile_app(binary: Path) -> str:
    swiftc = swift_toolchain()
    machine = platform.machine() or "arm64"
    target_arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
    run(
        [
            swiftc,
            "-swift-version", "5",
            "-O",
            "-target", f"{target_arch}-apple-macosx{MIN_MACOS}",
            "-framework", "Cocoa",
            "-framework", "WebKit",
            "-o", str(binary),
            str(SWIFT_SOURCE),
        ]
    )
    return target_arch


def copy_payload(payload: Path) -> None:
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "parity.py", payload / "parity.py")
    shutil.copytree(
        PROJECT_ROOT / "parity",
        payload / "parity",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(PROJECT_ROOT / "README.md", payload / "README.md")


def write_info_plist(app: Path) -> None:
    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleIconFile": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSApplicationCategoryType": "public.app-category.music",
        "LSMinimumSystemVersion": MIN_MACOS,
        "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True,
        # The app talks to its own loopback server over plain HTTP.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "ParityPythonCandidates": python_candidates(),
    }
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)


def sign(app: Path) -> str:
    result = run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=False)
    if result.returncode == 0:
        return "ad-hoc signed"
    last = (result.stderr or result.stdout).strip().splitlines()
    return f"codesign failed: {last[-1] if last else 'unknown'}"


def pin_to_dock(app: Path) -> str:
    """Add the app to the Dock without disturbing existing items."""
    import tempfile

    exported = run(["/usr/bin/defaults", "export", "com.apple.dock", "-"], check=False)
    if exported.returncode != 0:
        return "could not read the Dock preferences"

    with tempfile.NamedTemporaryFile("w+", suffix=".plist", delete=False) as handle:
        handle.write(exported.stdout)
        temp_path = Path(handle.name)
    try:
        with temp_path.open("rb") as fh:
            dock = plistlib.load(fh)
        persistent = dock.setdefault("persistent-apps", [])
        url = f"file://{app}/"
        if any(url in str(entry) for entry in persistent):
            return "already in the Dock"
        persistent.append(
            {
                "tile-data": {"file-data": {"_CFURLString": url, "_CFURLStringType": 15}},
                "tile-type": "file-tile",
            }
        )
        with temp_path.open("wb") as fh:
            plistlib.dump(dock, fh)
        run(["/usr/bin/defaults", "import", "com.apple.dock", str(temp_path)])
        run(["/usr/bin/killall", "Dock"], check=False)
        return "added to the Dock"
    finally:
        temp_path.unlink(missing_ok=True)


def build(output_dir: Path, icon_size: int) -> Path:
    if sys.platform != "darwin":
        raise SystemExit("Building a .app needs macOS.")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from make_icon import build_icns

    output_dir.mkdir(parents=True, exist_ok=True)
    app = output_dir / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir(parents=True)

    print("[1/5] drawing the icon…")
    icns, _png = build_icns(PROJECT_ROOT / "build" / "icon", icon_size)
    shutil.copy2(icns, app / "Contents" / "Resources" / f"{APP_NAME}.icns")

    # Compile outside the bundle first: the linker is much happier writing into
    # a plain directory than into a .app bundle.
    print("[2/5] compiling the app (swiftc)…")
    staging = PROJECT_ROOT / "build" / "swift"
    staging.mkdir(parents=True, exist_ok=True)
    binary = staging / APP_NAME
    target_arch = compile_app(binary)
    print(f"      built for {target_arch}, macOS {MIN_MACOS}+")
    shutil.copy2(binary, app / "Contents" / "MacOS" / APP_NAME)

    print("[3/5] copying the Parity server into the bundle…")
    copy_payload(app / "Contents" / "Resources" / "Parity")

    print("[4/5] writing Info.plist…")
    write_info_plist(app)

    print("[5/5] signing…")
    print("      " + sign(app))
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Parity.app for macOS.")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "dist"), help="where to put the .app")
    parser.add_argument("--icon-size", type=int, default=1024, help="master icon render size")
    parser.add_argument("--install", action="store_true", help="copy the app to ~/Applications")
    parser.add_argument("--system", action="store_true", help="install into /Applications instead")
    parser.add_argument("--pin-dock", action="store_true", help="add the app to the Dock")
    parser.add_argument("--run", action="store_true", help="launch the app when built")
    args = parser.parse_args()

    app = build(Path(args.output).expanduser(), args.icon_size)
    print(f"\nBuilt {app}")

    installed = app
    if args.install or args.system:
        destination_dir = Path("/Applications") if args.system else Path.home() / "Applications"
        destination_dir.mkdir(parents=True, exist_ok=True)
        installed = destination_dir / app.name
        if installed.exists():
            shutil.rmtree(installed)
        shutil.copytree(app, installed, symlinks=True)
        print(f"Installed to {installed}")

    if args.pin_dock:
        print("Dock: " + pin_to_dock(installed))

    if args.run:
        run(["/usr/bin/open", str(installed)])
        print(f"Launched {installed.name}")

    print(
        "\nDouble-click it in Finder, or drag it into the Dock.\n"
        "It starts its own server and shows the UI in an app window; quitting the app stops the server.\n"
        "Re-run this script after changing Parity's code."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
