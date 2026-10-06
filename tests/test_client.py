"""Unit tests for spotify_mcp. No network: every HTTP call is mocked.

Run:  python3 -m pytest        (or)        python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from spotify_mcp import client as s  # noqa: E402
from spotify_mcp import server  # noqa: E402
import auth_setup  # noqa: E402

FAKE_ENV = {
    "SPOTIFY_CLIENT_ID": "test-client-id",
    "SPOTIFY_CLIENT_SECRET": "test-client-secret",
    "SPOTIFY_REFRESH_TOKEN": "test-refresh-token",
}


class EnvCase(unittest.TestCase):
    """Isolated env + temp config dir so nothing touches real credentials or ~/.config."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {**FAKE_ENV, "SPOTIFY_MCP_CONFIG_DIR": self._tmp.name,
                                                 "SPOTIFY_MCP_TOKEN_CACHE": "on"})
        self._env.start()
        s._MEM_TOKENS.clear()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def token_path(self):
        return os.path.join(self._tmp.name, "tokens.json")


class TestCredentials(EnvCase):
    def test_missing_env_names_every_missing_var(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(s.AuthConfigError) as ctx:
                s._credentials()
        for k in FAKE_ENV:
            self.assertIn(k, str(ctx.exception))

    def test_refresh_uses_env_refresh_token_and_writes_private_cache(self):
        calls = []
        def fake_request(method, url, data=None, headers=None, json_body=None):
            calls.append((url, data, headers))
            return {"access_token": "AT1", "expires_in": 3600}
        with mock.patch.object(s, "_request", fake_request):
            tok = s._get_token()
        self.assertEqual(tok, "AT1")
        url, data, headers = calls[0]
        self.assertTrue(url.endswith("/api/token"))
        self.assertEqual(data, {"grant_type": "refresh_token", "refresh_token": "test-refresh-token"})
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        cached = json.load(open(self.token_path()))
        self.assertEqual(cached["access_token"], "AT1")
        self.assertEqual(oct(os.stat(self.token_path()).st_mode & 0o777), "0o600")

    def test_valid_cached_token_is_reused_without_network(self):
        s._save_tokens({"access_token": "CACHED", "expires_at": time.time() + 600,
                        "refresh_token": "test-refresh-token", "seed_refresh_token": "test-refresh-token"})
        with mock.patch.object(s, "_request", side_effect=AssertionError("no network expected")):
            self.assertEqual(s._get_token(), "CACHED")

    def test_changing_env_refresh_token_invalidates_cache(self):
        s._save_tokens({"access_token": "OLD", "expires_at": time.time() + 600,
                        "refresh_token": "rotated", "seed_refresh_token": "some-older-env-token"})
        with mock.patch.object(s, "_request", return_value={"access_token": "NEW", "expires_in": 3600}):
            self.assertEqual(s._get_token(), "NEW")

    def test_rotated_refresh_token_is_persisted(self):
        with mock.patch.object(s, "_request", return_value={
                "access_token": "AT", "expires_in": 3600, "refresh_token": "ROTATED"}):
            s._get_token()
        cached = json.load(open(self.token_path()))
        self.assertEqual(cached["refresh_token"], "ROTATED")
        self.assertEqual(cached["seed_refresh_token"], "test-refresh-token")

    def test_cache_can_be_disabled(self):
        with mock.patch.dict(os.environ, {"SPOTIFY_MCP_TOKEN_CACHE": "off"}):
            with mock.patch.object(s, "_request", return_value={"access_token": "MEM", "expires_in": 3600}) as r:
                self.assertEqual(s._get_token(), "MEM")
                self.assertEqual(s._get_token(), "MEM")  # second call served from memory
                self.assertEqual(r.call_count, 1)
        self.assertFalse(os.path.exists(self.token_path()))


class TestHttpLayer(EnvCase):
    def _http_error(self, code, body=b"nope", retry_after=None):
        hdrs = {"Retry-After": retry_after} if retry_after else {}
        return urllib.error.HTTPError("http://x", code, "err", hdrs, io.BytesIO(body))

    def test_request_parses_json_and_empty_bodies(self):
        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        with mock.patch("urllib.request.urlopen", return_value=R(b'{"a": 1}')):
            self.assertEqual(s._request("GET", "http://x"), {"a": 1})
        with mock.patch("urllib.request.urlopen", return_value=R(b"")):
            self.assertEqual(s._request("PUT", "http://x"), {})

    def test_request_raises_spotify_error_with_retry_after(self):
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(429, b"slow", "7")):
            with self.assertRaises(s.SpotifyError) as ctx:
                s._request("GET", "http://x")
        self.assertEqual((ctx.exception.code, ctx.exception.retry_after), (429, "7"))

    def test_401_triggers_one_refresh_then_retry(self):
        tokens = iter(["stale", "fresh"])
        seen = []
        def fake_request(method, url, headers=None, **kw):
            seen.append(headers["Authorization"])
            if headers["Authorization"] == "Bearer stale":
                raise s.SpotifyError(401, "expired")
            return {"ok": True}
        with mock.patch.object(s, "_get_token", lambda: next(tokens)), \
             mock.patch.object(s, "_refresh_if_stale") as refresh, \
             mock.patch.object(s, "_request", fake_request):
            self.assertEqual(s._api("GET", "/me"), {"ok": True})
        self.assertEqual(seen, ["Bearer stale", "Bearer fresh"])
        refresh.assert_called_once_with("stale")

    def test_429_backs_off_then_succeeds(self):
        responses = [s.SpotifyError(429, "slow", "1"), {"ok": True}]
        def fake_request(*a, **k):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(s, "_get_token", lambda: "tok"), \
             mock.patch.object(s, "_request", fake_request), \
             mock.patch.object(s.time, "sleep") as sleep:
            self.assertEqual(s._api("GET", "/me"), {"ok": True})
        sleep.assert_called_once_with(1)

    def test_403_is_raised_unchanged(self):
        with mock.patch.object(s, "_get_token", lambda: "tok"), \
             mock.patch.object(s, "_request", side_effect=s.SpotifyError(403, "Forbidden")):
            with self.assertRaises(s.SpotifyError) as ctx:
                s._api("POST", "/playlists/PID/tracks", json_body={})
        self.assertEqual(str(ctx.exception), "HTTP 403: Forbidden")


