#!/usr/bin/env python3
"""Spotify MCP server: zero-dependency stdio JSON-RPC 2.0 wrapping ``spotify_mcp.client``.

Exposes playback control, queue, search and discovery, library and follows,
and bulk playlist create/add/reorder/rename. Destructive operations (remove /
empty / delete / unsave / unfollow) require ``confirm=true``; without it they
return a dry-run preview.

Credentials are read from SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and
SPOTIFY_REFRESH_TOKEN (see README).
"""

import json
import sys

from . import __version__, client

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_ITEMS = {"type": "array", "items": {"type": "string"},
          "description": "Track names OR URIs OR open.spotify.com URLs — names are auto-resolved."}


def _obj(props, required=None):
    o = {"type": "object", "properties": props}
    if required:
        o["required"] = required
    return o


TOOLS = {
    # ── Playback ──
    "now_playing":       {"fn": client.now_playing,       "schema": _obj({})},
    "play":              {"fn": client.play,              "schema": _obj({"uri": _STR})},
    "pause":             {"fn": client.pause,             "schema": _obj({})},
    "skip":              {"fn": client.skip,              "schema": _obj({})},
    "previous":          {"fn": client.previous,          "schema": _obj({})},
    "volume":            {"fn": client.volume,            "schema": _obj({"pct": _INT}, ["pct"])},
    "seek":              {"fn": client.seek,              "schema": _obj({"ms": _INT}, ["ms"])},
    "queue":             {"fn": client.queue,             "schema": _obj({"uri": _STR}, ["uri"])},
    "devices":           {"fn": client.get_devices,       "schema": _obj({})},
    "transfer":          {"fn": client.transfer_playback, "schema": _obj({"device_id": _STR}, ["device_id"])},
    # ── Search & discovery ──
    "search":            {"fn": client.search,            "schema": _obj({"query": _STR, "type_": _STR, "limit": _INT, "year": _STR, "genre": _STR}, ["query"])},
    "resolve":           {"fn": client.resolve_uri,       "schema": _obj({"text": _STR}, ["text"])},
    "artist_catalog":    {"fn": client.artist_catalog,    "schema": _obj({"artist": _STR, "kind": {"type": "string", "enum": ["top_tracks", "albums"]}, "limit": _INT}, ["artist"])},
    "recommendations":   {"fn": client.recommendations,   "schema": _obj({"seed_tracks": _ITEMS, "seed_artists": _ITEMS, "seed_genres": _ITEMS, "limit": _INT})},
    "audio_features":    {"fn": client.audio_features,    "schema": _obj({"track": _STR}, ["track"])},
    "recent_tracks":     {"fn": client.recent_tracks,     "schema": _obj({"limit": _INT})},
    "top_tracks":        {"fn": client.top_tracks,        "schema": _obj({"limit": _INT, "time_range": {"type": "string", "enum": ["short_term", "medium_term", "long_term"]}})},
    # ── Playlists (read + write) ──
    "playlist_list":     {"fn": client.playlist_list,     "schema": _obj({"limit": _INT})},
    "playlist_get":      {"fn": client.playlist_get,      "schema": _obj({"playlist_id": _STR, "limit": _INT, "offset": _INT}, ["playlist_id"])},
    "playlist_create":   {"fn": client.playlist_create,   "schema": _obj({"name": _STR, "public": _BOOL, "description": _STR}, ["name"])},
    "playlist_add":      {"fn": client.playlist_add,      "schema": _obj({"playlist_id": _STR, "items": _ITEMS, "dedupe": _BOOL}, ["playlist_id", "items"])},
    "playlist_remove":   {"fn": client.playlist_remove,   "schema": _obj({"playlist_id": _STR, "items": _ITEMS, "confirm": _BOOL}, ["playlist_id", "items"])},
    "playlist_reorder":  {"fn": client.playlist_reorder,  "schema": _obj({"playlist_id": _STR, "range_start": _INT, "insert_before": _INT, "range_length": _INT}, ["playlist_id", "range_start", "insert_before"])},
    "playlist_rename":   {"fn": client.playlist_rename,   "schema": _obj({"playlist_id": _STR, "name": _STR}, ["playlist_id", "name"])},
    "playlist_set_details": {"fn": client.playlist_set_details, "schema": _obj({"playlist_id": _STR, "name": _STR, "description": _STR, "public": _BOOL}, ["playlist_id"])},
    "playlist_empty":    {"fn": client.playlist_empty,    "schema": _obj({"playlist_id": _STR, "confirm": _BOOL}, ["playlist_id"])},
    "playlist_delete":   {"fn": client.playlist_delete,   "schema": _obj({"playlist_id": _STR, "confirm": _BOOL}, ["playlist_id"])},
    # ── Library & follows ──
    "saved_tracks":      {"fn": client.saved_tracks,      "schema": _obj({"limit": _INT})},
    "save_tracks":       {"fn": client.save_tracks,       "schema": _obj({"items": _ITEMS}, ["items"])},
    "unsave_tracks":     {"fn": client.unsave_tracks,     "schema": _obj({"items": _ITEMS, "confirm": _BOOL}, ["items"])},
    "saved_albums":      {"fn": client.saved_albums,      "schema": _obj({"limit": _INT})},
    "save_albums":       {"fn": client.save_albums,       "schema": _obj({"items": _ITEMS}, ["items"])},
    "unsave_albums":     {"fn": client.unsave_albums,     "schema": _obj({"items": _ITEMS, "confirm": _BOOL}, ["items"])},
    "followed_artists":  {"fn": client.followed_artists,  "schema": _obj({"limit": _INT})},
    "follow_artists":    {"fn": client.follow_artists,    "schema": _obj({"items": _ITEMS}, ["items"])},
    "unfollow_artists":  {"fn": client.unfollow_artists,  "schema": _obj({"items": _ITEMS, "confirm": _BOOL}, ["items"])},
}

