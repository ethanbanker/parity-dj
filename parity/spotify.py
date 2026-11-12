"""Spotify playlists as a source and a destination for a set.

Two directions, one credential:

* **Pull.**  A playlist link or id becomes an ordered list of tracks, matched
  against the loaded library by artist and title, so the pool shows the files
  the DJ actually owns and the report names the ones they do not.  Spotify is
  metadata here: no audio is downloaded, and nothing is invented into BPM, key
  or energy -- Spotify's audio-features endpoints are closed to new apps, and a
  guessed tempo is worse than a blank one.
* **Push.**  A built set becomes a new playlist.  Each track is searched by
  artist and title, and the ones Spotify cannot place are reported rather than
  quietly dropped.

Authentication is OAuth with PKCE (RFC 7636): the app never asks for a client
secret, and the only thing stored is a refresh token in ``store.json``.  Spotify
matches the redirect URI exactly, so the loopback address Parity serves is the
one to register in the app's dashboard.

Standard library only, like the rest of Parity -- ``urllib`` does the HTTP.

The shapes here follow the Web API as of the 2026 playlist changes: contents
are ``items.items[].item`` on a playlist owned by the user, ``/items`` replaces
``/tracks``, and Spotify only returns the contents of playlists the account owns
or collaborates on.  The older ``tracks.tracks[].track`` shape is read too, so
an app with extended quota keeps working.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

log = logging.getLogger("parity.spotify")

ACCOUNTS_ROOT = "https://accounts.spotify.com"
API_ROOT = "https://api.spotify.com/v1"
AUTHORIZE_URL = f"{ACCOUNTS_ROOT}/authorize"
TOKEN_URL = f"{ACCOUNTS_ROOT}/api/token"

#: The environment fallbacks, for a script or a shell profile.  The panel saves
#: the same two values in the store, which wins over these.
ENV_CLIENT_ID = "SPOTIFY_CLIENT_ID"
ENV_REDIRECT = "SPOTIFY_REDIRECT_URI"
DEFAULT_REDIRECT = "http://127.0.0.1:8765/api/spotify/callback"

#: Read the user's own and collaborative playlists, and create private ones.
#: ``user-read-private`` is what lets the panel say which account is connected.
SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-read-private",
)

#: How long a connection attempt stays valid.  A code is single-use and short
#: lived; the pending verifier is dropped after this regardless.
PENDING_TTL_SECONDS = 600

REQUEST_TIMEOUT = 30

#: Spotify returns at most 100 items per write.
ADD_CHUNK = 100

USER_AGENT = "Parity/1.0 (local DJ set builder)"


class SpotifyError(ValueError):
    """A failure the user can read: not connected, refused, no such playlist."""


# ------------------------------------------------------------------- tokens


@dataclass
class Token:
    """An access token, the refresh token behind it, and when it expires."""

    access: str = ""
    refresh: str = ""
    expires_at: float = 0.0
    scope: str = ""

    def expired(self, now: float | None = None) -> bool:
        # Thirty seconds of slack: a token that expires while the request is in
        # flight fails with a 401 the caller would have to explain.
        return not self.access or (now or time.time()) >= self.expires_at - 30

    def to_dict(self) -> dict:
        # The access token is deliberately not persisted: it lives an hour and
        # is not worth writing to disk.
        return {"scope": self.scope, "expires_at": self.expires_at}


def new_pkce() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(72)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(18)


def authorize_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    scopes: tuple[str, ...] = SCOPES,
) -> str:
    """The page the user approves Parity on."""
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
            "scope": " ".join(scopes),
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


# ------------------------------------------------------------------ transport


def http_request(method: str, url: str, headers: dict, body: bytes | None) -> dict:
    """One JSON request, with Spotify's errors turned into readable sentences.

    Module level and replaceable so the tests can exercise the client without a
    network; :class:`Client` resolves it when the client is built.
    """
    request = Request(url, data=body, method=method, headers=headers or {})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read() or b""
        raise SpotifyError(_error_message(exc.code, raw, exc.headers)) from exc
    except URLError as exc:
        raise SpotifyError(
            f"Could not reach Spotify ({exc.reason}). Check the connection and try again."
        ) from exc
    except TimeoutError as exc:
        raise SpotifyError("Spotify did not answer in time. Try again.") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpotifyError("Spotify returned something that is not JSON.") from exc


def _error_message(code: int, raw: bytes, headers=None) -> str:
    """A sentence for the user, and the status code for anyone reading logs."""
    detail = ""
    try:
        payload = json.loads((raw or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif error:
            detail = str(payload.get("error_description") or error)
    log.warning("Spotify HTTP %s: %s", code, detail or (raw or b"")[:200])
    if code == 400:
        return f"Spotify refused the request: {detail or 'bad request'}."
    if code == 401:
        return (
            "Spotify refused the token. Connect Spotify again in the Spotify panel "
            f"({detail or 'unauthorized'})."
        )
    if code == 403:
        return (
            "Spotify refused this account access "
            f"({detail or 'forbidden'}). A development-mode app only reaches users "
            "listed in its dashboard, and the app owner needs Spotify Premium."
        )
    if code == 404:
        return "Spotify has no such playlist, or this account cannot see it."
    if code == 429:
        retry = ""
        try:
            retry = f" Try again in {int(headers.get('Retry-After', 0))}s." if headers else ""
        except (TypeError, ValueError):
            retry = ""
        return f"Spotify is rate-limiting the app.{retry}"
    return f"Spotify returned HTTP {code}" + (f": {detail}" if detail else "") + "."


def _token_request(payload: dict, transport=None, previous: str = "") -> Token:
    body = urlencode(payload).encode("utf-8")
    data = (transport or http_request)(
        "POST",
        TOKEN_URL,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        body,
    )
    access = str((data or {}).get("access_token") or "")
    if not access:
        raise SpotifyError("Spotify did not return an access token.")
    try:
        expires_in = int((data or {}).get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return Token(
        access=access,
        refresh=str((data or {}).get("refresh_token") or previous),
        expires_at=time.time() + expires_in,
        scope=str((data or {}).get("scope") or ""),
    )


def exchange_code(
    client_id: str, code: str, verifier: str, redirect_uri: str, *, transport=None
) -> Token:
    """Turn the one-time code from the callback into a refresh token."""
    if not code:
        raise SpotifyError("Spotify did not return a code.")
    return _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        transport=transport,
    )


def refresh_access_token(client_id: str, refresh: str, *, transport=None) -> Token:
    if not refresh:
        raise SpotifyError("Spotify is not connected. Use the Spotify panel to connect it.")
    return _token_request(
        {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id},
        transport=transport,
        previous=refresh,
    )


# -------------------------------------------------------------------- client


class Client:
    """The handful of Web API calls Parity needs, with refresh built in."""

    def __init__(self, client_id: str, token: Token, *, transport=None, on_refresh=None):
        self.client_id = client_id
        self.token = token
        self.transport = transport or http_request
        # Called with the new token so the caller can persist a rotated refresh
        # token; Spotify does not always issue one, and the old one stays valid.
        self.on_refresh = on_refresh

    def refresh(self) -> Token:
        self.token = refresh_access_token(
            self.client_id, self.token.refresh, transport=self.transport
        )
        if self.on_refresh:
            try:
                self.on_refresh(self.token)
            except Exception:  # noqa: BLE001 - a storage failure is not an auth failure
                log.exception("could not persist the refreshed Spotify token")
        return self.token

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
    ) -> dict:
        if self.token.expired():
            self.refresh()
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {self.token.access}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        result = self.transport(method, url, headers, body)
        return result or {}

    # ------------------------------------------------------------- endpoints

    def me(self) -> dict:
        return self.call("GET", "/me")

    def playlist(self, playlist_id: str) -> dict:
        return self.call(
            "GET",
            f"/playlists/{quote(playlist_id, safe='')}",
            params={"fields": PLAYLIST_FIELDS},
        )

    def search_tracks(self, query: str, limit: int = 10) -> list[dict]:
        # Ten is Spotify's maximum since the February 2026 change.
        data = self.call(
            "GET", "/search", params={"q": query, "type": "track", "limit": min(limit, 10)}
        )
        return ((data.get("tracks") or {}).get("items")) or []


#: Both shapes are asked for: ``items`` is the current field, ``tracks`` the one
#: an extended-quota app still returns.  Spotify ignores unknown fields rather
#: than failing, so requesting both is safe.
_TRACK_FIELDS = (
    "id,uri,name,type,is_local,duration_ms,external_ids,external_urls,"
    "artists(name),album(name)"
)
PLAYLIST_FIELDS = (
    "id,name,description,external_urls,owner(display_name,id),"
    f"items(items(item({_TRACK_FIELDS})),next,total),"
    f"tracks(tracks(track({_TRACK_FIELDS})),next,total)"
)

_PLAYLIST_URL = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z-]+/)?playlist/([A-Za-z0-9]+)", re.IGNORECASE
)
_PLAYLIST_URI = re.compile(r"^spotify:playlist:([A-Za-z0-9]+)$", re.IGNORECASE)
_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_playlist_reference(value: str) -> str:
    """Accept a link, a ``spotify:playlist:`` URI, or a bare id.

    The link case covers the share menu's ``?si=`` tracking parameters and the
    ``/intl-de/`` prefix Spotify adds for a non-English UI, because those are
    what a paste actually contains.
    """
    text = str(value or "").strip()
    if not text:
        raise SpotifyError("Paste a Spotify playlist link or id.")
    match = _PLAYLIST_URL.search(text)
    if match:
        return match.group(1)
    match = _PLAYLIST_URI.match(text)
    if match:
        return match.group(1)
    if _PLAYLIST_ID.match(text):
        return text
    match = re.search(r"playlist[/:]([A-Za-z0-9]{22})", text)
    if match:
        return match.group(1)
    shown = text if len(text) <= 80 else text[:77] + "..."
    raise SpotifyError(f"“{shown}” is not a Spotify playlist link or id.")


def _normalise_track(track: object) -> dict | None:
    """One playlist row, or ``None`` for a removed row or a podcast episode."""
    if not isinstance(track, dict) or not track:
        return None
    if str(track.get("type") or "").lower() == "episode":
        return None
    title = str(track.get("name") or "").strip()
    if not title:
        return None
    artists = [
        str(artist.get("name") or "").strip()
        for artist in track.get("artists") or []
        if isinstance(artist, dict) and str(artist.get("name") or "").strip()
    ]
    try:
        duration_ms = int(track.get("duration_ms") or 0)
    except (TypeError, ValueError):
        duration_ms = 0
    ids = track.get("external_ids") or {}
    urls = track.get("external_urls") or {}
    album = track.get("album") or {}
    return {
        "id": str(track.get("id") or ""),
        "uri": str(track.get("uri") or ""),
        "title": title,
        "artist": ", ".join(artists),
        "artists": artists,
        "album": str(album.get("name") or "") if isinstance(album, dict) else "",
        "duration": round(duration_ms / 1000, 1),
        "duration_ms": duration_ms,
        "isrc": str(ids.get("isrc") or "").upper() if isinstance(ids, dict) else "",
        "local": bool(track.get("is_local")),
        "url": str(urls.get("spotify") or "") if isinstance(urls, dict) else "",
    }


def fetch_playlist(client: Client, reference: str) -> dict:
    """Read every track of a playlist, following the paging links.

    A playlist owned by the account comes back with its contents; someone
    else's comes back with metadata only, which is a Spotify rule rather than a
    failure, so it is reported as such instead of as an empty playlist.
    """
    playlist_id = parse_playlist_reference(reference)
    data = client.playlist(playlist_id)
    page = data.get("items") or data.get("tracks")
    if not isinstance(page, dict) or not page:
        raise SpotifyError(
            "Spotify returned no tracks for that playlist. It only returns the "
            "contents of playlists this account owns or collaborates on -- open "
            "the playlist in Spotify, copy it to your account, and pull that."
        )

    tracks: list[dict] = []
    skipped = 0
    seen_pages = 0
    while isinstance(page, dict):
        seen_pages += 1
        if seen_pages > 200:  # a next link that points at itself
            break
        entries = page.get("items")
        if entries is None:
            entries = page.get("tracks")
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            # Current shape: items.items[].item.  Legacy: tracks.tracks[].track.
            row = entry.get("item") if "item" in entry else entry.get("track")
            normalised = _normalise_track(row)
            if normalised is None:
                skipped += 1
                continue
            tracks.append(normalised)
        next_url = page.get("next")
        if not next_url:
            break
        page = client.call("GET", next_url)

    try:
        total = int(page.get("total") or data.get("items", {}).get("total") or len(tracks))
    except (TypeError, ValueError):
        total = len(tracks)
    owner = data.get("owner") or {}
    return {
        "id": str(data.get("id") or playlist_id),
        "name": str(data.get("name") or "Spotify playlist"),
        "description": str(data.get("description") or ""),
        "url": str((data.get("external_urls") or {}).get("spotify") or ""),
        "owner": str(owner.get("display_name") or owner.get("id") or ""),
        "total": max(total, len(tracks)),
        "tracks": tracks,
        "skipped": skipped,
    }


# ------------------------------------------------------------------ matching

#: Words that join artists rather than name them.  Splitting on these is what
#: makes ``A feat. B`` pair with the local row credited to ``A``.
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|;|&|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b|\bwith\b|\band\b)\s*", re.IGNORECASE
)


def fold(text: str) -> str:
    """Case-, accent- and punctuation-insensitive text for comparison."""
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(char for char in value if not unicodedata.combining(char)).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^0-9a-z]+", " ", value)
    return " ".join(value.split())


def artist_forms(artist: str) -> list[str]:
    """The full credit and its first artist, folded and deduplicated."""
    text = str(artist or "")
    primary = _ARTIST_SPLIT.split(text, maxsplit=1)[0]
    forms: list[str] = []
    for form in (text, primary):
        folded = fold(form)
        if folded and folded not in forms:
            forms.append(folded)
    return forms


def title_forms(title: str) -> list[str]:
    """The title, then its bracketed tail and mix suffix removed.

    A playlist says ``Song (Extended Mix)`` and a DJ's file may be tagged
    ``Song - Extended Mix`` or just ``Song``; all three are the same record and
    the report labels the looser matches rather than hiding them.
    """
    text = str(title or "")
    without_brackets = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", " ", text)
    without_suffix = re.split(r"\s+[—–-]\s+", text)[0]
    forms: list[str] = []
    for form in (text, without_brackets, without_suffix):
        folded = fold(form)
        if folded and folded not in forms:
            forms.append(folded)
    return forms


def _iter_views(views):
    """Accept either the ``all_views`` mapping or an iterable of its values."""
    return views.values() if isinstance(views, dict) else views


@dataclass
class LibraryIndex:
    """The loaded library, keyed the three ways a match is attempted."""

    pairs: dict[tuple[str, str], str] = field(default_factory=dict)
    titles: dict[str, set[str]] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    blobs: dict[str, str] = field(default_factory=dict)
    artists: dict[str, list[str]] = field(default_factory=dict)


def library_index(views) -> LibraryIndex:
    """Index the library's local tracks for the matcher.

    Streaming rows are left out: they have no file, so pulling one back would
    only put a row in the pool that every filter then removes.
    """
    index = LibraryIndex()
    for view in _iter_views(views):
        if not isinstance(view, dict) or view.get("streaming"):
            continue
        key = str(view.get("key") or "")
        title = str(view.get("title") or "")
        artist = str(view.get("artist") or "")
        if not key or not title:
            continue
        index.labels[key] = str(view.get("label") or f"{artist} - {title}".strip(" -"))
        index.artists[key] = artist_forms(artist)
        index.blobs[key] = f"{fold(artist)} {fold(title)}".strip()
        title_list = title_forms(title)
        for artist_form in artist_forms(artist):
            for title_form in title_list:
                index.pairs.setdefault((artist_form, title_form), key)
        for title_form in title_list:
            index.titles.setdefault(title_form, set()).add(key)
    return index


def _artist_ratio(first: str, second: str) -> float:
    """How close two credits look, as 0..1, using their best artist forms."""
    left = artist_forms(first)
    right = artist_forms(second)
    if not left or not right:
        return 0.0
    best = 0.0
    for one in left:
        for two in right:
            if one == two:
                return 1.0
            best = max(best, difflib.SequenceMatcher(None, one, two).ratio())
    return best


def match_track(track: dict, index: LibraryIndex) -> dict | None:
    """Find the library row behind a Spotify track, or ``None``.

    Exact artist + title wins; then a looser title form; then a title only one
    library row carries, but only when the credits are broadly alike, because a
    confident wrong match is worse for a set than a missing one.
    """
    if not track or not index.pairs:
        return None
    for artist_index, artist_form in enumerate(artist_forms(track.get("artist"))):
        for title_index, title_form in enumerate(title_forms(track.get("title"))):
            key = index.pairs.get((artist_form, title_form))
            if key:
                exact = artist_index == 0 and title_index == 0
                return {
                    "key": key,
                    "label": index.labels.get(key, ""),
                    "match": "exact" if exact else "loose",
                }

    for title_form in title_forms(track.get("title")):
        keys = index.titles.get(title_form) or set()
        if len(keys) != 1:
            continue
        key = next(iter(keys))
        local_artist = next(iter([
            label.split(" - ", 1)[0] for label in [index.labels.get(key, "")] if " - " in label
        ]), "")
        if _artist_ratio(track.get("artist"), local_artist) >= 0.6:
            return {"key": key, "label": index.labels.get(key, ""), "match": "title"}

    query = f"{fold(track.get('artist'))} {fold(track.get('title'))}".strip()
    if query:
        closest = difflib.get_close_matches(
            query, list(index.blobs.values()), n=1, cutoff=0.9
        )
        if closest:
            blob = closest[0]
            for key, value in index.blobs.items():
                if value == blob:
                    return {"key": key, "label": index.labels.get(key, ""), "match": "fuzzy"}
    return None


def match_playlist(playlist: dict, views) -> dict:
    """Match every pulled track to the library, in the playlist's order."""
    index = library_index(views)
    matched: list[dict] = []
    missing: list[dict] = []
    keys: list[str] = []
    seen: set[str] = set()
    repeats = 0
    for track in playlist.get("tracks") or []:
        hit = match_track(track, index)
        if not hit:
            missing.append(track)
            continue
        matched.append({**track, **hit})
        if hit["key"] in seen:
            # The playlist names two rows that resolve to one file; the pool
            # cannot hold it twice, so the order keeps the first position.
            repeats += 1
            continue
        seen.add(hit["key"])
        keys.append(hit["key"])
    total = len(matched) + len(missing)
    return {
        "keys": keys,
        "matched": matched,
        "missing": missing,
        "summary": {
            "total": total,
            "matched": len(matched),
            "missing": len(missing),
            "in_pool": len(keys),
            "repeated": repeats,
        },
    }