class TestPureHelpers(unittest.TestCase):
    def test_chunk_splits_100s(self):
        seq = list(range(250))
        batches = s.chunk(seq, 100)
        self.assertEqual([len(b) for b in batches], [100, 100, 50])

    def test_chunk_empty(self):
        self.assertEqual(s.chunk([], 100), [])

    def test_extract_id_variants(self):
        self.assertEqual(s._extract_id("spotify:playlist:ABC123", "playlist"), "ABC123")
        self.assertEqual(
            s._extract_id("https://open.spotify.com/playlist/XYZ789?si=foo", "playlist"), "XYZ789")
        self.assertEqual(s._extract_id("RAWID", "playlist"), "RAWID")
        self.assertEqual(s._extract_id("spotify:track:T1", "track"), "T1")
        self.assertEqual(s._extract_id("spotify:artist:A1", "artist"), "A1")

    def test_normalize_track_uri(self):
        self.assertEqual(s._normalize_track_uri("spotify:track:T1"), "spotify:track:T1")
        self.assertEqual(
            s._normalize_track_uri("https://open.spotify.com/track/T2?si=x"), "spotify:track:T2")
        self.assertIsNone(s._normalize_track_uri("Blinding Lights"))


class TestResolver(unittest.TestCase):
    def test_resolve_uri_passthrough(self):
        # already a URI — should not call search at all
        self.assertEqual(
            s.resolve_uri("spotify:track:T1", search_fn=lambda q: (_ for _ in ()).throw(AssertionError)),
            "spotify:track:T1")

    def test_resolve_uri_by_name(self):
        fake = lambda q: [{"uri": "spotify:track:FOUND"}]
        self.assertEqual(s.resolve_uri("some song", search_fn=fake), "spotify:track:FOUND")

    def test_resolve_uri_miss(self):
        self.assertIsNone(s.resolve_uri("nonsense", search_fn=lambda q: []))

    def test_resolve_many_splits(self):
        def fake(q):
            return [{"uri": "spotify:track:OK"}] if q == "good" else []
        uris, unresolved = s.resolve_many(["good", "spotify:track:DIRECT", "bad"], search_fn=fake)
        self.assertEqual(uris, ["spotify:track:OK", "spotify:track:DIRECT"])
        self.assertEqual(unresolved, ["bad"])