TOOL_DESCS = {
    "now_playing":       "What's currently playing on Spotify (track, artist, album, progress).",
    "play":              "Resume playback, or play a specific track/album/playlist URI.",
    "pause":             "Pause playback.",
    "skip":              "Skip to the next track.",
    "previous":          "Go to the previous track.",
    "volume":            "Set playback volume 0-100.",
    "seek":              "Seek to a position (milliseconds) in the current track.",
    "queue":             "Add a track URI to the playback queue.",
    "devices":           "List available playback devices (speakers, desktop apps, phone).",
    "transfer":          "Move playback to a specific device by id.",
    "search":            "Search tracks/artists/albums/playlists. Optional year & genre filters.",
    "resolve":           "Resolve a track name (e.g. 'Blinding Lights by The Weeknd') to its URI.",
    "artist_catalog":    "An artist's top tracks or album catalog (name/URI/URL accepted).",
    "recommendations":   "Recommendations from seed tracks/artists/genres (Spotify has restricted this endpoint for newer apps; may return an explanatory error).",
    "audio_features":    "Audio features (tempo/energy/danceability) for a track (Spotify has restricted this endpoint for newer apps; may return an explanatory error).",
    "recent_tracks":     "Recently played tracks.",
    "top_tracks":        "Your top tracks over short/medium/long term.",
    "playlist_list":     "List your playlists (with ids + track counts).",
    "playlist_get":      "Get a playlist's details + tracks (paginated).",
    "playlist_create":   "Create a new playlist.",
    "playlist_add":      "Bulk-add songs (names or URIs) to a playlist. Auto-resolves names, dedupes, and chunks any number of songs up to Spotify's 10k/playlist ceiling.",
    "playlist_remove":   "Remove songs from a playlist. DESTRUCTIVE — needs confirm=true, else returns a dry-run preview.",
    "playlist_reorder":  "Move a track (or range) to a new position in a playlist.",
    "playlist_rename":   "Rename a playlist.",
    "playlist_set_details": "Update a playlist's name / description / public flag.",
    "playlist_empty":    "Remove ALL tracks from a playlist. DESTRUCTIVE — needs confirm=true.",
    "playlist_delete":   "Delete (unfollow) a playlist. DESTRUCTIVE — needs confirm=true.",
    "saved_tracks":      "Your Liked Songs.",
    "save_tracks":       "Add songs (names or URIs) to Liked Songs.",
    "unsave_tracks":     "Remove songs from Liked Songs. DESTRUCTIVE — needs confirm=true.",
    "saved_albums":      "Your saved albums.",
    "save_albums":       "Save albums (ids/URIs/URLs).",
    "unsave_albums":     "Remove saved albums. DESTRUCTIVE — needs confirm=true.",
    "followed_artists":  "Artists you follow (needs follow scope — needs the user-follow-* scopes).",
    "follow_artists":    "Follow artists (needs follow scope).",
    "unfollow_artists":  "Unfollow artists. DESTRUCTIVE — needs confirm=true (needs follow scope).",
}


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    """Handle one JSON-RPC request dict; return a response dict, or None for notifications."""
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "spotify-mcp", "version": __version__}
        }}
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        tools = [{"name": name, "description": TOOL_DESCS[name], "inputSchema": cfg["schema"]}
                 for name, cfg in TOOLS.items()]
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}
    if method == "tools/call":
        params = req.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        try:
            result = TOOLS[name]["fn"](**args)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
            }}
        except (client.SpotifyError, client.AuthConfigError) as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)}, indent=2)}],
                "isError": True
            }}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        try:
            resp = handle(req)
        except Exception as e:
            # A malformed-but-valid-JSON payload (e.g. a bare list/string instead
            # of an object) must not take down the whole persistent server.
            rid = req.get("id") if isinstance(req, dict) else None
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        if resp is not None:
            send(resp)


if __name__ == "__main__":
    main()