# ---------------------------------------------------------------- push (out)


def _escape_search(value: str) -> str:
    return re.sub(r'["\\]', " ", str(value or "")).strip()


def _search_queries(track: dict) -> list[str]:
    title = _escape_search(track.get("title") or "")
    artist = _escape_search(track.get("artist") or "")
    queries = []
    if artist and title:
        queries.append(f'artist:"{artist}" track:"{title}"')
    plain = " ".join(part for part in (artist, title) if part)
    if plain and plain not in queries:
        queries.append(plain)
    return queries


def _candidate_score(track: dict, candidate: dict) -> tuple[float, float, float]:
    """(combined, title ratio, artist ratio) for one search result."""
    title_ratio = difflib.SequenceMatcher(
        None, fold(track.get("title")), fold(candidate.get("name"))
    ).ratio()
    artist_ratio = _artist_ratio(
        track.get("artist") or "",
        ", ".join(
            str(artist.get("name") or "")
            for artist in candidate.get("artists") or []
            if isinstance(artist, dict)
        ),
    )
    try:
        seconds = int(candidate.get("duration_ms") or 0) / 1000.0
    except (TypeError, ValueError):
        seconds = 0.0
    wanted = float(track.get("duration") or 0)
    if not wanted or not seconds:
        duration_score = 0.5
    else:
        delta = abs(wanted - seconds)
        duration_score = 1.0 if delta <= 3 else 0.7 if delta <= 8 else 0.2 if delta <= 20 else 0.0
    score = 0.5 * title_ratio + 0.35 * artist_ratio + 0.15 * duration_score
    return round(score, 3), title_ratio, artist_ratio