class _ApiRecorder:
    """Fake _api: routes by method+path, records writes."""
    def __init__(self, existing=None, total=0):
        self.existing = existing or []
        self.total = total
        self.posts = []
        self.deletes = []

    def __call__(self, method, path, **kwargs):
        if method == "GET" and "/tracks?fields=items(track(uri))" in path:
            return {"items": [{"track": {"uri": u}} for u in self.existing], "next": None}
        if method == "GET" and "fields=tracks.total" in path:
            return {"tracks": {"total": self.total}}
        if method == "POST" and path.endswith("/tracks"):
            self.posts.append(kwargs.get("json_body", {}).get("uris", []))
            return {}
        if method == "DELETE" and path.endswith("/tracks"):
            self.deletes.append(kwargs.get("json_body", {}).get("tracks", []))
            return {}
        return {}


class TestPlaylistAdd(unittest.TestCase):
    def setUp(self):
        self._api_orig = s._api
        self._search_orig = s.search

    def tearDown(self):
        s._api = self._api_orig
        s.search = self._search_orig

    def test_add_dedupes_and_chunks(self):
        rec = _ApiRecorder(existing=["spotify:track:DUP"], total=1)
        s._api = rec
        # names resolve to distinct URIs; "dupe" resolves to the already-present one
        def fake_search(q, type_="track", limit=5, **kw):
            table = {"a": "spotify:track:A", "b": "spotify:track:B", "dupe": "spotify:track:DUP"}
            return [{"uri": table[q]}] if q in table else []
        s.search = fake_search
        out = s.playlist_add("PID", ["a", "b", "dupe"])
        self.assertEqual(out["added"], 2)           # A, B added
        self.assertEqual(out["skipped_dupes"], 1)   # DUP already present
        self.assertEqual(out["unresolved"], [])
        self.assertEqual(rec.posts, [["spotify:track:A", "spotify:track:B"]])

    def test_add_reports_unresolved(self):
        rec = _ApiRecorder(existing=[], total=0)
        s._api = rec
        s.search = lambda q, type_="track", limit=5, **kw: []  # nothing resolves
        out = s.playlist_add("PID", ["ghost"], dedupe=False)
        self.assertEqual(out["added"], 0)
        self.assertEqual(out["unresolved"], ["ghost"])

    def test_add_respects_10k_ceiling(self):
        rec = _ApiRecorder(existing=[], total=s.MAX_TRACKS_PER_PLAYLIST)  # already full
        s._api = rec
        s.search = lambda q, type_="track", limit=5, **kw: [{"uri": "spotify:track:X"}]
        with self.assertRaises(s.SpotifyError):
            s.playlist_add("PID", ["x"], dedupe=False)
        self.assertEqual(rec.posts, [])  # nothing written

    def test_add_large_batch_chunks_by_100(self):
        rec = _ApiRecorder(existing=[], total=0)
        s._api = rec
        uris = [f"spotify:track:{i}" for i in range(250)]
        s.search = self._search_orig  # unused; all inputs are URIs
        out = s.playlist_add("PID", uris, dedupe=False)
        self.assertEqual(out["added"], 250)
        self.assertEqual([len(b) for b in rec.posts], [100, 100, 50])


class TestDestructiveGating(unittest.TestCase):
    def setUp(self):
        self._api_orig = s._api
        self._search_orig = s.search

    def tearDown(self):
        s._api = self._api_orig
        s.search = self._search_orig

    def test_remove_without_confirm_is_dry_run(self):
        rec = _ApiRecorder()
        s._api = rec
        s.search = self._search_orig
        out = s.playlist_remove("PID", ["spotify:track:A"], confirm=False)
        self.assertTrue(out["dry_run"])
        self.assertEqual(rec.deletes, [])  # nothing deleted

    def test_remove_with_confirm_executes(self):
        rec = _ApiRecorder()
        s._api = rec
        out = s.playlist_remove("PID", ["spotify:track:A", "spotify:track:B"], confirm=True)
        self.assertEqual(out["removed"], 2)
        self.assertEqual(len(rec.deletes), 1)

    def test_empty_without_confirm_is_dry_run(self):
        rec = _ApiRecorder(existing=["spotify:track:A", "spotify:track:B"])
        s._api = rec
        out = s.playlist_empty("PID", confirm=False)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["would_remove_count"], 2)
        self.assertEqual(rec.deletes, [])

    def test_delete_without_confirm_is_dry_run(self):
        rec = _ApiRecorder()
        s._api = rec
        out = s.playlist_delete("PID", confirm=False)
        self.assertTrue(out["dry_run"])


