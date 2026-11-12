"""Tests for :mod:`parity.spotify`.

No network: the transport is a canned Spotify that records what was asked for.
The cases that matter are the ones a live account cannot be relied on to show --
paging, the 2026 playlist field rename, a refresh token being rotated, and the
matcher refusing a confident wrong answer.
"""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from unittest import mock

from parity import rekordbox, spotify
from parity.server import Api, start_in_thread
from parity.store import Store

#: A real-shaped playlist id: 22 characters, which is what a paste carries.
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"


# ------------------------------------------------------------------ fixtures


def sp_track(track_id, name, artists, *, duration_ms=300000, isrc="", is_local=False):
    return {
        "id": track_id,
        "uri": "" if is_local else f"spotify:track:{track_id}",
        "name": name,
        "type": "track",
        "is_local": is_local,
        "duration_ms": duration_ms,
        "artists": [{"name": artist} for artist in artists],
        "album": {"name": "An Album"},
        "external_ids": {"isrc": isrc},
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
    }


def playlist_page(items, *, total=None, next_url=None, legacy=False):
    """A playlist payload in either the 2026 or the older shape."""
    rows = (
        [{"track": item} for item in items]
        if legacy
        else [{"item": item} for item in items]
    )
    inner = {"items" if not legacy else "tracks": rows, "next": next_url, "total": total if total is not None else len(items)}
    return inner


def spotify_playlist(rows, *, name="Sunset Drive", total=None, next_url=None, legacy=False):
    payload = {
        "id": PLAYLIST_ID,
        "name": name,
        "description": "",
        "external_urls": {"spotify": "https://open.spotify.com/playlist/pl1"},
        "owner": {"display_name": "Ada", "id": "ada"},
    }
    key = "tracks" if legacy else "items"
    payload[key] = playlist_page(rows, total=total, next_url=next_url, legacy=legacy)
    return payload


class FakeSpotify:
    """The smallest Spotify that answers the calls the client makes."""

    def __init__(self, pages=None, candidates=None, refresh_token="", user="DJ Test"):
        self.calls = []
        self.pages = list(pages or [])
        self.candidates = list(candidates or [])
        self.refresh_token = refresh_token
        self.user = user
        self.created = []
        self.added = []

    def __call__(self, method, url, headers, body):
        payload = json.loads(body.decode("utf-8")) if body and headers.get("Content-Type", "").startswith("application/json") else None
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "json": payload})

        if url == spotify.TOKEN_URL:
            form = parse_qs((body or b"").decode("utf-8"))
            self.last_form = form
            answer = {"access_token": "access-1", "expires_in": 3600, "scope": "playlist-read-private"}
            if form.get("grant_type") == ["authorization_code"]:
                # A real code exchange always returns a refresh token.
                answer["refresh_token"] = self.refresh_token or "refresh-1"
            elif self.refresh_token:
                answer["refresh_token"] = self.refresh_token
            return answer
        if url.endswith("/me"):
            return {"id": "dj-test", "display_name": self.user}
        if method == "GET" and "/search" in url:
            return {"tracks": {"items": self.candidates}}
        if method == "POST" and url.endswith("/me/playlists"):
            self.created.append(payload)
            return {
                "id": "new1",
                "name": payload["name"],
                "public": payload.get("public"),
                "external_urls": {"spotify": "https://open.spotify.com/playlist/new1"},
            }
        if method == "POST" and "/items" in url:
            self.added.extend(payload.get("uris") or [])
            return {"snapshot_id": "snap"}
        if method == "GET" and "/playlists/" in url:
            if not self.pages:
                raise AssertionError(f"no playlist page left for {url}")
            return self.pages.pop(0)
        raise AssertionError(f"unexpected request: {method} {url}")


def make_api(tracks, tmp):
    """An Api over a real store whose library holds ``tracks``.

    ``tracks`` is (artist, title, duration, bpm, key) tuples; the files need not
    exist, because matching never reads one.
    """
    store = Store(Path(tmp))
    library = rekordbox.Library()
    for index, row in enumerate(tracks):
        artist, title, duration = row[0], row[1], row[2]
        track = rekordbox.Track(
            track_id=str(index + 1),
            title=title,
            artist=artist,
            location="file://localhost" + quote(f"/music/{index + 1}.aiff"),
            duration=duration,
            bpm=124.0,
            tonality="8A",
        )
        library.tracks[track.track_id] = track
    store.set_library(library, None, source="library.aiff", source_kind="files")
    return Api(store)