def _best_candidate(track: dict, candidates: list[dict]) -> dict | None:
    wanted_isrc = str(track.get("isrc") or "").upper()
    best: tuple[float, dict] | None = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        ids = candidate.get("external_ids") or {}
        if wanted_isrc and isinstance(ids, dict) and str(ids.get("isrc") or "").upper() == wanted_isrc:
            return candidate
        score, title_ratio, artist_ratio = _candidate_score(track, candidate)
        if title_ratio < 0.85 or artist_ratio < 0.55 or score < 0.72:
            continue
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def search_track(client: Client, track: dict) -> dict | None:
    """The Spotify catalogue row that best matches one library track."""
    for query in _search_queries(track):
        try:
            candidates = client.search_tracks(query)
        except SpotifyError as exc:
            log.warning("search failed for %r: %s", track.get("title"), exc)
            continue
        found = _best_candidate(track, candidates)
        if found:
            return _normalise_track(found)
    return None


def resolve_tracks(client: Client, views) -> dict:
    """Look every track of a set up in Spotify, reporting the ones it lacks."""
    matched: list[dict] = []
    missing: list[dict] = []
    for view in _iter_views(views):
        found = search_track(client, view)
        if found and found.get("uri"):
            matched.append(
                {
                    "key": view.get("key", ""),
                    "label": view.get("label") or view.get("title", ""),
                    "spotify": found,
                }
            )
        else:
            missing.append(
                {
                    "key": view.get("key", ""),
                    "label": view.get("label") or view.get("title", ""),
                    "title": view.get("title", ""),
                    "artist": view.get("artist", ""),
                }
            )
    return {"matched": matched, "missing": missing}


