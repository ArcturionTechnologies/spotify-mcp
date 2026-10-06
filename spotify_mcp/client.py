"""Spotify Web API client: stdlib only, no third-party dependencies.

Credentials come from environment variables:

    SPOTIFY_CLIENT_ID       your Spotify developer app's client id
    SPOTIFY_CLIENT_SECRET   your Spotify developer app's client secret
    SPOTIFY_REFRESH_TOKEN   a refresh token obtained with ``auth_setup.py``

Access tokens are minted from the refresh token on demand and cached in
``~/.config/spotify-mcp/tokens.json`` (override the directory with
``SPOTIFY_MCP_CONFIG_DIR``; disable the on-disk cache with
``SPOTIFY_MCP_TOKEN_CACHE=off``, in which case tokens live in memory only).
"""

import base64
import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:  # advisory cross-process locking is POSIX-only; degrade gracefully elsewhere
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

API_BASE = "https://api.spotify.com/v1"
AUTH_BASE = "https://accounts.spotify.com"
HTTP_TIMEOUT = 30

SCOPES = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
    "user-read-playback-position",
    "user-top-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-read",
    "user-library-modify",
    "user-follow-read",
    "user-follow-modify",
])

# Spotify hard limits (theirs, not ours)
MAX_TRACKS_PER_PLAYLIST = 10000   # a single playlist cannot exceed this
ITEMS_PER_WRITE = 100             # add/remove endpoints take at most 100 uris/call
IDS_PER_BATCH = 50                # saved/follow endpoints take at most 50 ids/call


class SpotifyError(RuntimeError):
    """Structured API error carrying the HTTP status + optional Retry-After."""
    def __init__(self, code, body, retry_after=None):
        self.code = code
        self.body = body
        self.retry_after = retry_after
        super().__init__(f"HTTP {code}: {body}")


class AuthConfigError(RuntimeError):
    """Required credentials are missing from the environment."""


# -- Credentials & token cache ------------------------------------------------

def _config_dir():
    return Path(os.environ.get("SPOTIFY_MCP_CONFIG_DIR", "~/.config/spotify-mcp")).expanduser()


def _token_file():
    return _config_dir() / "tokens.json"


def _cache_enabled():
    return os.environ.get("SPOTIFY_MCP_TOKEN_CACHE", "on").strip().lower() not in ("off", "0", "false", "no")


def _credentials():
    """Read the three required env vars, with a clear error naming any that are missing."""
    keys = ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN")
    env = {k: os.environ.get(k, "").strip() for k in keys}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise AuthConfigError(
            "Missing environment variable(s): " + ", ".join(missing)
            + ". See the README for how to create a Spotify app and obtain a refresh token.")
    return env


_MEM_TOKENS = {}
_MEM_LOCK = threading.Lock()


def _load_tokens():
    """Current token state, seeded from SPOTIFY_REFRESH_TOKEN.

    The cache holds the access token plus the (possibly rotated) refresh token.
    It records which env refresh token it was seeded from, so pasting a fresh
    SPOTIFY_REFRESH_TOKEN into your config invalidates a stale cache.
    """
    seed = _credentials()["SPOTIFY_REFRESH_TOKEN"]
    cached = {}
    if _cache_enabled():
        try:
            cached = json.loads(_token_file().read_text())
        except (OSError, json.JSONDecodeError):
            cached = {}
    else:
        cached = dict(_MEM_TOKENS)
    if cached.get("seed_refresh_token") != seed or not cached.get("refresh_token"):
        return {"refresh_token": seed, "seed_refresh_token": seed}
    return cached