def connected(api, transport):
    api._spotify_transport = transport
    api.spotify_configure(
        {
            "client_id": "client-1",
            "redirect_uri": "http://127.0.0.1:8765/api/spotify/callback",
        }
    )
    api.store.set_spotify_settings({"refresh_token": "refresh-1", "user_name": "DJ Test"})
    return api


# --------------------------------------------------------------- references


class ReferenceTest(unittest.TestCase):
    def test_a_share_link_uri_and_bare_id_all_name_the_same_playlist(self):
        playlist_id = "37i9dQZF1DXcBWIGoYBM5M"
        for value in (
            f"https://open.spotify.com/playlist/{playlist_id}?si=abc123&nd=1",
            f"https://open.spotify.com/intl-de/playlist/{playlist_id}",
            f"spotify:playlist:{playlist_id}",
            playlist_id,
            f"  {playlist_id}  ",
        ):
            self.assertEqual(spotify.parse_playlist_reference(value), playlist_id)

    def test_something_that_is_not_a_playlist_says_so(self):
        with self.assertRaises(spotify.SpotifyError) as caught:
            spotify.parse_playlist_reference("https://open.spotify.com/album/abc")
        self.assertIn("not a Spotify playlist", str(caught.exception))
        with self.assertRaises(spotify.SpotifyError):
            spotify.parse_playlist_reference("   ")


class TidyTextTest(unittest.TestCase):
    def test_accents_case_and_punctuation_do_not_split_a_match(self):
        self.assertEqual(spotify.fold("Beyoncé — DÉJÀ VU!"), "beyonce deja vu")
        self.assertEqual(spotify.fold("AC/DC"), "ac dc")

    def test_a_feature_credit_falls_back_to_the_first_artist(self):
        self.assertEqual(spotify.artist_forms("Ada Kaleh feat. Someone")[:2], ["ada kaleh feat someone", "ada kaleh"])

    def test_a_mix_suffix_is_a_looser_form_of_the_same_title(self):
        forms = spotify.title_forms("Night Ferry (Extended Mix)")
        self.assertEqual(forms[0], "night ferry extended mix")
        self.assertIn("night ferry", forms)


# ------------------------------------------------------------------ pulling