class TestLibraryChunking(unittest.TestCase):
    def setUp(self):
        self._api_orig = s._api

    def tearDown(self):
        s._api = self._api_orig

    def test_save_tracks_chunks_ids_by_50(self):
        puts = []
        def fake(method, path, **kw):
            if method == "PUT" and path == "/me/tracks":
                puts.append(kw["json_body"]["ids"])
            return {}
        s._api = fake
        uris = [f"spotify:track:{i}" for i in range(120)]
        out = s.save_tracks(uris)
        self.assertEqual(out["saved"], 120)
        self.assertEqual([len(b) for b in puts], [50, 50, 20])


class TestServer(EnvCase):
    def rpc(self, method, params=None, rid=1):
        return server.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    def test_initialize_and_notification(self):
        r = self.rpc("initialize")
        self.assertEqual(r["result"]["serverInfo"]["name"], "spotify-mcp")
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_is_complete_and_described(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertGreaterEqual(len(tools), 35)
        self.assertEqual({t["name"] for t in tools}, set(server.TOOLS))
        for t in tools:
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_every_tool_function_accepts_its_schema_properties(self):
        import inspect
        for name, cfg in server.TOOLS.items():
            params = set(inspect.signature(cfg["fn"]).parameters)
            props = set(cfg["schema"]["properties"])
            self.assertTrue(props <= params, f"{name}: schema props {props - params} not in function signature")
            for req in cfg["schema"].get("required", []):
                self.assertIn(req, props)

    def test_tool_call_dispatches_and_serializes(self):
        with mock.patch.dict(server.TOOLS["now_playing"], {"fn": lambda: {"track": "T"}}):
            r = self.rpc("tools/call", {"name": "now_playing", "arguments": {}})
        self.assertEqual(json.loads(r["result"]["content"][0]["text"]), {"track": "T"})

    def test_spotify_error_becomes_is_error_result(self):
        def boom(**kw):
            raise s.SpotifyError(403, "Forbidden")
        with mock.patch.dict(server.TOOLS["pause"], {"fn": boom}):
            r = self.rpc("tools/call", {"name": "pause", "arguments": {}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("403", r["result"]["content"][0]["text"])

    def test_missing_credentials_is_reported_not_crashed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            r = self.rpc("tools/call", {"name": "devices", "arguments": {}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("SPOTIFY_CLIENT_ID", r["result"]["content"][0]["text"])

    def test_unknown_tool_and_method(self):
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})["error"]["code"], -32601)
        self.assertEqual(self.rpc("bogus")["error"]["code"], -32601)

    def test_bad_arguments_return_internal_error(self):
        r = self.rpc("tools/call", {"name": "volume", "arguments": {"wrong": 1}})
        self.assertEqual(r["error"]["code"], -32603)

    def test_destructive_tools_default_to_dry_run_end_to_end(self):
        rec = _ApiRecorder(existing=["spotify:track:A"])
        with mock.patch.object(s, "_api", rec):
            r = self.rpc("tools/call", {"name": "playlist_empty", "arguments": {"playlist_id": "PID"}})
        self.assertTrue(json.loads(r["result"]["content"][0]["text"])["dry_run"])
        self.assertEqual(rec.deletes, [])


class TestAuthSetup(unittest.TestCase):
    def test_pkce_challenge_matches_verifier(self):
        import base64, hashlib
        verifier, challenge = auth_setup.pkce_pair()
        expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, expect)

    def test_authorize_url_contains_required_params_and_all_scopes(self):
        import urllib.parse
        url = auth_setup.build_authorize_url("cid", "http://127.0.0.1:8888/callback", "st", "chal")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["client_id"], ["cid"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["state"], ["st"])
        self.assertEqual(set(q["scope"][0].split()), set(s.SCOPES.split()))
        self.assertIn("playlist-modify-private", q["scope"][0])

    def test_exchange_code_posts_authorization_code_grant(self):
        with mock.patch.object(s, "_request", return_value={"refresh_token": "R"}) as r:
            out = auth_setup.exchange_code("cid", "sec", "CODE", "http://127.0.0.1:8888/callback", "VER")
        self.assertEqual(out, {"refresh_token": "R"})
        data = r.call_args.kwargs["data"]
        self.assertEqual(data["grant_type"], "authorization_code")
        self.assertEqual(data["code"], "CODE")
        self.assertEqual(data["code_verifier"], "VER")

    def test_non_loopback_redirect_is_rejected(self):
        with self.assertRaises(SystemExit):
            auth_setup.wait_for_callback("https://example.com/callback", "st")


if __name__ == "__main__":
    unittest.main(verbosity=2)