def _save_tokens(tokens):
    if not _cache_enabled():
        _MEM_TOKENS.clear()
        _MEM_TOKENS.update(tokens)
        return
    path = _token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (temp file + replace) so a concurrent reader in another
    # process never observes a partially-written token file.
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(tokens, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


@contextlib.contextmanager
def _token_lock():
    """Serialize token refresh/save across threads and (on POSIX) processes.

    Several MCP server instances may share one token cache. Without a lock, two
    of them can both see an expired token, both refresh, and race Spotify's
    rotating refresh_token, leaving the loser holding a superseded one.
    """
    with _MEM_LOCK:
        if fcntl is None or not _cache_enabled():
            yield
            return
        lock_path = _token_file().with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _request(method, url, data=None, headers=None, json_body=None):
    headers = dict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        retry_after = e.headers.get("Retry-After") if e.headers else None
        raise SpotifyError(e.code, err, retry_after)


def _refresh_access_token(tokens):
    creds = _credentials()
    basic = base64.b64encode(
        f"{creds['SPOTIFY_CLIENT_ID']}:{creds['SPOTIFY_CLIENT_SECRET']}".encode()).decode()
    result = _request("POST", f"{AUTH_BASE}/api/token",
                      data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
                      headers={"Authorization": f"Basic {basic}"})
    tokens = dict(tokens)
    tokens["access_token"] = result["access_token"]
    tokens["expires_at"] = time.time() + result["expires_in"] - 60
    if "refresh_token" in result:  # Spotify may rotate the refresh token
        tokens["refresh_token"] = result["refresh_token"]
    _save_tokens(tokens)
    return tokens


def _get_token():
    _credentials()  # fail fast, before touching the filesystem
    with _token_lock():
        tokens = _load_tokens()
        if not tokens.get("access_token") or time.time() >= tokens.get("expires_at", 0):
            tokens = _refresh_access_token(tokens)
        return tokens["access_token"]


def _refresh_if_stale(prev_token):
    """Refresh under the lock, but skip if another process already rotated the
    token out from under us (avoids a redundant refresh racing the one that
    just succeeded elsewhere)."""
    with _token_lock():
        tokens = _load_tokens()
        if tokens.get("access_token") in (None, prev_token):
            _refresh_access_token(tokens)


def _api(method, path, **kwargs):
    """Authenticated Spotify call with a single 401-refresh retry and 429 backoff."""
    base_headers = kwargs.pop("headers", {})
    attempts = 0
    while True:
        attempts += 1
        token = _get_token()
        headers = dict(base_headers)
        headers["Authorization"] = f"Bearer {token}"
        try:
            return _request(method, f"{API_BASE}{path}", headers=headers, **kwargs)
        except SpotifyError as e:
            if e.code == 401 and attempts == 1:
                _refresh_if_stale(token)  # token rejected: force a refresh, retry once
                continue
            if e.code == 429 and attempts <= 3:
                wait = int(e.retry_after) if e.retry_after and str(e.retry_after).isdigit() else 2
                time.sleep(min(wait, 30))
                continue
            raise


# ── Playback ──────────────────────────────────────────────────────────────────

def now_playing():
    """Return current playing track info."""
    r = _api("GET", "/me/player/currently-playing")
    if not r or r.get("currently_playing_type") != "track":
        return {"status": "nothing playing"}
    item = r["item"]
    return {
        "track": item["name"],
        "artist": ", ".join(a["name"] for a in item["artists"]),
        "album": item["album"]["name"],
        "progress_ms": r["progress_ms"],
        "duration_ms": item["duration_ms"],
        "is_playing": r["is_playing"],
        "uri": item["uri"],
    }


def play(uri=None):
    """Resume playback, or play a specific track/album/playlist URI."""
    body = {}
    if uri:
        if uri.startswith("spotify:track:"):
            body["uris"] = [uri]
        else:
            body["context_uri"] = uri
    _api("PUT", "/me/player/play", json_body=body)
    return "playing"


def pause():
    _api("PUT", "/me/player/pause")
    return "paused"


def skip():
    _api("POST", "/me/player/next")
    return "skipped"


def previous():
    _api("POST", "/me/player/previous")
    return "went to previous track"


def volume(pct: int):
    """Set volume 0-100."""
    _api("PUT", f"/me/player/volume?volume_percent={max(0, min(100, pct))}")
    return f"volume {pct}%"


def seek(ms: int):
    """Seek to position in ms."""
    _api("PUT", f"/me/player/seek?position_ms={ms}")
    return f"seeked to {ms}ms"


def queue(uri: str):
    """Add track URI to queue."""
    _api("POST", f"/me/player/queue?uri={uri}")
    return f"queued {uri}"


def get_devices():
    r = _api("GET", "/me/player/devices")
    return r.get("devices", [])


def transfer_playback(device_id: str):
    _api("PUT", "/me/player", json_body={"device_ids": [device_id], "play": True})
    return f"transferred playback to {device_id}"


# ── Search ────────────────────────────────────────────────────────────────────

def search(query: str, type_="track", limit=5, year=None, genre=None):
    """Search Spotify. type_: track, album, artist, playlist.

    Optional filters: year (e.g. 2020 or "2018-2022"), genre (e.g. "indie").
    """
    q = query
    if year:
        q += f" year:{year}"
    if genre:
        q += f" genre:{genre}"
    params = urllib.parse.urlencode({"q": q, "type": type_, "limit": limit})
    r = _api("GET", f"/search?{params}")
    key = f"{type_}s"
    items = r.get(key, {}).get("items", [])
    results = []
    for item in items:
        if type_ == "track":
            results.append({
                "name": item["name"],
                "artist": ", ".join(a["name"] for a in item["artists"]),
                "uri": item["uri"],
                "duration_ms": item["duration_ms"],
            })
        elif type_ == "artist":
            results.append({"name": item["name"], "uri": item["uri"], "genres": item.get("genres", [])})
        else:
            results.append({"name": item["name"], "uri": item["uri"]})
    return results


# ── Library ───────────────────────────────────────────────────────────────────

def recent_tracks(limit=10):
    params = urllib.parse.urlencode({"limit": limit})
    r = _api("GET", f"/me/player/recently-played?{params}")
    return [
        {
            "track": i["track"]["name"],
            "artist": ", ".join(a["name"] for a in i["track"]["artists"]),
            "played_at": i["played_at"],
            "uri": i["track"]["uri"],
        }
        for i in r.get("items", [])
    ]


def top_tracks(limit=10, time_range="medium_term"):
    params = urllib.parse.urlencode({"limit": limit, "time_range": time_range})
    r = _api("GET", f"/me/top/tracks?{params}")
    return [
        {"track": i["name"], "artist": ", ".join(a["name"] for a in i["artists"]), "uri": i["uri"]}
        for i in r.get("items", [])
    ]


def get_playlists(limit=20):
    r = _api("GET", f"/me/playlists?limit={limit}")
    return [{"name": p["name"], "uri": p["uri"], "tracks": p["tracks"]["total"]} for p in r.get("items", [])]


# ── Helpers: ids, chunking, resolving ──────────────────────────────────────────

def _extract_id(s, kind):
    """Accept a raw id, spotify:{kind}:{id}, or open.spotify.com/{kind}/{id}."""
    s = str(s).strip()
    if s.startswith(f"spotify:{kind}:"):
        return s.split(":")[2]
    if "open.spotify.com" in s and f"/{kind}/" in s:
        return s.split(f"/{kind}/", 1)[1].split("?")[0].split("/")[0]
    return s  # assume already a raw id


def chunk(seq, n):
    """Split seq into consecutive lists of at most n items each."""
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _normalize_track_uri(text):
    """If text is already a track URI/URL, return spotify:track:ID, else None."""
    t = str(text).strip()
    if t.startswith("spotify:track:"):
        return t
    if "open.spotify.com/track/" in t:
        tid = t.split("/track/", 1)[1].split("?")[0].split("/")[0]
        return f"spotify:track:{tid}"
    return None


def resolve_uri(text, search_fn=None):
    """Resolve a track name (or URI/URL) to a spotify:track: URI. None if unresolved.

    search_fn is injectable for testing; defaults to a live 1-result track search.
    """
    direct = _normalize_track_uri(text)
    if direct:
        return direct
    sf = search_fn or (lambda q: search(q, "track", limit=1))
    hits = sf(text)
    return hits[0]["uri"] if hits else None


def resolve_many(items, search_fn=None):
    """Resolve a list of names/URIs -> (uris, unresolved_inputs)."""
    uris, unresolved = [], []
    for it in items:
        u = resolve_uri(it, search_fn=search_fn)
        if u:
            uris.append(u)
        else:
            unresolved.append(it)
    return uris, unresolved


def _resolve_artist_id(artist):
    if artist.startswith("spotify:artist:") or "open.spotify.com/artist/" in artist:
        return _extract_id(artist, "artist")
    if len(artist) == 22 and artist.isalnum():
        return artist
    hits = search(artist, "artist", limit=1)
    return hits[0]["uri"].split(":")[2] if hits else None


# ── Playlists (read + write) ────────────────────────────────────────────────────

def playlist_list(limit=50):
    r = _api("GET", f"/me/playlists?limit={min(limit, 50)}")
    out = []
    for p in r.get("items", []):
        if not p:  # Spotify occasionally returns null entries
            continue
        out.append({"id": p.get("id"), "name": p.get("name"), "uri": p.get("uri"),
                    "tracks": (p.get("tracks") or {}).get("total"), "public": p.get("public")})
    return out


def playlist_track_uris(playlist_id):
    """All track URIs currently in a playlist (paginated)."""
    pid = _extract_id(playlist_id, "playlist")
    uris, offset = [], 0
    while True:
        r = _api("GET", f"/playlists/{pid}/tracks?fields=items(track(uri)),next&limit=100&offset={offset}")
        for it in r.get("items", []):
            tr = it.get("track") or {}
            if tr.get("uri"):
                uris.append(tr["uri"])
        if not r.get("next"):
            break
        offset += 100
    return uris


def playlist_get(playlist_id, limit=100, offset=0):
    pid = _extract_id(playlist_id, "playlist")
    meta = _api("GET", f"/playlists/{pid}?fields=name,description,public,tracks.total")
    r = _api("GET", f"/playlists/{pid}/tracks?limit={min(limit, 100)}&offset={offset}"
                    f"&fields=items(track(name,uri,artists(name)))")
    tracks = []
    for it in r.get("items", []):
        tr = it.get("track") or {}
        tracks.append({"name": tr.get("name"),
                       "artist": ", ".join(a["name"] for a in tr.get("artists", [])),
                       "uri": tr.get("uri")})
    return {"name": meta.get("name"), "description": meta.get("description"),
            "public": meta.get("public"), "total": meta.get("tracks", {}).get("total"),
            "tracks": tracks}


def playlist_create(name, public=False, description=""):
    # NOTE: /users/{id}/playlists was retired for Development-Mode apps in
    # Spotify's Feb 2026 Web API migration (returns 403). /me/playlists is the
    # supported replacement.
    r = _api("POST", "/me/playlists",
             json_body={"name": name, "public": public, "description": description})
    return {"id": r["id"], "name": r["name"], "uri": r["uri"],
            "url": r.get("external_urls", {}).get("spotify")}


def playlist_add(playlist_id, items, dedupe=True):
    """Add tracks (names or URIs) to a playlist. Auto-resolves, dedupes, chunks by 100.

    Returns {added, skipped_dupes, unresolved}. Raises if it would break the 10k ceiling.
    """
    pid = _extract_id(playlist_id, "playlist")
    uris, unresolved = resolve_many(items)
    skipped_dupes = 0
    if dedupe and uris:
        existing = set(playlist_track_uris(pid))
        deduped, seen = [], set()
        for u in uris:
            if u in existing or u in seen:
                skipped_dupes += 1
            else:
                seen.add(u)
                deduped.append(u)
        uris = deduped
    current_total = _api("GET", f"/playlists/{pid}?fields=tracks.total").get("tracks", {}).get("total", 0)
    if current_total + len(uris) > MAX_TRACKS_PER_PLAYLIST:
        raise SpotifyError(400, f"Would exceed Spotify's {MAX_TRACKS_PER_PLAYLIST}-track playlist "
                                f"ceiling (has {current_total}, adding {len(uris)}).")
    added = 0
    for batch in chunk(uris, ITEMS_PER_WRITE):
        _api("POST", f"/playlists/{pid}/tracks", json_body={"uris": batch})
        added += len(batch)
    return {"added": added, "skipped_dupes": skipped_dupes, "unresolved": unresolved}


def playlist_remove(playlist_id, items, confirm=False):
    pid = _extract_id(playlist_id, "playlist")
    uris, unresolved = resolve_many(items)
    if not confirm:
        return {"dry_run": True, "would_remove": uris, "unresolved": unresolved,
                "hint": "destructive — call again with confirm=true to execute"}
    removed = 0
    for batch in chunk(uris, ITEMS_PER_WRITE):
        _api("DELETE", f"/playlists/{pid}/tracks", json_body={"tracks": [{"uri": u} for u in batch]})
        removed += len(batch)
    return {"removed": removed, "unresolved": unresolved}


def playlist_reorder(playlist_id, range_start, insert_before, range_length=1):
    pid = _extract_id(playlist_id, "playlist")
    _api("PUT", f"/playlists/{pid}/tracks", json_body={
        "range_start": range_start, "insert_before": insert_before, "range_length": range_length})
    return {"reordered": True}


def playlist_set_details(playlist_id, name=None, description=None, public=None):
    pid = _extract_id(playlist_id, "playlist")
    body = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if public is not None:
        body["public"] = public
    if not body:
        return {"changed": False}
    _api("PUT", f"/playlists/{pid}", json_body=body)
    return {"changed": True, **body}


def playlist_rename(playlist_id, name):
    return playlist_set_details(playlist_id, name=name)


def playlist_empty(playlist_id, confirm=False):
    pid = _extract_id(playlist_id, "playlist")
    uris = playlist_track_uris(pid)
    if not confirm:
        return {"dry_run": True, "would_remove_count": len(uris),
                "hint": "destructive — call again with confirm=true to empty this playlist"}
    removed = 0
    for batch in chunk(uris, ITEMS_PER_WRITE):
        _api("DELETE", f"/playlists/{pid}/tracks", json_body={"tracks": [{"uri": u} for u in batch]})
        removed += len(batch)
    return {"emptied": True, "removed": removed}


def playlist_delete(playlist_id, confirm=False):
    """Delete = unfollow your own playlist (Spotify has no hard-delete)."""
    pid = _extract_id(playlist_id, "playlist")
    if not confirm:
        return {"dry_run": True, "would_delete": pid,
                "hint": "destructive — call again with confirm=true to delete this playlist"}
    _api("DELETE", f"/playlists/{pid}/followers")
    return {"deleted": True, "id": pid}


# ── Library & follows ────────────────────────────────────────────────────────────

def saved_tracks(limit=50):
    r = _api("GET", f"/me/tracks?limit={min(limit, 50)}")
    return [{"name": i["track"]["name"],
             "artist": ", ".join(a["name"] for a in i["track"]["artists"]),
             "uri": i["track"]["uri"]} for i in r.get("items", [])]


def save_tracks(items):
    uris, unresolved = resolve_many(items)
    ids = [u.split(":")[2] for u in uris]
    for batch in chunk(ids, IDS_PER_BATCH):
        _api("PUT", "/me/tracks", json_body={"ids": batch})
    return {"saved": len(ids), "unresolved": unresolved}


def unsave_tracks(items, confirm=False):
    uris, unresolved = resolve_many(items)
    ids = [u.split(":")[2] for u in uris]
    if not confirm:
        return {"dry_run": True, "would_unsave": len(ids), "unresolved": unresolved,
                "hint": "destructive — call again with confirm=true"}
    for batch in chunk(ids, IDS_PER_BATCH):
        _api("DELETE", "/me/tracks", json_body={"ids": batch})
    return {"unsaved": len(ids), "unresolved": unresolved}


def saved_albums(limit=50):
    r = _api("GET", f"/me/albums?limit={min(limit, 50)}")
    return [{"name": i["album"]["name"],
             "artist": ", ".join(a["name"] for a in i["album"]["artists"]),
             "uri": i["album"]["uri"]} for i in r.get("items", [])]


def save_albums(items):
    ids = [_extract_id(x, "album") for x in items]
    for batch in chunk(ids, IDS_PER_BATCH):
        _api("PUT", "/me/albums", json_body={"ids": batch})
    return {"saved": len(ids)}


def unsave_albums(items, confirm=False):
    ids = [_extract_id(x, "album") for x in items]
    if not confirm:
        return {"dry_run": True, "would_unsave": len(ids),
                "hint": "destructive — call again with confirm=true"}
    for batch in chunk(ids, IDS_PER_BATCH):
        _api("DELETE", "/me/albums", json_body={"ids": batch})
    return {"unsaved": len(ids)}


_FOLLOW_REAUTH = {"error": "Follow features need the user-follow-read / user-follow-modify scopes. "
                  "Re-run auth_setup.py to get a new refresh token (see README)."}


def _is_scope_error(e):
    return isinstance(e, SpotifyError) and e.code in (401, 403)


def followed_artists(limit=50):
    try:
        r = _api("GET", f"/me/following?type=artist&limit={min(limit, 50)}")
    except SpotifyError as e:
        if _is_scope_error(e):
            return _FOLLOW_REAUTH
        raise
    return [{"name": a["name"], "uri": a["uri"], "genres": a.get("genres", [])}
            for a in r.get("artists", {}).get("items", [])]


def follow_artists(items):
    ids = [_extract_id(x, "artist") for x in items]
    try:
        for batch in chunk(ids, IDS_PER_BATCH):
            _api("PUT", "/me/following?type=artist", json_body={"ids": batch})
    except SpotifyError as e:
        if _is_scope_error(e):
            return _FOLLOW_REAUTH
        raise
    return {"followed": len(ids)}


def unfollow_artists(items, confirm=False):
    ids = [_extract_id(x, "artist") for x in items]
    if not confirm:
        return {"dry_run": True, "would_unfollow": len(ids),
                "hint": "destructive — call again with confirm=true"}
    try:
        for batch in chunk(ids, IDS_PER_BATCH):
            _api("DELETE", "/me/following?type=artist", json_body={"ids": batch})
    except SpotifyError as e:
        if _is_scope_error(e):
            return _FOLLOW_REAUTH
        raise
    return {"unfollowed": len(ids)}


# ── Discovery ────────────────────────────────────────────────────────────────────

def artist_catalog(artist, kind="top_tracks", limit=20, market="US"):
    """kind: top_tracks | albums. artist may be a name, URI, URL, or id."""
    aid = _resolve_artist_id(artist)
    if not aid:
        return {"error": f"artist not found: {artist}"}
    if kind == "albums":
        r = _api("GET", f"/artists/{aid}/albums?limit={min(limit, 50)}&include_groups=album,single")
        return [{"name": a["name"], "uri": a["uri"], "release_date": a.get("release_date")}
                for a in r.get("items", [])]
    r = _api("GET", f"/artists/{aid}/top-tracks?market={market}")
    return [{"name": t["name"], "album": t["album"]["name"], "uri": t["uri"]}
            for t in r.get("tracks", [])][:limit]


def recommendations(seed_tracks=None, seed_artists=None, seed_genres=None, limit=20):
    params = {"limit": min(limit, 100)}
    if seed_tracks:
        params["seed_tracks"] = ",".join(_extract_id(x, "track") for x in seed_tracks)
    if seed_artists:
        params["seed_artists"] = ",".join(_extract_id(x, "artist") for x in seed_artists)
    if seed_genres:
        params["seed_genres"] = ",".join(seed_genres)
    try:
        r = _api("GET", "/recommendations?" + urllib.parse.urlencode(params))
    except SpotifyError as e:
        if e.code in (403, 404):
            return {"error": "Spotify retired the recommendations endpoint for this app; use search instead."}
        raise
    return [{"name": t["name"], "artist": ", ".join(a["name"] for a in t["artists"]), "uri": t["uri"]}
            for t in r.get("tracks", [])]


def audio_features(track):
    tid = _extract_id(track, "track")
    try:
        return _api("GET", f"/audio-features/{tid}")
    except SpotifyError as e:
        if e.code in (403, 404):
            return {"error": "Spotify retired the audio-features endpoint for this app."}
        raise