class PullTest(unittest.TestCase):
    def test_current_shape_is_read_in_order(self):
        client = spotify.Client("client-1", spotify.Token(access="a", refresh="r", expires_at=2**31))
        transport = FakeSpotify([spotify_playlist([
            sp_track("t1", "Night Ferry", ["Ada Kaleh"]),
            sp_track("t2", "Salt", ["Some Artist"]),
        ])])
        client.transport = transport
        playlist = spotify.fetch_playlist(client, "https://open.spotify.com/playlist/pl1")
        self.assertEqual(playlist["name"], "Sunset Drive")
        self.assertEqual([track["title"] for track in playlist["tracks"]], ["Night Ferry", "Salt"])
        self.assertEqual(playlist["tracks"][0]["artist"], "Ada Kaleh")
        self.assertEqual(playlist["total"], 2)

    def test_paging_and_the_legacy_tracks_field_are_both_followed(self):
        next_url = f"https://api.spotify.com/v1/playlists/{PLAYLIST_ID}/items?offset=2&limit=2"
        first = spotify_playlist(
            [sp_track("t1", "One", ["A"]), sp_track("t2", "Two", ["B"])],
            total=3, next_url=next_url, legacy=True,
        )
        second = playlist_page([sp_track("t3", "Three", ["C"])], total=3, legacy=True)
        client = spotify.Client("c", spotify.Token(access="a", refresh="r", expires_at=2**31))
        transport = FakeSpotify([first, second])
        client.transport = transport
        playlist = spotify.fetch_playlist(client, PLAYLIST_ID)
        self.assertEqual([track["title"] for track in playlist["tracks"]], ["One", "Two", "Three"])
        self.assertIn(next_url, [call["url"] for call in transport.calls])

    def test_episodes_and_local_files_are_kept_out_and_counted(self):
        episode = sp_track("e1", "Episode", ["Pod"])
        episode["type"] = "episode"
        local = sp_track("", "A Local File", ["A"], is_local=True)
        client = spotify.Client("c", spotify.Token(access="a", refresh="r", expires_at=2**31))
        client.transport = FakeSpotify([spotify_playlist([episode, local])])
        playlist = spotify.fetch_playlist(client, PLAYLIST_ID)
        self.assertEqual([track["title"] for track in playlist["tracks"]], ["A Local File"])
        self.assertEqual(playlist["skipped"], 1)

    def test_a_playlist_without_items_explains_the_spotify_rule(self):
        empty = spotify_playlist([])
        del empty["items"]
        client = spotify.Client("c", spotify.Token(access="a", refresh="r", expires_at=2**31))
        client.transport = FakeSpotify([empty])
        with self.assertRaises(spotify.SpotifyError) as caught:
            spotify.fetch_playlist(client, PLAYLIST_ID)
        self.assertIn("owns or collaborates", str(caught.exception))


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = make_api(
            [
                ("Ada Kaleh", "Night Ferry", 300.0),
                ("Someone Else", "Salt", 240.0),
                ("Third", "Harbour Lights", 200.0),
            ],
            self.tmp.name,
        )
        self.views = self.api.all_views().values()

    def pull(self, rows):
        # ``match_playlist`` takes what ``fetch_playlist`` returns, not the raw
        # API payload the fetch test uses, so the rows are normalised first.
        tracks = [spotify._normalise_track(row) for row in rows]
        return spotify.match_playlist({"name": "Sunset Drive", "tracks": tracks}, self.views)

    def test_an_exact_artist_and_title_matches(self):
        report = self.pull([sp_track("t1", "Night Ferry", ["Ada Kaleh"])])
        self.assertEqual(report["summary"]["matched"], 1)
        self.assertEqual(report["matched"][0]["match"], "exact")
        self.assertEqual(len(report["keys"]), 1)

    def test_a_bracketed_mix_suffix_is_a_loose_match(self):
        report = self.pull([sp_track("t1", "Night Ferry (Extended Mix)", ["Ada Kaleh"])])
        self.assertEqual(report["matched"][0]["match"], "loose")
        self.assertEqual(report["summary"]["missing"], 0)

    def test_a_title_alone_does_not_match_a_different_artist(self):
        report = self.pull([sp_track("t1", "Night Ferry", ["Zed"])])
        self.assertEqual(report["summary"]["matched"], 0)
        self.assertEqual(report["missing"][0]["title"], "Night Ferry")

    def test_missing_tracks_are_reported_and_order_is_kept(self):
        report = self.pull([
            sp_track("t1", "Salt", ["Someone Else"]),
            sp_track("t2", "Unknown Song", ["Nobody"]),
            sp_track("t3", "Night Ferry", ["Ada Kaleh"]),
        ])
        self.assertEqual([row["title"] for row in report["matched"]], ["Salt", "Night Ferry"])
        self.assertEqual([row["title"] for row in report["missing"]], ["Unknown Song"])
        self.assertEqual(report["summary"], {
            "total": 3, "matched": 2, "missing": 1, "in_pool": 2, "repeated": 0,
        })
        labels = [self.api.all_views()[key]["title"] for key in report["keys"]]
        self.assertEqual(labels, ["Salt", "Night Ferry"])

    def test_two_spotify_rows_that_resolve_to_one_file_collapse(self):
        report = self.pull([
            sp_track("t1", "Night Ferry", ["Ada Kaleh"]),
            sp_track("t2", "Night Ferry (Original Mix)", ["Ada Kaleh"]),
        ])
        self.assertEqual(report["summary"]["matched"], 2)
        self.assertEqual(report["summary"]["repeated"], 1)
        self.assertEqual(len(report["keys"]), 1)


# -------------------------------------------------------------------- push


class PushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = make_api(
            [("Ada Kaleh", "Night Ferry", 300.0), ("Nobody", "Mystery Song", 180.0)],
            self.tmp.name,
        )
        self.candidate = {
            "id": "sp1",
            "uri": "spotify:track:sp1",
            "name": "Night Ferry",
            "duration_ms": 301000,
            "artists": [{"name": "Ada Kaleh"}],
            "external_ids": {},
        }

    def test_a_found_track_is_added_and_a_missing_one_is_reported(self):
        transport = FakeSpotify(candidates=[self.candidate])
        connected(self.api, transport)
        views = list(self.api.all_views().values())
        result = spotify.create_playlist(
            self.api._spotify(), "Friday Set", views, public=False
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["missing"][0]["title"], "Mystery Song")
        self.assertFalse(transport.created[0]["public"])
        self.assertEqual(transport.created[0]["name"], "Friday Set")
        self.assertEqual(transport.added, ["spotify:track:sp1"])

    def test_a_long_set_is_added_in_chunks_spotify_accepts(self):
        transport = FakeSpotify(candidates=[self.candidate])
        api = connected(self.api, transport)
        views = list(api.all_views().values())
        with mock.patch.object(spotify, "ADD_CHUNK", 1):
            spotify.create_playlist(api._spotify(), "Chunked", views)
        item_posts = [call for call in transport.calls if call["method"] == "POST" and "/items" in call["url"]]
        self.assertEqual(len(item_posts), 1)  # only the found track: the other misses
        self.assertEqual(transport.added, ["spotify:track:sp1"])

    def test_nothing_found_means_no_empty_playlist_is_left_behind(self):
        transport = FakeSpotify(candidates=[])
        api = connected(self.api, transport)
        views = list(api.all_views().values())
        with self.assertRaises(spotify.SpotifyError) as caught:
            spotify.create_playlist(api._spotify(), "Empty", views)
        self.assertIn("None of these tracks", str(caught.exception))
        self.assertEqual(transport.created, [])


# ------------------------------------------------------------------- tokens


class TokenTest(unittest.TestCase):
    def test_an_expired_access_token_is_refreshed_once_and_persisted(self):
        transport = FakeSpotify(refresh_token="refresh-2")
        saved = []
        client = spotify.Client(
            "client-1",
            spotify.Token(access="old", refresh="refresh-1", expires_at=0),
            transport=transport,
            on_refresh=saved.append,
        )
        self.assertEqual(client.me()["display_name"], "DJ Test")
        self.assertEqual(transport.last_form["grant_type"], ["refresh_token"])
        self.assertEqual(transport.last_form["refresh_token"], ["refresh-1"])
        self.assertEqual(saved[-1].refresh, "refresh-2")
        auth = next(call for call in transport.calls if call["url"].endswith("/me"))
        self.assertEqual(auth["headers"]["Authorization"], "Bearer access-1")

    def test_a_token_endpoint_error_is_readable(self):
        def refuse(*args, **kwargs):
            raise spotify.SpotifyError("Spotify refused the request: invalid_grant.")

        with self.assertRaises(spotify.SpotifyError) as caught:
            spotify.refresh_access_token("client-1", "bad", transport=refuse)
        self.assertIn("invalid_grant", str(caught.exception))

    def test_the_pkce_challenge_is_the_sha256_of_the_verifier(self):
        import base64
        import hashlib

        verifier, challenge = spotify.new_pkce()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        self.assertEqual(challenge, expected)
        self.assertEqual(len(verifier), 96)

    def test_the_authorize_url_carries_everything_spotify_requires(self):
        url = spotify.authorize_url(
            "client-1", "http://127.0.0.1:8765/api/spotify/callback", "state-1", "challenge-1"
        )
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["client_id"], ["client-1"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], ["challenge-1"])
        self.assertEqual(query["state"], ["state-1"])
        self.assertIn("playlist-modify-private", query["scope"][0])


