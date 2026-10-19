# Parity

A free, local DJ set builder for **rekordbox** libraries.

Read your rekordbox library — straight from `master.db`, no XML export needed —
pick a playlist or a folder on disk, purge everything that isn't a local file,
order what's left along an energy curve while respecting the **Camelot wheel**
and keeping tempo jumps mixable, place eight hot cues across the intro, build,
drop, breakdown and mix-out, fill in the BPM, key and beat grid rekordbox has
not analysed, tidy duplicates and metadata, take an M3U or a watched folder as
a library, manage both libraries from the window, and export something both
rekordbox and AUTO/PILOT can import — or a printable HTML/PDF set list.

Everything runs on your machine. No account, no upload, no network access at all:
the server binds to `127.0.0.1` only, and your library never leaves the computer.

The name is the whole idea, in one shape: a ring cut into two equal halves. Two
tracks that are level with each other is what a mix is, and it is what the
toolbar, the icon and the two accent colours — cyan for the curve you asked for,
coral for the set that came out — are drawn from.

```
python3 parity.py
```

The core is Python 3.9+ and the standard library, with no `pip install` at all.
Reading `master.db` needs one optional extra (see below); without it Parity
degrades cleanly to the XML-export path.

---

## Quick start

**Straight from your library** (recommended):

```
python3 tools/install_extras.py     # once: installs pyrekordbox for your user
python3 parity.py
```

1. **It reads your library on the way up.** Parity loads
   `~/Library/Pioneer/rekordbox/master.db` at startup, so the window opens on the
   live library with nothing to click. Add `--no-connect` if you would rather it
   did not (and `--import` to work from an XML export instead).
2. **Pick the folder or playlist** you're building from (e.g. a `Syd Bday` folder
   on an external SSD).
3. **Tick "Local files only"** — streaming stubs and broken paths are dropped,
   with a count of what went and why.
4. **Analyse cues** if you want rekordbox's phrase structure turned into eight
   hot cues (A–H) and memory cues.
5. **Choose a shape, hit "Build set"**, then fine-tune by hand.
6. **Write ▾ → Dry run** to see every database row the write would touch, then
   **Write ▾ → rekordbox master.db** (quit rekordbox first) — or **Export →**
   **AUTO/PILOT bundle** / **rekordbox XML** if you would rather import a file.
7. **Files & libraries ▾** (topbar) → **Manage libraries** renames or deletes
   playlists in rekordbox and AUTO/PILOT, and deletes the built set's tracks from
   either — each as a dry run you confirm. Saved sets are deleted from the same
   window. The one button holds both file panels; the other entry is
   **Rename & convert** (see below).

AUTO/PILOT has no import format of its own, so **Export → Send to AUTO/PILOT**
writes the rekordbox XML to `~/Downloads` for you to import there; that import is
what makes AUTO/PILOT analyse the tracks, which a row written into its database
behind its back never gets.

**From an XML export** is the fallback when `master.db` cannot be read — a
machine without the pyrekordbox extra on it, say. It has no button of its own
any more: the database is the normal path, and offering a step nobody needs is
one more thing to explain, so an export is loaded by **dragging the `.xml` onto
the Parity window**.

1. **In rekordbox:** `File → Export Collection in xml format` and save the `.xml`.
2. **Drag the file onto the window.** It loads as the library, and the status
   line says where the library came from.

Command line options:

```
python3 parity.py                            # start the UI
python3 parity.py --from-rekordbox           # preload master.db on start
python3 parity.py --from-rekordbox --import ~/rekordbox.xml   # db, XML fallback
python3 parity.py --master-db /path/master.db  # non-standard database
python3 parity.py --import ~/rekordbox.xml   # preload a collection on start
python3 parity.py --port 9000 --no-browser   # different port, no browser
python3 parity.py --data-dir /somewhere      # where local data is kept
python3 parity.py --write-status                     # can a write run right now?
python3 parity.py --write-rekordbox "Friday Set"      # dry run of a saved set
python3 parity.py --write-rekordbox "Friday Set" --commit
python3 parity.py --write-rekordbox "Friday Set" --commit --playlist "Syd BDAY"
python3 parity.py --write-rekordbox "Friday Set" --commit --tags   # also tag the comments
python3 parity.py --preflight "Friday Set"           # missing files, duplicates
```

The write commands work off the saved sets in the local store, so point them at
the one the app uses when you want to write a set you built in the window:
`--data-dir ~/Library/Application\ Support/Parity`. A write is a dry run unless
you pass `--commit`, and it refuses to run while the target app is open
(`--write-status` says which process is in the way). `--preflight` checks a saved
set and exits `1` when something is wrong with it, so it can be a step in a
script; the report prints either way.

## Reading your rekordbox database

rekordbox 6/7 keeps everything in an SQLCipher-encrypted SQLite database at
`~/Library/Pioneer/rekordbox/master.db`. That one file has what an XML export
lacks: live playlists and folders, which entries are streaming stubs, and the
ANLZ analysis files (beat grid and phrase map) rekordbox wrote for every track.

```
python3 tools/install_extras.py            # install into your user site
python3 tools/install_extras.py --check    # just report what is available
python3 tools/install_extras.py --venv DIR # isolated virtualenv instead
```

Homebrew's and python.org's interpreters refuse a plain `pip install` outside a
virtualenv, so the installer uses the interpreter's **user site**
(`~/Library/Python/3.x/lib/python/site-packages`) with
`--break-system-packages` — per-user, no sudo, reversible with `pip uninstall`,
and exactly what PEP 668's error message recommends.

It is deliberately *not* a virtualenv in this project folder. A virtualenv inside
a protected location such as `~/Downloads` cannot be read by a Finder-launched
app, and the interpreter hangs during startup instead of failing loudly — so
`tools/install_extras.py` keeps the dependency somewhere both the CLI and the app
can reach. `parity.py` prints a reminder when the running interpreter lacks it,
and the app falls back to an XML import when the extra is missing.

**Your database is never opened in place.** Opening it lets SQLCipher/SQLAlchemy
replay and checkpoint the write-ahead log, which rewrites the file — risky while
rekordbox is running. Parity copies `master.db` (plus its `-wal`/`-shm`
siblings and `masterPlaylists6.xml`) into a temporary folder, reads the copy, and
deletes it. The live library is read-only, always.

Missing the extra, or a database that cannot be unlocked? `/api/rekordbox`
reports exactly why, and connecting falls back to the XML export you pick.

## The Mac app (no terminal)

There is a real Mac app too: double-click it, no terminal at all.

```
python3 tools/build_macos_app.py --install --run
```

That builds `Parity.app` and copies it to `~/Applications`. From then on you
just double-click it in Finder (or drag it to the Dock and click that). It is a
native Cocoa app: its own window with the UI inside, a menu bar, Cmd-Q, and a
proper Dock icon.

What it does when you open it:

1. Starts the local server as its own child process.
2. Shows the UI in an app window (not a browser tab).
3. Keeps the server alive while the app is open.

The app always runs *its own* server and stops it when you quit. If port 8765 is
already taken (say you have `python3 parity.py` running), it takes the next free
port instead — it never adopts another server, because that server could be
running older code or a different data folder. Your own server is left alone.
Its data and logs live in `~/Library/Application Support/Parity/`; a server left
behind by a crash is cleaned up on the next launch. That folder is named after
the app, so the rename means it is a *new* one: anything built under the old name
(sets saved in the window, energy overrides, the cached library) stays in the old
`Application Support` folder and is not read from there. A saved set from before
the rename has to be rebuilt or copied across by hand.