def create_playlist(
    client: Client,
    name: str,
    views,
    *,
    description: str = "",
    public: bool = False,
) -> dict:
    """Create a playlist from a set, in order, and add what Spotify found."""
    title = (str(name or "").strip() or "Parity Set")[:100]
    resolved = resolve_tracks(client, views)
    uris = [row["spotify"]["uri"] for row in resolved["matched"] if row["spotify"].get("uri")]
    if not uris:
        raise SpotifyError(
            "None of these tracks could be found on Spotify, so no playlist was created. "
            "The report lists what was searched for."
        )

    playlist = client.call(
        "POST",
        "/me/playlists",
        payload={
            "name": title,
            "description": str(description or "")[:300],
            "public": bool(public),
        },
    )
    playlist_id = str(playlist.get("id") or "")
    if not playlist_id:
        raise SpotifyError("Spotify did not return the new playlist's id.")

    added = 0
    for start in range(0, len(uris), ADD_CHUNK):
        chunk = uris[start:start + ADD_CHUNK]
        client.call("POST", f"/playlists/{quote(playlist_id, safe='')}/items", payload={"uris": chunk})
        added += len(chunk)

    urls = playlist.get("external_urls") or {}
    return {
        "playlist": {
            "id": playlist_id,
            "name": str(playlist.get("name") or title),
            "url": str(urls.get("spotify") or "")
            or f"https://open.spotify.com/playlist/{playlist_id}",
            "public": bool(playlist.get("public", public)),
        },
        "added": added,
        "matched": resolved["matched"],
        "missing": resolved["missing"],
        "summary": {
            "tracks": len(list(_iter_views(views))),
            "added": added,
            "matched": len(resolved["matched"]),
            "missing": len(resolved["missing"]),
        },
    }


# ------------------------------------------------------------------- helpers


def redirect_uri_problem(value: str) -> str:
    """Why a redirect URI cannot be used, or an empty string when it can.

    The callback is a local route, so only a loopback address is accepted: a
    remote host would hand the one-time code to someone else.
    """
    text = str(value or "").strip()
    if not text:
        return "A redirect URI is required. Spotify matches it exactly."
    parsed = urlparse(text)
    if parsed.scheme != "http":
        return "Spotify's redirect URI must start with http:// for this local app."
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return "The redirect URI must point at this machine (127.0.0.1 or localhost)."
    if not parsed.path.endswith("/api/spotify/callback"):
        return "The redirect URI path must end with /api/spotify/callback."
    if parsed.query or parsed.fragment:
        return "The redirect URI cannot carry a query string or fragment."
    return ""


def settings_from_env() -> dict:
    """The two values the panel can also read from the environment."""
    values: dict = {}
    client_id = os.environ.get(ENV_CLIENT_ID, "").strip()
    if client_id:
        values["client_id"] = client_id
    redirect = os.environ.get(ENV_REDIRECT, "").strip()
    if redirect:
        values["redirect_uri"] = redirect
    return values
