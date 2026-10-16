#!/usr/bin/env python3
"""Generate a synthetic rekordbox XML library.

Handy for trying Parity without exporting a real collection, and for testing
at scale.

    python3 tools/make_sample_library.py --tracks 3000 --out /tmp/big.xml
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

GENRES = [
    ("Deep House", 118, 124, 4.2),
    ("Tech House", 122, 128, 6.6),
    ("Progressive House", 120, 126, 6.0),
    ("Melodic House", 118, 124, 5.4),
    ("Minimal", 122, 128, 4.6),
    ("Techno", 128, 136, 8.0),
    ("Peak Time Techno", 132, 140, 9.2),
    ("Hard Techno", 138, 146, 9.6),
    ("Drum & Bass", 172, 176, 8.6),
    ("Ambient", 70, 100, 1.8),
]
KEYS = [
    "Am", "Em", "Bm", "F#m", "C#m", "G#m", "D#m", "A#m", "Fm", "Cm", "Gm", "Dm",
    "C", "G", "D", "A", "E", "B", "F#", "C#", "Ab", "Eb", "Bb", "F",
]
FIRST = ["Marlow", "Kilma", "Ari Vex", "Soha", "Nils Ou", "Ferra", "Bosko", "Ines",
         "Tessellate", "Odd Mob", "Rivet", "Halcyon", "Dusk Line", "Cobalt", "Vera June"]
SECOND = ["Intro", "Basement", "Pressure", "Night Bus", "Rivet", "Cold Room", "Signal",
          "Low Ceiling", "Ulterior", "Peak Hour", "Static Bloom", "Afterglow", "Marble",
          "Runout", "Grey Area", "Slow Burn", "Undertow", "Lattice"]


def esc(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def build(tracks: int, playlists: int, seed: int) -> str:
    rng = random.Random(seed)
    rows = []
    used_titles: set[str] = set()

    for index in range(1, tracks + 1):
        genre, low, high, base = rng.choice(GENRES)
        artist = f"{rng.choice(FIRST)} {rng.choice(['', 'Crew', 'Duo', 'B2B'])}".strip()
        base_title = f"{rng.choice(SECOND)} {rng.choice(['', 'II', 'III', 'Dub', 'Reprise'])}".strip()
        title = base_title
        version = 1
        while title in used_titles:
            version += 1
            title = f"{base_title} ({rng.choice(['One', 'Two', 'Redux', 'Edit'])} {version})"
        used_titles.add(title)

        stars = max(0, min(5, int(round((base - 3.5) / 1.1 + rng.gauss(0, 1)))))
        rating = min(255, max(0, int(round(stars * 51))))
        bpm = round(rng.uniform(low, high), 2)
        key = rng.choice(KEYS)
        seconds = int(rng.uniform(180, 480))

        comments = ""
        if rng.random() < 0.18:
            comments = f"E{max(1, min(10, int(round(base + rng.gauss(0, 0.8)))))}"
        elif rng.random() < 0.08:
            comments = f"[{max(1, min(10, int(round(base))))}/10]"

        location = f"file://localhost/Users/dj/Music/sample/{artist.replace(' ', '%20')}%20-%20{title.replace(' ', '%20')}.aiff"
        rows.append(
            f'    <TRACK TrackID="{1000 + index}" Name="{esc(title)}" Artist="{esc(artist)}" '
            f'Album="{esc(rng.choice(["White Label", "Harbour", "Nocturne", "Cut Records", "Field Notes"]))}" '
            f'Genre="{esc(genre)}" Kind="AIFF File" Size="{rng.randint(20_000_000, 60_000_000)}" '
            f'TotalTime="{seconds}" AverageBpm="{bpm:.2f}" Tonality="{esc(key)}" Rating="{rating}" '
            f'Comments="{esc(comments)}" PlayCount="{rng.randint(0, 40)}" '
            f'DateAdded="{rng.randint(2019, 2026)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}" '
            f'Location="{location}" Year="{rng.randint(2015, 2026)}" '
            f'Label="{esc(rng.choice(["Harbour", "Nocturne", "Cut", "Field Notes"]))}"/>'
        )

    ids = list(range(1001, 1001 + tracks))
    rng.shuffle(ids)
    chunks = [ids[i::playlists] for i in range(playlists)]
    nodes = []
    for number, chunk in enumerate(chunks[:playlists], start=1):
        track_nodes = "\n".join(f'        <TRACK Key="{tid}"/>' for tid in chunk)
        nodes.append(
            f'      <NODE Name="Crate {number}" Type="1" KeyType="0" Entries="{len(chunk)}">\n'
            f"{track_nodes}\n      </NODE>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<DJ_PLAYLISTS Version="1.0.0">\n'
        '  <PRODUCT Name="rekordbox" Version="6.8.1" Company="AlphaTheta"/>\n'
        f'  <COLLECTION Entries="{len(rows)}">\n' + "\n".join(rows) + "\n  </COLLECTION>\n"
        "  <PLAYLISTS>\n"
        f'    <NODE Type="0" Name="ROOT" Count="{playlists}">\n'
        + "\n".join(nodes)
        + "\n    </NODE>\n"
        "  </PLAYLISTS>\n"
        "</DJ_PLAYLISTS>\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic rekordbox XML library.")
    parser.add_argument("--tracks", type=int, default=1000, help="how many tracks (default: 1000)")
    parser.add_argument("--playlists", type=int, default=8, help="how many playlists to split into")
    parser.add_argument("--out", default="sample-library.xml", help="where to write it")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    xml = build(max(1, args.tracks), max(1, args.playlists), args.seed)
    path = Path(args.out).expanduser()
    path.write_text(xml, encoding="utf-8")
    print(f"wrote {args.tracks} tracks to {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