Inside the app window, the **Choose…** buttons beside the **Rename & convert**
paths open the normal macOS folder chooser, and **Export** writes the file
straight to `~/Downloads` and tells you the path (a web view cannot use the
browser's download button, so the server saves it directly). In a browser,
exports work the usual way, but a folder has to be typed: a page is never told
the path of a folder you picked, so there is nothing for the button to fill in.

The Python code is copied *into* the bundle, so the app is self-contained and can
live in `/Applications`. That also means **you must re-run the build script after
changing Parity's code**. Building needs Apple's developer tools
(`xcode-select --install`) for `swiftc`; running the app needs nothing extra.

The app launches the first usable interpreter from `ParityPythonCandidates` in
its `Info.plist` — the supporting interpreter, then the one you built with. Run
`python3 tools/install_extras.py` before building and the app reads `master.db`
at launch too (`/api/rekordbox` reports which interpreter is in use).

## Rename & convert a folder before you import it

A download folder is where most libraries start, and it arrives named for a web
page: `01_Ada_Kaleh_-_Night_Ferry_🎵_[Official_Video]_[FREE_DL]_320kbps.mp3`.
That is not a name a CDJ's one-line display can be read from, and it is not a
name an exFAT stick will always take. **Files & libraries ▾ → Rename & convert**
opens the panel that fixes this *before* rekordbox sees any of it:

1. Point Parity at the folder the downloads land in — **Choose…** in the app
   window, or type the path — pick **AIFF** or **WAV**, and press **Scan
   folder**.
2. Read the report. It lists every file, what it would be called, what changed and
   why — `underscores became spaces`, `removed the tag “[Official Video]”`,
   `“/” is a path separator and became “-”`, `“CON” is a reserved device name`,
   `CON is already used here, so this one became CON (2)` — plus the leading
   silence of each file, measured, because that is where its cue points sit.
3. Press **Convert**. The tidied files are written to the destination and the
   originals are left exactly where they were.

What it does to a name: illegal characters (`/ \ | : * ? " < >`, control and
invisible characters) become something a filesystem will take; emoji, dingbats
and private-use symbols are dropped; the junk a download site adds —
`[Official Video]`, `(Lyric Video)`, `[FREE DL]`, `www.…`, `320kbps`, `HQ` — goes;
underscores and spaced hyphens are normalised; names too long for a 255-byte
limit are shortened; a Windows device name is moved out of the way; and two files
that tidy to the same thing are numbered apart. Letters in *any* alphabet are
kept: accents, Greek, Cyrillic and CJK are names, not junk.

The conversion is where the honest part is. `ffmpeg` re-encodes to uncompressed
PCM (`pcm_s16be` inside AIFF, `pcm_s16le` inside WAV, at the source's own rate)
and carries the tags across with `-map_metadata 0` — an ID3 chunk for AIFF, which
the muxer only writes when asked, so artist/album/genre survive rather than the
container quietly keeping the title alone. Every converted file then reports
**what it starts and ends with**: an MP3 carries encoder delay and end padding, so
the same track as WAV and as MP3 do not put a cue point at the same timecode, and
a file that begins 250 ms late says so on the screen. Off by default, **Trim
silence** cuts that lead during the conversion.

Nothing is written twice and nothing is lost: files already in the target format
are copied rather than converted, a file already in the destination is skipped
and reported instead of overwritten, a converted file takes the extension of the
format you chose (an `.mp3` that becomes AIFF is written `.aiff`), subfolders are
mirrored, and artwork travels where the container can hold it — an MP3's cover
comes across into AIFF; where it cannot (WAV), the report says so on the rows
where the source had it, rather than leaving you to find out.

## Starting from an M3U or a watch folder

A library does not have to begin in rekordbox. **Files & libraries ▾ → Watch
folder & M3U** does both jobs:

* **An .m3u or .m3u8** becomes a playlist. `#EXTINF` supplies the length and the
  `artist - title`, relative paths resolve against the playlist's own folder,
  and a file the library already has is matched by its absolute path so an
  import never adds a second row for it. A streaming URL is kept as a
  streaming row — the pool already knows it has no file to play. Missing paths
  are counted and named, not silently dropped; the tracks that do exist are
  imported.
* **A watch folder** is the same import on a timer. Point it at the folder new
  downloads land in, tick **Watch it**, and each new audio file is probed with
  ffprobe, added to the library and appended to a playlist named for the folder
  (`Watch: incoming`). Nothing is moved, converted or written to a DJ app;
  files that disappear stay in the library rather than being deleted behind
your back.

Both work when Parity has no library at all, so a folder of downloads can be
built into a set without exporting anything from rekordbox first. A dropped
M3U is read in the browser: absolute paths work, while relative ones need the
folder they are relative to.

## Measuring tempo, key and the beat grid

**Analyse audio** is the local answer to a track rekordbox has never analysed.
It decodes the first two minutes with ffmpeg and measures, with numpy:

| What | How |
|------|-----|
| **BPM** | the strongest period in the onset envelope's autocorrelation, then a least-squares line through the onsets that land on the grid — the autocorrelation finds the ballpark, the regression gets 128 BPM to 128.0 rather than 129.2 |
| **Key** | a chroma vector correlated against the Krumhansl-Schmuckler major and minor profiles in all twelve rotations, with a bonus when the candidate's tonic is the lowest strong bass note |
| **Beat grid** | that tempo's period plus the phase that puts the most onset energy on the beat |

The library's own BPM and key always win; the measurement fills what is
missing and is reported beside it (`analysed_bpm`, `analysed_key`). On the
reference synthetic checks the tempo lands within 0.2 BPM from 90 to 174, and
major and minor triads map to the right Camelot code. The result is cached by
file (path, size, mtime) and model, so a second run only reads what changed.
A track with no tonal centre (a bare kick and hats) reports no key rather than
inventing one, and a weak pulse says the tempo is a guess.

The grid is one tempo for the whole track; a track that changes tempo is not
split into segments, and the direct `master.db` write does not rewrite BPM
(the measurement is in the export's `<TEMPO>` and in Parity's own pool).

## What it does

**Build along an energy curve.** Pick `Journey`, `Peak time`, `Warm up`,
`Festival`, `Cool down`, `Chill`, `Flat`… or drag the curve's control points on
the chart to draw your own. The builder picks the group of tracks that fits the
curve best *as a group* and places them by rank — quietest target gets the
quietest track — instead of filling slot by slot, which spends the pool's only
quiet track on a slot that could have used anything. On the reference library
the `Chill` curve went from 0.67 energy points of average miss and a shape
correlation of 0.70 to **0.33 and 0.95**.

**And it tells you when the pool is the problem.** The chart shades the energy
range your pool can actually reach, and a build says what stopped it: *"The
curve opens at E2.5 but the quietest track in the pool is E2.8 (Levity — Flip It),
so the set cannot start that low"*, *"19 of 20 tracks take their energy from BPM
alone — type real values into the Energy column and rebuild"*. The stats line
carries `Curve ±0.33 (worst 0.57)`, so a build that follows the curve and one
that could not look different.

**Mix in key.** Every track is mapped onto the Camelot wheel, and each transition
is scored: same key, relative major/minor, one step around the wheel (safe), the
diagonal move (lifts the mood), two steps (a lift you feel), or a clash. The
verdict is shown between every pair of tracks, live, including after you drag
things around. Strictness controls how hard the builder fights for it —
`Strict` refuses a clash *and* the two-steps below a diagonal, reworking the
places the search left weak (see *How the builder works*), and if the pool has
nothing that bridges a gap in the wheel it names the pair it could not fix
rather than leaving you with a percentage.

**Keep it beatmatched.** Transitions are penalised when the BPM jump exceeds your
tolerance, and half/double-time pairs (87 ↔ 174) are recognised as matching.

**Stay varied.** Same artist within N tracks is penalised, so sets don't become
three tracks by the same producer in a row.

**Preview before you commit.** Every row — in the pool and in the set — has a ▶
button. One shared player sits in the topbar, so a second click moves playback
instead of layering a second track over the first: the title, energy, BPM and key
of what you are hearing, a progress bar you can read, ↺/↻ for ten seconds and ✕
to stop. Streaming-only rows show the button greyed out rather than failing on
click, and a file that has gone missing says so by name.

The transport is keyboard-driven while you work: **Space** play/pause, **[** and
**]** to jump to the previous or next hot cue, **Esc** to stop. Once you have run
*Analyse cues*, the player shows a button per hot cue — A–H — so you can hear
the build and the drop of a candidate track and decide where it belongs in the
arc, which is a better guide to energy than the estimate from its tags.

Nothing is uploaded and nothing is written: the player reads the file straight
off your drive through the local server, in byte ranges, so a 30-minute WAV starts
in a moment instead of downloading first.

**Scale.** Playlists of several thousand tracks stay smooth — the track pool is
virtualised, so only the rows on screen exist in the DOM — and a set can be up to
1000 tracks. Building a 300-track set from a 3000-track playlist takes a couple
of seconds.

**Fill in what rekordbox has not.** A download, a folder from a friend or a
playlist file often arrives with no BPM, no key and no beat grid. **Analyse
audio** measures all three locally: the strongest period in the onset
envelope's autocorrelation, refined by the onsets that land on the grid; a
chroma correlated against the Krumhansl-Schmuckler major and minor profiles;
and the phase that puts the most onset energy on the beat. Values the library
already has are never overwritten — the measurement fills the gaps, and the
pool marks a measured value `audio`. The measured grid also reaches the export
as a `<TEMPO>`, even when the export carries no cues.

**Clean the set and the pool.** **Duplicates** finds the same track listed
twice, one file behind two rows, and the same artist and title on different
files, groups them once even when several of those are true at the same time,
and lets you keep the row you want. A duplicate removed from a set is dropped
from the set; one removed from the pool is unticked there. No file and no
library row is deleted.

**Edit the metadata, and optionally the file.** Every track row has a ✎ button:
title, artist, album, genre, comment, year, track number, BPM and key. Saving
changes Parity's library record; ticking **Write tags to the file** also
rewrites the file's own tags with ffmpeg — the audio is copied, not re-encoded,
so the samples are untouched. A container that cannot hold a field says so:
WAV's INFO chunk has no BPM or key slot, and the report names them rather than
claiming a write that never landed.

**Know what's actually on disk.** A rekordbox collection also contains streaming
entries — SoundCloud edits and bootlegs, for instance — which have no file behind
them (rekordbox writes those as `file://localhostsoundcloud:…` locations).
Parity tags such rows `streaming` in the pool and in the set, and the
**Local files only** tickbox hides them, builds included. If a set you built does
contain streaming-only tracks, the stats line says how many, so it is never a
surprise when you go to play it.

For Spotify and Beatport rows, rekordbox keeps the name encrypted inside
master.db (`$A7:v1:…`) — the service's metadata is not handed to another app —
so Parity does not display that ciphertext. It shows the one readable fact, the
service and the track id (`Spotify track 5ZcmUzUzeuYnMjHYQXyHSz`), leaves the
fields it cannot read blank, and keeps the BPM, length and id that are plain.
The `streaming` badge says why on hover. A library imported before this check
existed is repaired in place on the next launch; your own edits are untouched.

**Purge, and say what was purged.** The pool filter drops, and reports on, four
kinds of noise: streaming stubs, local paths that no longer exist (external drive
unmounted, file renamed), anything outside the chosen playlist or folder, and
duplicate paths so an export never lists the same file twice. Every removal
carries a reason, and the pool preview shows the counts, so nothing disappears
silently. If a selected folder lives on a volume that isn't mounted, the UI says
so instead of showing an empty pool.

## Cues: eight pads, A to H

The **Analyse cues** button reads rekordbox's *own* phrase analysis — the same
`Intro / Up / Down / Chorus / Outro` blocks it draws above the waveform — and
fills all eight hot-cue slots:

| Slot | Colour | Role | Rule |
|------|--------|------|------|
| A | red | First Downbeat | first beat of the first phrase |
| B | orange | Intro End | where the intro gives way to the body — the mix-in point |
| C | yellow | Build Up | the last `Up` phrase before the drop |
| D | green | Main Drop | first `Chorus` after the intro |
| E | aqua | Breakdown | first `Down`/`Bridge` after the drop |
| F | purple | Drop 2 | the next `Chorus` after the breakdown |
| G | pink | Outro | where the outro starts |
| H | blue | Mix-Out | the closing 32, 16 or 8 beats |

**Every slot is filled, every time.** A role with no phrase to land on — a track
with no second drop, an edit whose outro runs straight off the end — falls back to
the nearest phrase line that is still in order, and each cue records the rule
that produced it. Across the 194 analysed tracks in the reference library, all 194
come back with eight cues. Where two cues would land closer than 400 ms (a
one-second sampler loop, say), the later one moves up instead of being dropped.

**The `1.1Bars` duplicate is gone.** rekordbox puts a memory cue named `1.1Bars`
on the first beat of most analysed tracks, which is exactly where hot cue A goes,
so the two markers used to sit on top of each other. The write now removes a
memory cue sitting within 100 ms of one of Parity's hot cues: the pad wins,
because you can hit it.

**Tracks that already have cues are yours to decide about.** Every track in the
pool carries a badge for the cues rekordbox already has on it — `yours` (blue) for
cues you placed by hand, `rk` (grey) for the ones Parity wrote on an earlier
run — and **Write ▾ → Already cued tracks** picks what a write does with them:

| Setting | What happens |
|---------|--------------|
| **Replace them (A–H)** — the default | Every track in the set gets the eight pads, as before. |
| **Keep them, skip that track** | A track whose hot cues are your own is left *exactly* as it is — no cue rows added, none removed, not even the `CueUpdated` flag — while its playlist position is still written. Tracks carrying only Parity's own cues are cued as usual, so "keep" never freezes your library on the first write. |

Whatever the setting, the per-track switch on each set row overrides it for that
track (`keep cues` / `write cues`), and the dry-run report lists every track that
was kept, with the cues it kept. Cues Parity wrote itself are recognised by the
names it stamps on them, including the old four-cue layout
(`First Downbeat`, `Build Up`, `Main Drop`, `Mix-Out / Breakdown`). A lone
rekordbox memory cue does not count as "yours" — count it and *keep* would mean
never cueing anything again. The XML export follows the same rule: a kept track's
`<POSITION_MARK>`s are the ones it already has, plus its beat grid.

It also places a **memory cue** on every other phrase boundary, named after the
phrase (`Chorus 9`, `Outro 11`). The analysis is logged per track with its
timestamps:

```
[parity] INFO parity.cues: Diplo, Major Lazer, Tokischa - pAPi wiTH tOKisCha |
  rekordbox-anlz | phrases=11 beats=331 bpm=145.0 |
  A=0.002 B=6.623 C=41.381 D=107.588 E=120.100 F=143.700 G=178.400 H=185.100 | 7 memory
```

A track rekordbox has not analysed falls back to a duration + BPM estimate, and
that is flagged `confidence: "low"` everywhere (an orange cue badge) rather than
passed off as real. There is no audio decoding and no heavy dependency: the ANLZ
files rekordbox already wrote are far more accurate than a generic beat detector
would be.

That is a deliberate substitution for running a `librosa` novelty/segmenter here.
librosa would pull in an audio decode stack plus SciPy and numba to re-derive a
phrase map rekordbox has already computed per track, and the derived map is the
less accurate of the two — the phrases map onto the roles one-to-one anyway:
**Intro** → First Downbeat and Intro End, **Build** → Build Up, **Drop** → Main
Drop and Drop 2, **Down**/**Bridge** → Breakdown, **Outro** → Outro and Mix-Out.

**Edit afterwards.** Drag rows to reorder, arrow buttons to nudge, 🔒 to pin a
track to its position, ✕ to drop it. `Optimize` re-runs the solver around your
pinned tracks and everything you fixed by hand. `Undo` (Ctrl+Z) steps back.
`Ctrl/Cmd+Enter` rebuilds.

**Export.** AUTO/PILOT bundle, Send to AUTO/PILOT (an XML written to
`~/Downloads`), rekordbox XML, M3U8, CSV, a printable text set list, or a
self-contained **HTML** / **PDF** set list for a phone, a printer or a folder
beside the XML. Saved sets are stored locally too — and **Delete** removes the
loaded one — while cue plans persist between runs, so exporting later does not
mean analysing again. A cue plan written by an older cue layout is dropped
rather than shown half-filled, and the pool says so until you analyse again.

**Listen while you choose.** The preview has a loop (⇤ / ⇥ place its ends at the
playhead, ↻ switches it on, `L` toggles it), beat jump by 1–32 beats using the
track's own BPM (`,` and `.`), and a queue: **⏭** on any row puts it next, and a
track that ends moves on to the queue instead of stopping. The queue and the
loop are for listening; neither changes the set, the cue plan or an export.

## The cue editor: the waveform, and moving a cue by hand

Every row in the pool and every set row carries a **〰** button that opens the cue
editor for that track, and so does the preview player while a track is loaded —
the editor belongs to a *track*, not to the set, so it is reachable whether or not
anything has been built yet. One track at a time, because a waveform you drag cues
on wants the whole width and the eight pads belong to one track. It draws:

- **A three-band waveform**, from peaks the server extracted once and cached
  (`parity/waveform.py`): below 250 Hz in blue (the kick and the bassline),
  250 Hz–4 kHz in green (vocals and synths), 4 kHz up in white (hats and snares).
  Each band is mirrored around the centre line and each pixel column takes the
  loudest frame inside it, so a transient stays a transient at every zoom level
  instead of being averaged into the body around it. A band is scaled against
  its own 99.5th percentile, not its loudest single frame: one click where a
  needle dropped cannot flatten a whole track into a line.
- **A bar grid** from the track's own BPM — thin every 8 bars, brighter every 32 —
  so the phrase lines cue B to H were placed on can be counted by eye.
- **The mix windows** as striped bands along the bottom: mix-in (the intro, up to
  **Intro End**) in green, mix-out (the closing bars, from **Mix-Out**) in orange.
  Two adjacent tracks whose stripes do not overlap cannot be blended without
  cutting one of them short, which is worth seeing here rather than in the booth.
- **The eight cues**, each a line in its own colour with a flag carrying its
  letter, using the same RGB values the write puts in rekordbox's palette table —
  so what is on screen is what the CDJ shows.

**Moving a cue.** Drag a flag and it lands on the grid: 1/16 note by default, or
a beat, or a bar, from **Snap** in the toolbar. Hold **Shift** to place it
anywhere — the readout says `free` while you do, and the exact position is always
under the cursor. **A cue you move is locked**, and that is what the lock is for:
**Analyse cues** rebuilds every position from rekordbox's phrase map and would
otherwise quietly undo the one position you chose yourself. A locked cue stays
where it is and the plan says so (`3 hand-moved cue(s) kept where you put them`);
the set row's badge counts it. Click a flag *without* dragging to lock or unlock
it by hand. A locked cue is written even under the **keep** policy, because it is
a decision of yours made after the cues that made the track one of "yours".

Dragging the waveform pans it, scrolling over it steps the zoom (whole track to
16×), and clicking it starts the preview from that point with the playhead drawn
across the waveform while it runs. Zoom is centred on what you are looking at,
which is how a cue ends up within a few milliseconds of the beat you meant.

The picture is Canvas 2D, not WebGL: three bands of a thousand peaks is 3000
numbers per redraw at 60 fps while the preview plays, which this draws in well
under a frame, and a shader pipeline would be more machinery for no measured
gain. The peaks are read the first time a track is opened and cached in
`store.json` beside the measured energy — about ten kilobytes a track — so
opening it again is instant; the
cache notices the file changing (size and mtime), and `force` re-reads it either
way.

## Exporting: rekordbox and AUTO/PILOT

The **AUTO/PILOT bundle** is one zip with everything both importers want:

```
Syd-Bday-Ultimate.zip                (the set name, spaces turned into hyphens)
  Syd-Bday-Ultimate.xml              the import file (rekordbox XML)
  Syd-Bday-Ultimate.autopilot.json   order, Camelot path, tempo ramp, cue times
  Syd-Bday-Ultimate.txt              printable set list
  Syd-Bday-Ultimate.csv              spreadsheet
```

**Export → rekordbox XML** writes that `.xml` on its own, which is the file to
import if you are not using the bundle.

The **set package** (see *Tagging, the set package, and pre-flight*) is the same
XML plus the set list, the cue log and the pre-flight report, for handing a set to
someone else.

AUTO/PILOT imports rekordbox XML, so that XML is the import target for both — it
is not a proprietary format. What makes it clean to import:

- **Only the set, in order.** `<COLLECTION>` holds exactly the exported tracks,
  and the playlist node lists the same ids in the same order — no library clutter.
- **Real paths.** `Location` keeps the original file URL (e.g.
  `file://localhost/Volumes/EXT%20SSD/RekordBox%20Library/Syd%20Bday%20Playlist/…`),
  so the tracks play from the drive they live on. The pool filter has already
  proved every path exists, so there are no missing-file errors.
- **Injected cues.** `<POSITION_MARK>` elements with `Num` 0–7 (hot cues A–H),
  their `Red`/`Green`/`Blue` values, and `Num="-1"` memory cues. All eight hot
  slots belong to Parity, so an injected cue always replaces what was there.
- **A beat grid.** `<TEMPO>` written from the analysed grid, so the exported track
  is beat-gridded on import too.

Turning the cue toggle off in the Export menu writes the XML without any
`POSITION_MARK`, exactly like the old behaviour.

## Tagging, the set package, and pre-flight

Three things that matter after the set is built: what the tracks say about
themselves in rekordbox, what travels with the set if you hand it over or put it
away, and what is wrong with it before either happens.

### The tag: `[E7] [peak] 14/24`

The comment field is where every DJ already writes things like *"banger edit"*,
and it is the one field rekordbox searches directly — so that is where a set tag
goes.

| Tag | Means |
|-----|-------|
| `[E7]` | the track's energy as a whole number, 1–10 |
| `[peak]` | the part it played in *this* set: `opener`, `build`, `peak` or `closer` |
| `14/24` | where it sat: track 14 of 24 |

The role is positional and needs no guessing: the set opens with the opener and
closes with the closer, any track within half an energy point of the set's own
peak is a peak, and everything else is a build — including the slide down after
the peak, because one word has to carry the arc and the exact energies ride in
the package's manifest beside it. Every tag records the rule that fired, so the
dry run can say *why* a track is a `[peak]` rather than just asserting it.

**The tag is on the row before anything is written.** Every track in the built
set shows its tag on the right of its row, and reordering the set, rebuilding it
or retyping an energy updates those tags — so the tag can be read, and argued
with, without exporting to find out what it says. The hover text carries the
rule that fired and the comment that will result. The tags are worked out by
`parity/tags.py` over `/api/tags`, the same code the export and the write run,
so the row is a statement about the file rather than a second guess at it.

**Nothing you wrote is ever lost.** A tag is appended, never assigned:
`"banger edit [E9] [peak] 3/24"`. A second write replaces only Parity's own tag,
so tagging the same track for a second set does not stack two tags, and anything
else in the field — including another tool's brackets — is left alone.

It goes to both places the set can end up:

- **The exported XML**, as the `Comments` attribute of `<TRACK>` — that is how
  rekordbox's XML carries a comment (there is no `<COMMENT>` element in the
  format) and it is the attribute rekordbox reads on import, so the tag is
  searchable in the library the file lands in. On by default; untick **Tag
  comments** in the Export menu to leave comments exactly as they are.
- **`master.db`**, as `DjmdContent.Commnt`. Backed up first, one transaction,
  dry run lists every comment before and after — the same guards as every other
  write. Off by default in **Write ▾**: this is the one part of a write that
  changes something you read in rekordbox later, so it is a tick, not a default.
  Nothing is written alongside it: `DjmdContent.SearchStr` is `NULL` on every row
  in a real library, so the comment is the field the search reads and there is no
  second copy of it to go stale.

A tagged set feeds itself: `[E7]` is one of the tags Parity already reads when
it estimates energy (see *Where "energy" comes from*), so a library tagged here
comes back with those energies intact. Tags past rekordbox's declared 255-character
comment field are still written — SQLite does not enforce the length — and the
report says which tracks went over rather than silently truncating them.

### The set package

**Export → Set package (.zip)** writes one file that describes the set without
Parity and without rekordbox:

```
Friday-Peak.zip
  Friday-Peak.xml              the set as rekordbox XML: cues, tags, beat grids — import this
  Friday-Peak.txt              the set list: BPM, key and energy per track, in order
  Friday-Peak.csv              the same list for a spreadsheet
  Friday-Peak.cues.txt         the cue log: every hot and memory cue, its colour, and why it landed there
  Friday-Peak.preflight.txt    the pre-flight check as it stood when the package was written
  Friday-Peak.autopilot.json   the set as JSON: order, transitions, stats, cue timestamps
  README.txt                   what is in the package and how to use it
```

The cue log is the part worth keeping: it names each pad, its timestamp, colour,
whether it came from rekordbox's phrase map or the heuristic, and the reason the
position was chosen — so a cue plan can be read and argued about a year later
without either app.

### Pre-flight

Before an export or a write, Parity checks the set and reports:

| Check | Severity | What it means |
|-------|----------|---------------|
| no file behind a track | error | unplugged drive, renamed folder, never downloaded — the XML would list a row you cannot play |
| the same track listed twice | error | one track takes two slots |
| two tracks pointing at one file | error | one file, listed as two tracks |
| same artist and title, different files | warning | looks like a duplicate import |
| streaming-only track | warning | it travels in the XML but there is no file to carry |
| no cues generated yet | warning | that track exports with no cue points |

**Nothing here blocks anything.** An export is a copy and you decide what
belongs in the set, so the check reports and gets out of the way: the report
appears before the file is written, the write report carries the same list, the
package keeps a copy of it, and `--preflight` returns exit status 1 for a script.
**Export → Pre-flight check** runs it on its own any time.

## Writing straight into the libraries

The XML export is no longer the only way in. **Write ▾** talks to the two
databases directly, which is what makes an exported file optional:

| Action | What it does |
|--------|--------------|
| **Dry run** | Resolves the set against `master.db` and lists every row it would touch. Writes nothing, ever. |
| **rekordbox master.db** | Writes hot cues A–H into `djmdCue`, drops the memory cue that duplicates one of them, and points a playlist at the set, in set order, in `djmdSongPlaylist`. |
| **AUTO/PILOT library** | Upserts the set's tracks and its playlist order into AUTO/PILOT's own SQLite library. |
| **Both libraries** | rekordbox first, then AUTO/PILOT, reporting each separately. |

Everything below is enforced, not documented-and-hoped:

- **rekordbox must be closed** (and AUTO/PILOT, for its own library). The check
  runs before anything is opened, names the process that is in the way, and the
  endpoint answers `409` rather than half-writing. pyrekordbox refuses to commit
  while the app is open too, so there are two independent guards.
- **A backup is taken first**, every time, next to the database:
  `~/Library/Pioneer/rekordbox/parity-writes/<timestamp>/` holds `master.db`,
  its `-wal`/`-shm` sides and `masterPlaylists6.xml` (the last five are kept).
- **One transaction, not one per row.** Every cue row and the whole playlist
  reorder are buffered and committed once, at the end; any failure rolls the lot
  back. pyrekordbox's own `remove_from_playlist` commits once per row, so the
  writer touches `djmdSongPlaylist` through the session directly instead — which
  keeps the reorder atomic *and* keeps a dry run genuinely read-only, since the
  committing path is never entered.
- **A dry run never opens the live file.** It resolves against a snapshot copy in
  `/tmp`, so previewing cannot change a byte — and it works while rekordbox is
  open, which is exactly when you want to look before quitting the app.
- **All eight slots are ours** — under the default policy. The write deletes and
  rewrites `Kind` 2–9 (that is A–H), so a hot cue you placed by hand in one of
  those pads is replaced. Choose **Keep them, skip that track** and a track whose
  cues are your own is left untouched instead: the check reads the live database
  at write time, so a cue you placed in rekordbox after the import is respected
  too, and the report's `kept` list says which tracks were skipped and what they
  kept. A *memory* cue sitting within 100 ms of one of our hot cues goes either
  way — that is almost always rekordbox's own `1.1Bars`, which would otherwise
  stack on hot cue A. A memory cue anywhere else is left untouched, and every
  removed one is listed in the report's `duplicates`. Writing memory cues
  *wholesale* is still opt-in, and turning it on replaces every memory cue on the
  affected tracks, including rekordbox's own cue analysis — the toggle says so.
- **Order is written, not just appended.** The playlist table ends up in exactly
  the built order; entries that left the set are dropped from it, and the totals
  (written / removed / new / dropped) come back in the report.

What lands in `master.db`, per track: eight `djmdCue` rows with `Kind` 2–9,
`InMsec` from the plan, `InFrame` at rekordbox's 150 fps, the cue's name as `Comment`, the
palette index in `ColorTableIndex` (`Color` 255, as rekordbox writes it) and the
content's `UUID`; then `DjmdContent.CueUpdated` is flagged, which is how rekordbox
marks a track whose cues changed. The commit advances the local USN and adds the
playlist's node to `masterPlaylists6.xml`, so rekordbox sees the change on next
launch. Every row is logged as it happens:

```
[parity] INFO parity.writedb: djmdCue insert id=177718394 content=44211410 slot=A kind=2
  msec=0 inFrame=0 colorTableIndex=2 comment='First Downbeat' (HAVEN. - I Run)
[parity] INFO parity.writedb: djmdContent update ID=44211410 CueUpdated='1'
[parity] INFO parity.writedb: djmdSongPlaylist insert playlist=91499145 content=44211410
  trackNo=1 title='HAVEN. - I Run'
[parity] INFO parity.writedb: playlist 'Syd Bday Ultimate' (id=91499145): 108 tracks,
  108 rows written, 0 removed, 0 new, 0 dropped, rewritten (created)
```

**AUTO/PILOT's sidecars are not written.** Its `Database/TrackData/**/*.apd` files
hold cues, beat grid, waveform and artwork in AUTO/PILOT's own container. Measured
across the 20 sidecars of a real library, every file has the same layout:

| Offset | Bytes | Contents |
|--------|-------|----------|
| 0 | 8 | `415046494c453031` — `APFILE01`, the only plaintext in the file, and the same magic string AUTO/PILOT's own binary carries |
| 8 | 8 | `01 00 01 03 01 00 00 00` — format/version flags, identical in all 20 |
| 16 | 8 | a small number (8984–9254) — *not* the file size |
| 24 | 8 | the payload length: file size − 72, exact in all 20 |
| 32 | 16 | random and unique per file — a salt/IV |
| 48 | n | the payload: 7.97 of a possible 8.0 bits per byte, no ASCII run of any length, and nothing that inflates at any offset as deflate, zlib, LZMA, bz2, LZFSE or LZ4 |
| end | 24 | random and unique per file — a tag or second key |

That says encrypted-or-obfuscated rather than packed: a compressed container would
leave readable structure and a lower byte entropy, and AUTO/PILOT is a
cloud-activated app (`activation.json` carries a `deviceToken` and a session). With
no public spec, a Parity-authored container would be a guess that risks
corrupting the analysis store instead of a sidecar AUTO/PILOT accepts, so Parity
writes the track rows and the playlist and reports how many of the written tracks
already have a sidecar — the rest get one when AUTO/PILOT analyses the track. The
library rows themselves are the same ones the app creates (its own
`INSERT INTO Tracks … ON CONFLICT(FileLocation) DO UPDATE` statement), so the
entries show up in the browser immediately.

## Where "energy" comes from

rekordbox's XML export has no energy column, so Parity derives it — in this
order of trust, and always editable:

1. A value you type into the Energy column (remembered per track, and it
   survives a re-import).
2. An explicit tag in the **Comments** field: `E7`, `energy 8`, `Energy: 6`,
   `[7/10]`, `banger E9`. (1–10, or 11–100 read as a percentage.)
3. **Measured from the audio** — press **Measure energy** and see below.
4. The **star rating**, nudged around the genre's typical intensity.
5. The **genre** alone (Techno ≈ 8, Deep House ≈ 4.6, Ambient ≈ 1.8, …).
6. Bare **BPM**, as a last resort.

Estimates that rest on thin evidence are outlined in orange in the track pool —
worth a glance before you trust them; a measured value has a solid green edge and
lists what it saw when you hover it.

### Measured from the audio

Guessing from tags has a ceiling: rekordbox exports nothing that separates two
same-genre, same-tempo tracks, which is why a pool can come out with thirty
records on E4.2. **Measure energy** in the track bar reads each local file
instead — `ffmpeg` decodes it to mono 22.05 kHz, and four things are measured:

| Feature | What it is | What it separates |
|---------|-----------|-------------------|
| **Loudness** | RMS of the loud frames, dBFS | a club master (≈ -5) from a demo or a loop (≈ -18) |
| **Brightness** | spectral centroid, Hz | peak-time techno from deep house at the same level |
| **Dynamic range** | spread between the loud and quiet frames | a squashed master from a breathing, less driving mix |
| **Drive** | spectral changes per second (onset rate) | a busy roller from a slow burner |

The features are taken with `librosa.feature.rms` and
`librosa.feature.spectral_centroid` when librosa is installed — the functions
this was specified around — and with numpy at the same frame size and hop when it
is not, so the 23-package librosa install is optional rather than required.
Nothing leaves the machine and no service is involved.

**If it says ffmpeg is missing.** It is looked for in four places, in this order:
`$PARITY_FFMPEG`, your `$PATH`, the standard install locations
(`/opt/homebrew/bin`, `/usr/local/bin`, `/opt/local/bin`,
`/home/linuxbrew/.linuxbrew/bin`, `/snap/bin`, `/usr/bin`, `/bin`), and only then
given up on. That order matters on a Mac: an app launched from Finder inherits
launchd's minimal PATH — `/usr/bin:/bin:/usr/sbin:/sbin` — and not the one in
your shell, so Homebrew's ffmpeg is invisible to it. Searching the standard
locations is what finds it, without you setting anything. If it still cannot, the
error names every path it tried and both ways to point it at the binary:

```
launchctl setenv PARITY_FFMPEG /opt/homebrew/bin/ffmpeg    # the Mac app; relaunch it after
export PARITY_FFMPEG=/opt/homebrew/bin/ffmpeg               # a terminal run
```

`ffprobe` is found beside the ffmpeg that was found — it ships in the same
install — or by the same search, and `PARITY_FFPROBE` overrides it the same
way. `GET /api/energy/audio` and `GET /api/ingest` both report where they looked.
The scoring constants are
fitted to a real 116-file library, not chosen by taste: with them those files
spread E2.1–E10.0 (median 7.9), bare production loop samples landing at E2–E6 and
mastered club tracks at E7–E10, where the metadata estimate had put every one of
them on E4.2. Against the same 20-seed sweep as before, measuring the audio takes
the shape correlation from 0.88 to 0.93 on that library (worst build 0.76 →
0.86).

What it costs: a measured library is an *honest* one, and if yours is all mastered
peak-time material it will say so — every track in a narrow band, and a `Chill`
curve that cannot be built from it. The build says that in words ("the pool's
energies span E7.3–E8.3 against a curve of E2.5–E4.0, so there is not enough range
in it to build that shape") instead of drawing a flat line and letting you wonder.

Results are cached by file (path, size, mtime) and by scoring model, so the second
press is instant and only changed files are re-read; 116 files take about 20
seconds the first time, 0.2 s each. `GET /api/energy/audio` reports what is
available and how much is measured, `POST /api/energy/audio` runs it (with
`dry_run`, `force` and `limit`), and `POST /api/energy/audio/clear` forgets it.
A run answers with the keys it measured, the keys it skipped as already cached
(`cached_keys`, not only a count), and the ones it could not read, so a caller
asking for a big pool a chunk at a time can drop the settled keys instead of
counting the same cached track again in every chunk.

## Getting the set back into rekordbox

**Straight into the database** (quit rekordbox first): **Write ▾ → Dry run**, read
the rows, then **Write ▾ → rekordbox master.db**. The cues and the playlist are
there when rekordbox next opens; nothing to import, and a backup sits beside the
database in case you want to go back. See *Writing straight into the libraries*
above for what exactly is touched.

**By file**, if you would rather import it yourself:

1. **Export → rekordbox XML** and save the file somewhere permanent.
2. In rekordbox: `File → Preferences → Advanced → rekordbox xml` (called
   *Imported Library* in some versions) and point it at the exported file.
3. A `rekordbox xml` tree appears in the sidebar with your set as a playlist.
   Drag it into your collection to keep it.

When the library came from an XML export, the original `<TRACK>` elements are
copied verbatim, so BPM, key, cue points and beat grids come along untouched. When
it came from `master.db` there is no export to copy, so Parity writes the full
`<TRACK>` attribute set plus a `<TEMPO>` grid and the generated cues.
For AUTO/PILOT: **File → Import rekordbox XML…** and pick the same file (or unzip
the bundle first).

## How the builder works

`parity/builder.py` runs three stages:

1. **Curve fit** (`fit_curve`). The slots are ranked by target energy and the
   tracks by energy, and a dynamic program picks the group that minimises the
   total squared distance between the two rankings. Matching the k-th quietest
   track to the k-th quietest target is the pairing that minimises the total
   squared deviation, so the only real question is which tracks to take — and a
   slot-by-slot greedy gets that wrong (it spends a track an early slot can
   afford on one a later slot needed more). Where a library clumps (no energy
   column in rekordbox, so untagged tracks share one BPM-derived number), the
   members of a clump are drawn at random: choosing between them costs the curve
   nothing, and it is why two builds are not the same set.
2. **Selection repair** (`refine_selection`). The fit picks by energy alone, so
   it can choose a track no rearrangement will sequence — the pool's only 120 BPM
   cut next to two 140s. Each slot is offered the near-miss tracks the fit
   rejected, and a swap is kept only when the whole set's cost improves.
3. **Simulated annealing**, over swaps, moves and *swap-ins* (a slot trades its
   track for one from the pool) — the move that lets the search change which
   tracks are in the set, which reordering alone cannot do. Locks are held
   exactly: no move may cross a pinned slot.
4. **Strict repair** (`resolve_clashes`), Strict mode only. Annealing proposes
   moves at random, so it can simply never try the pair that removes the last
   weak transition. This walks every transition scoring below a diagonal and
   tries every swap and near-miss replacement around it; when no single move
   helps it swings a five-slot window across the whole set, because the track
   that has to move is often not one of the pair — a 4A and a 6A cannot be
   chained if the tracks either side are a 2A and a 7A, and no swap fixes that.
   It spends a bounded amount of the curve doing it (an extra key weight and a
   half per fault removed), takes cost-lowering moves for free first, and
   `strict_notes` reports any pair the pool cannot bridge at that price.

The cost of a candidate set is:

```
Σ energy_weight · (3 · (track_energy − target_energy)² + outlier_charge)   curve
+ Σ key_weight     · (1 − harmonic_compatibility)                          Camelot
+ Σ bpm_weight     · tempo_jump_penalty                                    beatmatching
+ artist repetition, and a large constant for clashes in Strict mode
```

`outlier_charge` charges a deviation past one energy point again (12 per point
squared): plain squared error tolerates a single visible outlier, which is how a
build ends up opening a whole energy point off the curve it drew. Both constants
were set by measuring, not by taste — see the note in `builder.py`. Over 20 seeds
of three curve shapes on a 194-track library, the pair give a mean correlation
of 0.88 between the set's energies and the curve it was drawn (worst 0.76, none
below 0.7) at 0.37 mean energy points of fit, where the previous constants gave
0.83 and left single builds at 0.63. Strict mode costs curve to buy harmony: the
same sweep is 0.87 correlation, 0.54 of fit, and 96% of transitions harmonic
rather than 81% — and no clash at all in any of the 60 builds, where Normal
leaves one in two thirds of them. Where Strict cannot bridge a gap it says so
instead of pretending.

A build is still milliseconds over a few dozen tracks, which is why the UI feels
instant and why `Optimize` is worth pressing repeatedly with different seeds.

## The pipeline

The four modules and the flow between them:

```
  master.db ──┐
              ├──▶ rxdb.read_library ──▶ Library ──▶ poolfilter ──▶ builder ──▶ structure ──▶ exporting
  .xml export ┘        (module 1)                     (module 2)    (existing)    (module 3)     (module 4)
```

`POST /api/pipeline` runs the whole thing in one request — filter, purge, sequence,
cue, export, save — and returns the report, the ordered set, its stats, the cue
counts and where the file landed. It is an API-only shortcut: the UI walks the
same stages with the buttons it already has, so there is no pipeline button to
press. Each stage is also available on its own:

| Endpoint | What it does |
|----------|--------------|
| `GET  /api/rekordbox` | database path, whether pyrekordbox is available, library counts |
| `POST /api/rekordbox/connect` | read `master.db` into the store, XML fallback |
| `POST /api/pool` | preview a filter: kept count, removals and reasons |
| `POST /api/cues` | analyse phrases and store hot + memory cues (a locked, hand-moved cue is kept) |
| `GET  /api/cues?key=` | one track's cue plan — the preview player's jump targets, plus the grid, phrases and lock state the editor draws |
| `POST /api/cues/edit` | move one hot cue, or lock/unlock it (`key`, `slot`, `ms` or `seconds`, `locked`) |
| `GET  /api/waveform?key=` | three-band waveform peaks for a track, read from the file the first time and cached (`force` re-reads) |
| `GET  /api/audio?key=` | stream a local track for the preview (byte ranges supported) |
| `POST /api/cues/clear` | forget the stored cue plans |
| `GET  /api/energy/audio` | whether ffmpeg/librosa are available, and how many tracks are measured |
| `POST /api/energy/audio` | measure loudness, brightness, dynamics and drive per track (`keys`, `dry_run`, `force`, `limit`) |
| `POST /api/energy/audio/clear` | forget the measured energies |
| `GET  /api/analyze` | whether BPM/key/grid analysis can run here, and how many local tracks have it |
| `POST /api/analyze` | measure BPM, key and a beat grid (`keys`, `dry_run`, `force`, `limit`) |
| `POST /api/analyze/clear` | forget the measured tempo, keys and grids |
| `POST /api/duplicates` | group duplicates in a set or a pool (`order` / `keys` / `filter`) |
| `POST /api/duplicates/remove` | drop the unchosen rows from a set (`order`, `keep` by group id) |
| `POST /api/track/edit` | edit one track's metadata (`fields`), and optionally write the file's tags (`write_tags`) |
| `GET  /api/watch` | the watch folder's settings, playlists and what the last scan did |
| `POST /api/watch` | save the folder and switches; a switched-on folder scans at once (`path`, `enabled`, `recursive`, `playlist`) |
| `POST /api/watch/tick` | scan once, now |
| `POST /api/import/m3u` | import an M3U/M3U8 as a playlist (`path`, or `text` + `name`) |
| `GET  /api/ingest` | whether ffmpeg/ffprobe are available, the formats, and the folder last used |
| `POST /api/ingest` | scan a download folder: the tidied names, the conversions, nothing written |
| `POST /api/ingest/run` | convert and copy that folder into the destination (`dry_run` to only report) |
| `POST /api/cue-policy` | what a write does with tracks that already have cues (`policy`, plus per-track `values`) |
| `POST /api/preflight` | missing files, duplicate entries and tracks with no cues for a set (`order`); `GET /api/preflight?key=…` checks one track |
| `POST /api/tags` | the set tags for an ordered set (`order`): the `[E7] [peak] 14/24` each track would carry, with the rule that fired and the comment it would produce. Read-only — the same plan the export and the write use, for the row label in the UI |
| `POST /api/pipeline` | all of the above, ending in a saved export |
| `GET  /api/write/status` | whether rekordbox / AUTO/PILOT can be written to now, and where the backups go |
| `POST /api/write/rekordbox` | write cues + playlist order into `master.db` (dry run unless `dry_run: false`) |
| `POST /api/write/autopilot` | write tracks + playlist order into AUTO/PILOT's library |

## Layout

```
parity.py             entry point (starts the local server)
requirements-extras.txt optional pyrekordbox, for master.db support
parity/
  camelot.py            key parsing (Camelot, Open Key, musical) + wheel scores
  rekordbox.py          collection.xml and playlist tree parser
  rxdb.py               master.db reader (snapshot copy, never the live file)
  writedb.py            master.db writer: cue + playlist rows, backups, dry runs
  autopilot.py          AUTO/PILOT library writer (tracks, playlists, order)
  poolfilter.py         playlist/folder selection, streaming + broken-path purge
  structure.py          ANLZ phrase + beat-grid reader, cue placement rules
  tags.py               set tags ([E7] [peak] 14/24) and the merge that keeps your comment
  preflight.py          missing files and duplicate entries, before an export
  audio.py              energy measured from the audio (ffmpeg + numpy/librosa)
  analysis.py           BPM, key and beat grid measured from the audio (ffmpeg + numpy)
  duplicates.py         duplicate grouping and the remove-one plan
  metadata.py           metadata validation and ffmpeg tag writing (atomic re-mux)
  watchfolder.py        M3U import and the watch-folder scanner
  pdf.py                a small, dependency-free PDF writer for set lists
  waveform.py           three-band waveform peaks for the cue editor (ffmpeg + fft)
  ingest.py             filename tidying + ffmpeg conversion of a download folder
  energy.py             energy heuristics
  builder.py            curve templates, set construction, optimisation
  store.py              local JSON persistence (edits, sets, cue plans)
  exporting.py          cue injection, tags into the XML, cue log, XML / bundle / package / M3U8 / CSV / TXT
  server.py             JSON API on 127.0.0.1 + static file serving
  web/                  the UI (plain HTML/CSS/JS, no build step)
tools/
  install_extras.py     installs the optional extra (user site, or --venv DIR)
  build_macos_app.py    builds Parity.app (Swift + WebKit shell)
  macos/main.swift      the native app: window, menu bar, server lifecycle
  make_icon.py          draws the app icon and packs the .icns
                        (--preview prints the mark as text, to check it)
  make_sample_library.py  generates a fake rekordbox XML of any size
tests/                  unittest suite + a sample rekordbox export
data/                   created on first run; your library, edits, sets, cues
```

`build/` (the icon, the compiled Swift shell) and `dist/` (`Parity.app`) are
generated by the tools above and are not in the repository.

Your data lives in `data/` (or `--data-dir`): `store.json` for edits and saved
sets, and `library.xml` — a copy of your export, so exports can reproduce tracks
exactly even if you move the original file.

## Tests

```
python3 -m unittest discover -s tests -t .
```

604 tests cover key notation and harmonic scoring, the XML parser, energy
heuristics, the filename rules and the folder plan (including the silence
arithmetic and the conversion offsets), curve adherence (including the curve-fit selection against a
slot-by-slot greedy it has to beat), lock handling, the optimiser, every export
format, the API, a 1500-track library round trip, the track-view cache, and an
HTTP smoke test of the real server. The newer library tools are covered from
both ends too: duplicate grouping (one problem reported once, transitive
matches, a chosen row kept, a stale choice refused), metadata validation and
tag writing (an MP3 round trip, an AIFF, WAV naming the fields it cannot hold,
and an unreadable file leaving the original untouched), tempo/key/grid
estimation on synthetic clicks and triads plus caching and the grid in the
exported XML, M3U parsing and re-import without duplicate rows, the watch folder
test import/re-scan/missing-folder cases, and the HTML/PDF writers (escape
rules, a valid xref, and a long set paginating) — plus the master.db reader (against a
duck-typed stand-in, so it runs without pyrekordbox), the pool filter, cue
placement rules and the injected `<POSITION_MARK>` output. The tags are covered
from both ends: the merge (a comment kept, our own tag replaced, another tool's
brackets untouched, the round trip back through the energy reader) and the three
places it lands (the XML attribute, a re-export, `DjmdContent.Commnt`). The
pre-flight cases use real temporary files, so an unplugged drive is a real
missing file rather than a mocked one.

The writers are tested the same way: the rekordbox one against a database
stand-in (slots, replacement of Parity's own cues, foreign cues kept, the
`keep` policy skipping a hand-cued track and leaving its rows alone, the old
four-cue layout counting as ours, per-track overrides, playlist rewrites, and the
process guard), the AUTO/PILOT one against a real SQLite file created from that
app's schema. Strict repair has its own cases, using a hand-built pool whose
faults no swap can fix, plus one the pool genuinely cannot bridge. The audio
energy path is covered both pure (scoring and feature maths, no files) and end to
end (ffmpeg decoding WAVs the standard library writes, skipping itself where
ffmpeg is absent) including which of ffmpeg and numpy is the missing one, plus
the endpoint's caching and storage. The cue editor is covered the same way: one
read per track and a cache that notices the file changing, a moved cue that
survives the next analysis pass, a locked cue still written under `keep`, and the
waveform bands, column reduction and scaling on synthetic audio — with a real
ffmpeg decode where ffmpeg exists, and the librosa and numpy paths asserted to
draw the same track within a 255th on a machine that has librosa. The rename & convert
panel is covered the same way: the naming rules and the plan are pure and always run,
while the conversion, the artwork carried through it (counted in the written file),
the tags that come out the other side and the measured leading silence skip
themselves where ffmpeg is absent. Together they also cover
the endpoint contract (a dry run by default, `409` when an app is open) and the
CLI's write path.

## Limits, honestly

- Energy is only as good as your tags, ratings and genres *until* you press
  **Measure energy**, which reads the file itself. After that it is only as good
  as the measurement — and a library of one kind of music measures as one kind of
  music. Cue *placement* uses rekordbox's own audio analysis, the ANLZ files, and
  only for tracks you have already analysed in rekordbox; untouched tracks fall
  back to a duration + BPM estimate, marked low confidence. When most of a pool
  rests on that fallback, the curve cannot do better than the numbers it was
  given, and the build says so instead of drawing a curve it did not follow.
- The `keep` policy tells your cues from Parity's by the comment on the hot cue.
  A hand-placed cue that happens to be *named* `Main Drop` therefore reads as
  Parity's own and is replaced; if you rename cues in rekordbox, avoid its eight
  role names.
- Tempo changes are smooth within the set; Parity orders tracks and writes the
  grid it read, it does not analyse audio itself.
- The preview plays whatever the browser can decode — MP3, AAC/M4A, WAV, AIFF,
  FLAC and Ogg — from a local file, and only for tracks in the loaded library.
  Rekordbox's streaming entries have no file behind them, so there is nothing to
  play; the button says so instead of pretending.
- Reading a library never touches the live file (`master.db` is opened through a
  throwaway copy). Writing is the opposite and is treated that way: only on an
  explicit **Write ▾** action or `--write-* --commit`, only with the app closed,
  always after a backup, and only after a dry run has listed the rows.
- **Slots are assumed, not proven.** `Kind` 2–9 being hot cues A–H is the same
  convention the reader uses, checked against rekordbox's own XML export in this
  library (one cue, `Kind` 6, exported as `Num="4"`). If a written cue ever shows
  up in the wrong slot, that is a single constant — `CUE_KIND_OFFSET` in
  `parity/rxdb.py` — and re-running the write fixes every track.
- AUTO/PILOT's `.apd` sidecars are not written (see above): the playlist and the
  track rows are, the cached cues/grid/waveform are not, so newly added tracks
  look unanalysed in AUTO/PILOT until it analyses them.
- Rename & convert renames files; it does not read artist and title out of the tags
  and build the name from those. That is a decision about your library rather
  than about a filename, and the report would be the wrong place to discover it
  had been made — so a name is tidied, never re-authored. It also never moves or
  deletes anything: files are copied into the destination, and the staging folder
  keeps what it had.
- The conversion carries the tags the container can hold, cover art included:
  an embedded picture is an attached-picture stream rather than a tag, so it is
  mapped across and copied untouched, and AIFF holds it as an APIC frame in the
  ID3 chunk rekordbox already reads the tags from. WAV cannot hold artwork at
  all, so a covered MP3 sent to WAV loses it — the report flags that file, and
  rekordbox still shows artwork from its own analysis once the track is imported.
  A file already in the target format is copied byte for byte.
- The cue editor's waveform needs ffmpeg **and** a local file: a streaming-only
  track has no peaks, and the panel says so while still showing its cues. A file
  longer than 30 minutes is drawn as its first 30 minutes (the status line says
  so) rather than decoded whole, and the peaks are one byte per column — 0-255 is
  what a display needs, not what a measurement would keep.
- The waveform is an STFT magnitude per band, not a loudness model. Each band is
  scaled against its own percentile, so two tracks drawn the same height are not
  necessarily as loud as each other; it is a picture of shape, and the energy
  column is the measurement.
- A moved cue is written to Parity's own plan, not to rekordbox: the pads reach
  the database on the next **Write ▾ → rekordbox master.db**, exactly like the
  generated ones, and until then nothing outside Parity has changed.
- Wrong or missing BPM/key in rekordbox means missing data here — **Analyse
audio** fills the gaps from the files, but it measures one tempo for the whole
track and does not write BPM back into `master.db`: the value lives in Parity's
pool and in the export's `<TEMPO>`. A measured key with little tonal material
is reported as ambiguous rather than confidently wrong.
- **Write tags to the file** re-muxes with ffmpeg (`-c copy`), so the audio
samples are untouched, but the file is rewritten in place after a temp copy. A
container keeps what it can hold: WAV has no BPM or key in its INFO chunk, so
the report names those as not written. Parity's library record and the file
agree only when the tick was on; without it, the edit is local to Parity.
- **Duplicates** are removed from a set (the unchosen rows are dropped) or from
the pool (they are unticked), never from the library and never from disk. A
library-wide delete is still `Files & libraries ▾ → Manage libraries`, and it
is a separate, explicit decision.
- The **watch folder** imports into Parity's library and its playlist; it does
not move the files, convert them, or make rekordbox see them. Export the set or
write it when you want the tracks in a DJ app. An M3U dropped in a browser has
no base folder, so only its absolute paths resolve; a path passed to the API
does have one.
- The **queue and the loop** belong to the preview player: they are not saved
with a set and do not travel in an export. The loop is one in/out pair, and beat
jump needs a BPM — a track the library and the analyser both lack one for says
so instead of guessing.
- The **HTML and PDF** set lists are text: title, order, BPM, key, energy, genre
and length. No waveform, artwork or cue markers, and a title outside Latin-1 is
replaced in the PDF by `?` because the base-14 fonts cannot draw it (the HTML
page keeps the original text).
- Cue colours: the XML export carries the RGB values in
  `parity/structure.py` (the green is taken verbatim from a real rekordbox
  export); the direct write stores the matching *palette* entry instead
  (`djmdColor` 2 red, 4 yellow, 5 green, 7 blue), which is how rekordbox keeps
  cue colours itself. A colour with no palette name writes index 0.
- The Mac app bundle is a snapshot of the code: rebuild it after you change
  anything. It is also compiled for the architecture it was built on.
- The server has no password. It binds `127.0.0.1` and is meant to: `--host`
  with anything else prints a warning rather than refusing, because occasionally
  that is what someone wants — but everything the API can do (import a library,
  build a set, write into `master.db`) is then available to every machine that
  can reach the port.
- Importing XML that carries its own `<!DOCTYPE>`/`<!ENTITY>` declarations is
  refused, with a message, before it is parsed: entity expansion is how a
  document makes a parser turn a few lines into gigabytes, and a rekordbox export
  never has one. An export that would not open in rekordbox will not open here
  either, and says why instead of hanging.

## License

MIT — see [LICENSE](LICENSE).