# ---------------------------------------------------------------------- api


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = make_api([("Ada Kaleh", "Night Ferry", 300.0)], self.tmp.name)

    def test_status_names_what_is_missing(self):
        status = self.api.spotify_status()
        self.assertFalse(status["connected"])
        self.assertFalse(status["client_id"])
        self.assertIn("/api/spotify/callback", status["redirect_uri"])

    def test_a_redirect_uri_that_is_not_this_machine_is_refused(self):
        for bad in (
            "https://example.com/api/spotify/callback",
            "http://192.168.1.4:8765/api/spotify/callback",
            "http://127.0.0.1:8765/other",
        ):
            with self.assertRaises(ValueError):
                self.api.spotify_configure({"client_id": "c", "redirect_uri": bad})
        with self.assertRaises(ValueError):
            self.api.spotify_configure({"client_id": "c", "redirect_uri": ""})

    def test_connect_saves_the_refresh_token_and_the_account(self):
        transport = FakeSpotify()
        self.api._spotify_transport = transport
        self.api.spotify_configure(
            {"client_id": "client-1", "redirect_uri": "http://127.0.0.1:8765/api/spotify/callback"}
        )
        started = self.api.spotify_authorize()
        state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]

        html, status = self.api.spotify_callback({"code": ["code-1"], "state": [state]})
        self.assertEqual(status, 200)
        self.assertIn("Spotify is connected", html)

        saved = self.api.store.spotify_settings()
        self.assertEqual(saved["refresh_token"], "refresh-1")
        self.assertEqual(saved["user_name"], "DJ Test")
        status = self.api.spotify_status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["user"], "DJ Test")

    def test_a_callback_state_is_single_use(self):
        self.api._spotify_transport = FakeSpotify()
        self.api.spotify_configure({"client_id": "client-1"})
        state = parse_qs(urlparse(self.api.spotify_authorize()["authorize_url"]).query)["state"][0]
        self.api.spotify_callback({"code": ["code-1"], "state": [state]})
        html, status = self.api.spotify_callback({"code": ["code-1"], "state": [state]})
        self.assertEqual(status, 400)
        self.assertIn("expired", html.lower())

    def test_disconnect_forgets_the_account_but_keeps_the_app(self):
        connected(self.api, FakeSpotify())
        self.api.store.clear_spotify_account()
        status = self.api.spotify_status()
        self.assertFalse(status["connected"])
        self.assertEqual(status["client_id"], "client-1")

    def test_import_returns_the_pool_in_the_playlist_order(self):
        transport = FakeSpotify([spotify_playlist([
            sp_track("t1", "Night Ferry", ["Ada Kaleh"]),
            sp_track("t2", "Not Here", ["Nope"]),
        ])])
        connected(self.api, transport)
        result = self.api.spotify_import({"url": f"https://open.spotify.com/playlist/{PLAYLIST_ID}"})
        self.assertEqual(result["playlist"]["name"], "Sunset Drive")
        self.assertEqual([track["title"] for track in result["tracks"]], ["Night Ferry"])
        self.assertEqual(result["summary"]["missing"], 1)
        self.assertEqual(result["missing"][0]["title"], "Not Here")

    def test_import_without_a_connection_says_so(self):
        self.api.spotify_configure({"client_id": "client-1"})
        with self.assertRaises(ValueError) as caught:
            self.api.spotify_import({"url": PLAYLIST_ID})
        self.assertIn("not connected", str(caught.exception))

    def test_export_creates_the_playlist_from_the_set_order(self):
        transport = FakeSpotify(candidates=[{
            "id": "sp1", "uri": "spotify:track:sp1", "name": "Night Ferry",
            "duration_ms": 300000, "artists": [{"name": "Ada Kaleh"}], "external_ids": {},
        }])
        api = connected(self.api, transport)
        keys = [track["key"] for track in api.tracks(None)["tracks"]]
        result = api.spotify_export({"order": keys, "name": "Friday Set", "public": True})
        self.assertEqual(result["playlist"]["url"], "https://open.spotify.com/playlist/new1")
        self.assertEqual(result["added"], 1)
        self.assertTrue(transport.created[0]["public"])


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = start_in_thread("127.0.0.1", 0, Path(self.tmp.name))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def test_the_callback_address_carries_the_port_actually_bound(self):
        status, body, _ = self.get("/api/spotify")
        self.assertEqual(status, 200)
        answer = json.loads(body)
        self.assertEqual(
            answer["redirect_uri"], f"{self.base}/api/spotify/callback"
        )
        self.assertEqual(answer["callback"], answer["redirect_uri"])

    def test_config_and_a_bad_callback_over_the_wire(self):
        status, answer = self.post("/api/spotify/config", {"client_id": "client-1"})
        self.assertEqual(status, 200)
        self.assertEqual(answer["status"]["client_id"], "client-1")

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/spotify/callback?code=x&state=not-a-state")
        self.assertEqual(caught.exception.code, 400)
        page = caught.exception.read().decode("utf-8")
        self.assertIn("expired", page.lower())


class StoreTest(unittest.TestCase):
    def test_spotify_settings_round_trip_and_empty_values_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.set_spotify_settings({"client_id": "c", "refresh_token": "r", "nonsense": "x"})
            self.assertEqual(store.spotify_settings(), {"client_id": "c", "refresh_token": "r"})
            store.set_spotify_settings({"refresh_token": ""})
            self.assertEqual(store.spotify_settings(), {"client_id": "c"})

            # A second Store reads the same file, which is what the next launch does.
            again = Store(Path(tmp))
            self.assertEqual(again.spotify_settings(), {"client_id": "c"})


if __name__ == "__main__":
    unittest.main()
